"""Cross-client isolation of the audit log, as `shifde_app`: client A's context cannot read B's
events or the global (installation-level) ones, cannot write for B, and cannot write a global
row (ARCH m7: SETH's `write_own` admitted `tenant_id IS NULL` to every client). With no tenant
context nothing is visible.

The module has no routes yet (they arrive with app and governance); `audit.service` is the
surface other modules call, so the service is tested here too."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, PendingRollbackError
from sqlalchemy.orm import Session

from shifde.audit import repository
from shifde.audit import service as audit
from shifde.audit.models import AuditEvent
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import (
    Database,
    get_context,
    set_platform_admin,
    set_principal,
    set_tenant,
)
from shifde.core.ids import uuid7

INSERT = (
    "INSERT INTO shifde.audit_events (id, tenant_id, actor_type, action, outcome) "
    "VALUES (gen_random_uuid(), {tenant}, 'user', 'test.forged', 'success')"
)


@dataclass
class World:
    a: UUID
    b: UUID
    global_id: UUID
    a_id: UUID
    b_id: UUID


@pytest.fixture
def world(owner_db: Database) -> Iterator[World]:
    a, b = uuid7(), uuid7()
    ctx = RequestContext(request_id="seed")
    with owner_db.session() as s:
        audit.record(s, ctx, action="test.a", tenant_id=a)
        audit.record(s, ctx, action="test.b", tenant_id=b)
        audit.record(s, ctx, action="identity.login.failed", outcome="failure", tenant_id=None)
        ids = {r[0]: r[1] for r in s.execute(text("SELECT action, id FROM shifde.audit_events"))}
        s.commit()
    yield World(a, b, ids["identity.login.failed"], ids["test.a"], ids["test.b"])


def _as(db: Database, tenant: UUID | None) -> Session:
    s = db.session()
    if tenant is not None:
        set_tenant(s, tenant)
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    return s


def _ids(s: Session) -> set[UUID]:
    return {r[0] for r in s.execute(text("SELECT id FROM shifde.audit_events"))}


def _sqlstate(err: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    return getattr(err.value.orig, "sqlstate", None)


def test_a_client_sees_only_its_own_events(app_db: Database, world: World) -> None:
    with _as(app_db, world.a) as s:
        assert _ids(s) == {world.a_id}
        rows = audit.list_events(
            s,
            tenant_id=world.b,
            category=None,
            actor=None,
            action=None,
            occurred_from=None,
            occurred_to=None,
            before=None,
            limit=50,
        )
        assert rows == []
        assert audit.verify_chain(s, world.b) == audit.VerifyResult(True, 0)
        s.rollback()
    with _as(app_db, world.b) as s:
        assert _ids(s) == {world.b_id}
        s.rollback()


def test_the_global_events_need_the_platform_administrator_context(
    app_db: Database, world: World
) -> None:
    with _as(app_db, world.a) as s:
        assert world.global_id not in _ids(s)
        set_platform_admin(s, True)
        assert world.global_id in _ids(s)
        assert world.b_id not in _ids(s)
        s.rollback()


def test_without_a_tenant_context_nothing_is_visible(app_db: Database, world: World) -> None:
    with _as(app_db, None) as s:
        assert _ids(s) == set()
        s.rollback()


@pytest.mark.parametrize("tenant", [":other", "NULL"], ids=["another-client", "global-row"])
def test_a_client_cannot_insert_for_another_client_or_a_global_row(
    app_db: Database, world: World, tenant: str
) -> None:
    """Fails on SETH's policy (`tenant_id = current OR tenant_id IS NULL`) for the global row."""
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            s.execute(text(INSERT.format(tenant=tenant)), {"other": world.b})
        assert _sqlstate(err) == "42501"  # row-level security
        s.rollback()


def test_a_client_can_insert_its_own_row_and_not_change_or_delete_any(
    app_db: Database, world: World
) -> None:
    with _as(app_db, world.a) as s:
        s.execute(text(INSERT.format(tenant=":me")), {"me": world.a})
        for sql in (
            "UPDATE shifde.audit_events SET action = 'x'",
            "DELETE FROM shifde.audit_events",
            "TRUNCATE shifde.audit_events",
        ):
            with pytest.raises(DBAPIError) as err, s.begin_nested():
                s.execute(text(sql))
            assert _sqlstate(err) == "42501", sql  # no privilege
        s.rollback()


def test_the_service_writes_a_global_row_only_for_an_installation_action(
    app_db: Database, world: World
) -> None:
    ctx = RequestContext(request_id="svc")
    # a pre-tenant action is filed globally only from a session with no client context (audit
    # review 2 item 3, B-912): the no-context write succeeds and leaves no admin flag behind
    with _as(app_db, None) as s:
        audit.record(s, ctx, action="identity.login.failed", tenant_id=None, outcome="failure")
        s.commit()
        assert s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one() is False
    with _as(app_db, world.a) as s:
        with pytest.raises(audit.NotInstallationAction):
            audit.record(s, ctx, action="identity.login.failed", tenant_id=None, outcome="failure")
        with pytest.raises(audit.NotInstallationAction):
            audit.record(s, ctx, action="test.forged", tenant_id=None)
        # the write left the context as it was: the client still cannot write a raw global row
        with pytest.raises(DBAPIError) as err, s.begin_nested():
            s.execute(text(INSERT.format(tenant="NULL")))
        assert _sqlstate(err) == "42501"
        s.rollback()


def test_the_service_cannot_write_for_another_client(app_db: Database, world: World) -> None:
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            audit.record(s, RequestContext(request_id="x"), action="test.x", tenant_id=world.b)
        assert _sqlstate(err) == "42501"
        s.rollback()


# --- isolation review 1 (fix round 1) ---------------------------------------------------------


def _assert_no_leaked_admin(s: Session) -> None:
    """After the failed write and a rollback: the Session and the next transaction hold no
    platform-administrator flag, and a raw global insert still fails."""
    s.rollback()
    assert get_context(s).platform_admin is False
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    assert s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one() is False
    with pytest.raises(DBAPIError) as err:
        s.execute(text(INSERT.format(tenant="NULL")))
    assert _sqlstate(err) == "42501"
    s.rollback()


@pytest.mark.parametrize("with_context", [True, False], ids=["client-context", "no-context"])
def test_a_failed_installation_write_in_an_aborted_transaction_leaks_no_admin_flag(
    app_db: Database, world: World, with_context: bool
) -> None:
    ctx = RequestContext(request_id="x")
    with _as(app_db, world.a if with_context else None) as s:
        with pytest.raises(DBAPIError):
            s.execute(text("SELECT 1/0"))
        with pytest.raises(DBAPIError):  # InFailedSqlTransaction
            audit.record(s, ctx, action="identity.login.failed", tenant_id=None)
        _assert_no_leaked_admin(s)


def test_a_failed_installation_write_after_a_failed_flush_leaks_no_admin_flag(
    app_db: Database, world: World
) -> None:
    with _as(app_db, world.a) as s:
        s.add(
            AuditEvent(
                id=uuid7(), tenant_id=world.a, occurred_at=utcnow(), actor_type="user",
                action="test.x", outcome="bogus",
            )
        )  # fmt: skip
        with pytest.raises(IntegrityError):
            s.flush()
        with pytest.raises(PendingRollbackError):
            audit.record(
                s, RequestContext(request_id="x"), action="identity.login.failed", tenant_id=None
            )
        _assert_no_leaked_admin(s)


def test_an_insert_that_fails_reports_its_own_error(app_db: Database, world: World) -> None:
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            audit.record(
                s, RequestContext(request_id="x"), action="test.x", outcome="bogus",
                tenant_id=world.a,
            )  # fmt: skip
        assert _sqlstate(err) == "23514"
        s.rollback()
    with _as(app_db, None) as s:  # pre-tenant actions need a session with no client (B-912)
        with pytest.raises(DBAPIError) as err:
            audit.record(
                s, RequestContext(request_id="x"), action="identity.login.failed",
                outcome="bogus", tenant_id=None,
            )  # fmt: skip
        assert _sqlstate(err) == "23514"  # not InFailedSqlTransaction from the restore
        _assert_no_leaked_admin(s)


@pytest.mark.parametrize("action", ["egress.default.updated", "installation.profile.applied"])
def test_installation_configuration_events_need_a_platform_administrator(
    app_db: Database, world: World, action: str
) -> None:
    ctx = RequestContext(request_id="x")
    with _as(app_db, world.a) as s:
        with pytest.raises(audit.NotInstallationAction):
            audit.record(s, ctx, action=action, tenant_id=None)
        s.rollback()
    with _as(app_db, world.a) as s:
        set_platform_admin(s, True)
        audit.record(s, ctx, action=action, tenant_id=None)
        assert get_context(s).platform_admin  # the administrator's own context survives
        s.rollback()
    with _as(app_db, None) as s:
        with pytest.raises(audit.NotInstallationAction):
            audit.record(s, ctx, action=action, tenant_id=None)
        s.rollback()


@pytest.mark.parametrize("with_context", [True, False], ids=["client-context", "no-context"])
def test_a_client_cannot_stall_another_clients_chain_before_being_refused(
    app_db: Database, owner_db: Database, world: World, with_context: bool
) -> None:
    """B's writer holds B's chain lock; A's attempt for B is refused by RLS at once (42501), not
    queued behind the lock (SQLSTATE 55P03 here would mean A reached the lock)."""
    with owner_db.session() as holder:
        repository.lock_chain(holder, world.b)
        with _as(app_db, world.a if with_context else None) as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            with pytest.raises(DBAPIError) as err:
                audit.record(s, RequestContext(request_id="x"), action="test.x", tenant_id=world.b)
            assert _sqlstate(err) == "42501"
            s.rollback()
        holder.rollback()


def test_a_rolled_back_savepoint_that_set_another_client_does_not_reach_its_chain_lock(
    app_db: Database, owner_db: Database, world: World
) -> None:
    """B-703: the lock decision reads the database, not `session.info`. A's session sets B inside
    a savepoint that is then rolled back; recording for B is refused at once (42501), not queued
    on B's chain lock (55P03 here would mean A reached the lock)."""
    with owner_db.session() as holder:
        repository.lock_chain(holder, world.b)
        with _as(app_db, world.a) as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            sp = s.begin_nested()
            set_tenant(s, world.b)
            sp.rollback()
            assert get_context(s).tenant_id == world.a
            with pytest.raises(DBAPIError) as err:
                audit.record(s, RequestContext(request_id="x"), action="test.x", tenant_id=world.b)
            assert _sqlstate(err) == "42501"
            s.rollback()
        holder.rollback()


def test_a_session_whose_context_lags_the_database_does_not_reach_the_chain_lock(
    app_db: Database, owner_db: Database, world: World
) -> None:
    """The lock decision does not trust `session.info` even if it names the target tenant: here
    the database says A while the Session's context says B."""
    with owner_db.session() as holder:
        repository.lock_chain(holder, world.b)
        with _as(app_db, world.a) as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            s.info["shifde_ctx"] = replace(get_context(s), tenant_id=world.b)
            with pytest.raises(DBAPIError) as err:
                audit.record(s, RequestContext(request_id="x"), action="test.x", tenant_id=world.b)
            assert _sqlstate(err) == "42501"
            s.rollback()
        holder.rollback()


@pytest.mark.parametrize(
    "action",
    sorted(audit.PRE_TENANT_ACTIONS),
)
def test_a_pre_tenant_action_is_refused_from_a_client_context(
    app_db: Database, world: World, action: str
) -> None:
    """Audit review 2 item 3 (carry_forward, B-912): code running in client A cannot file a
    global (tenant NULL) row under a pre-tenant action; nothing is written."""
    with _as(app_db, world.a) as s:
        before = _ids(s)
        with pytest.raises(audit.NotInstallationAction):
            audit.record(s, RequestContext(request_id="x"), action=action, tenant_id=None)
        assert get_context(s).platform_admin is False
        s.commit()
    with _as(app_db, None) as s:
        actor = None
        if action in audit.SELF_ACTIONS:  # a person's own event: the acting person (item 5)
            actor = uuid7()
            set_principal(s, actor)
        audit.record(
            s, RequestContext(request_id="x"), action=action, tenant_id=None, actor_id=actor
        )
        s.commit()
    with _as(app_db, world.a) as s:
        assert _ids(s) == before  # A sees only its own rows; the global row is not A's


@pytest.mark.parametrize("action", sorted(audit.SELF_ACTIONS))
@pytest.mark.parametrize("principal", [False, True])
def test_a_global_sign_in_event_names_only_the_acting_person(
    app_db: Database, action: str, principal: bool
) -> None:
    """Identity review 2 item 5: with no client context, a person's own sign-in event (login
    succeeded, logout, setup token redeemed) is filed globally only with
    `actor_id = current_principal_id()`: no actor, or anyone else, is refused and nothing is
    written."""
    someone_else = uuid7()
    with _as(app_db, None) as s:
        if principal:
            set_principal(s, uuid7())
        before = _ids(s)
        for actor in (None, someone_else):
            with pytest.raises(audit.NotInstallationAction):
                audit.record(
                    s, RequestContext(request_id="x"), action=action, tenant_id=None, actor_id=actor
                )
        s.commit()
        assert _ids(s) == before
