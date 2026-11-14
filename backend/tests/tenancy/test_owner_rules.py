"""Tenancy isolation review 2 follow-ups (tenancy_0003, DECISIONS B-1015..B-1018), as `shifde_app`
unless a test says otherwise.

* item 1: the group-binding check fails closed with no client context;
* item 2: only the owner or the platform administrator names an owner;
* item 3: a platform administrator adding themselves through a group is a self-add;
* item 4: the PDP grants nothing through a group once the membership is not active;
* item 5: a failure after the client context was set leaves neither the client nor the flag.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NoReturn
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.core import db
from shifde.core.db import Database
from shifde.core.errors import Forbidden
from shifde.core.ids import uuid7
from shifde.tenancy import service
from shifde.tenancy.schemas import OrgCreate
from tests.tenancy.conftest import World, app_session, owner_member, owner_person
from tests.tenancy.test_service import (
    CTX,
    _admin,
    _bind_group,
    _events,
    _owner_of,
    _p,
    _role_id,
    _scope,
    _visible,
)

SELF_ADDED = "tenancy.platform_admin.self_added"
_INSERT_BINDING = text(
    "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
    "scope_type, scope_id) VALUES (:i, :t, :st, :s, :r, 'tenant', :t)"
)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


def _owner_principal(s: Session, principal: UUID | None) -> None:
    s.execute(text("SELECT set_config('app.principal_id', :p, true)"), {"p": str(principal or "")})


# ------------------------------------------------------------------ item 1
def test_the_group_binding_check_fails_closed_without_a_client_context(
    world: World, app_db: Database, owner_db: Database, owner_session: Session
) -> None:
    _owner_of(owner_session, world.a.tenant, world.a.member)
    viewer = _role_id(owner_session, "viewer")
    # the owner, in their own client: true (the positive case)
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        assert authz_may_bind(s, world.a.tenant, world.b.group) is True
    # no client context: false, not NULL
    with app_session(app_db, None, world.a.member) as s:
        assert authz_may_bind(s, world.a.tenant, world.b.group) is False
    # the schema owner (no RLS), principal A's owner, no client context: the trigger refuses
    with owner_db.session() as o:
        _owner_principal(o, world.a.member)
        with pytest.raises(DBAPIError, match="needs the platform administrator"):
            o.execute(
                _INSERT_BINDING,
                {"i": uuid7(), "t": world.a.tenant, "st": "group", "s": world.b.group, "r": viewer},
            )
    assert _visible(app_db, world.b.staff) == set()


def authz_may_bind(s: Session, tenant: UUID, group: UUID) -> object:
    return s.execute(
        text("SELECT shifde.shifde_may_bind_group(:t, :g)"), {"t": tenant, "g": group}
    ).scalar_one()


# ------------------------------------------------------------------ item 2
def _grant_owner(s: Session, tenant: UUID, user: UUID, actor: UUID) -> None:
    authz.grant_role(
        s,
        tenant_id=tenant,
        user_id=user,
        role_key="tenant_administrator",
        scope_type="tenant",
        scope_id=tenant,
        created_by=actor,
    )


def test_only_the_owner_or_the_platform_administrator_names_an_owner(
    world: World, app_db: Database, owner_db: Database, owner_session: Session
) -> None:
    a = world.a
    _owner_of(owner_session, a.tenant, a.member)
    viewer_member = owner_person(owner_session, "viewer-a")
    owner_member(owner_session, a.tenant, viewer_member)
    owner_session.commit()
    _bind_group(owner_session, a.tenant, a.group, "tenant_administrator")  # staff: group-held
    ta = _role_id(owner_session, "tenant_administrator")
    refused = [
        a.staff,  # tenant_administrator through a group (role:manage, not the owner)
        viewer_member,  # a member naming themselves
        world.b.member,  # another client's person
    ]
    for actor in refused:
        with app_session(app_db, a.tenant, actor) as s:
            with pytest.raises(Forbidden):
                _grant_owner(s, a.tenant, viewer_member, actor)
            s.rollback()
            with pytest.raises(DBAPIError, match="naming an owner needs"):
                s.execute(
                    _INSERT_BINDING,
                    {"i": uuid7(), "t": a.tenant, "st": "user", "s": viewer_member, "r": ta},
                )
    with owner_db.session() as o, pytest.raises(DBAPIError, match="naming an owner needs"):
        o.execute(  # the schema owner with no context either
            _INSERT_BINDING,
            {"i": uuid7(), "t": a.tenant, "st": "user", "s": viewer_member, "r": ta},
        )
    # A's owner cannot name an owner in B, from B's context or A's
    with app_session(app_db, world.b.tenant, a.member) as s, pytest.raises(Forbidden):
        _grant_owner(s, world.b.tenant, world.b.member, a.member)
    with app_session(app_db, a.tenant, a.member) as s, pytest.raises(DBAPIError):
        s.execute(
            _INSERT_BINDING,
            {"i": uuid7(), "t": world.b.tenant, "st": "user", "s": world.b.member, "r": ta},
        )
    with app_session(app_db, a.tenant, viewer_member) as s:
        assert authz_may_bind(s, a.tenant, world.b.group) is False
    # the owner may
    with app_session(app_db, a.tenant, a.member) as s:
        _grant_owner(s, a.tenant, viewer_member, a.member)
        s.commit()
    with app_session(app_db, a.tenant, viewer_member) as s:
        assert authz_may_bind(s, a.tenant, world.b.group) is True


# ------------------------------------------------------------------ item 3
def _join_group(owner: Session, group: UUID, user: UUID) -> None:
    owner.execute(
        text("INSERT INTO shifde.group_members (group_id, user_id) VALUES (:g, :u)"),
        {"g": group, "u": user},
    )
    owner.commit()


def test_a_platform_administrator_binding_their_own_group_is_a_self_add(
    world: World, app_db: Database, owner_session: Session
) -> None:
    admin = _admin(owner_session)
    _join_group(owner_session, world.a.group, admin)
    viewer = _role_id(owner_session, "viewer")
    with app_session(app_db, None, admin) as s:
        for tenant, group in ((world.b.tenant, world.b.group), (world.a.tenant, world.a.group)):
            service.bind_group_as_platform_admin(
                s,
                CTX,
                _p(admin, admin=True),
                tenant_id=tenant,
                group_id=group,
                role_id=viewer,
                scope_type="tenant",
                scope_id=tenant,
            )
    # B: not in B's group, no self-add; A: in the group, a self-add in A only
    assert _events(owner_session, world.b.tenant, SELF_ADDED) == []
    [event] = _events(owner_session, world.a.tenant, SELF_ADDED)
    assert event["through_group_id"] == str(world.a.group)
    assert event["by_platform_administrator"] is True
    assert str(world.b.tenant) not in str(event)
    with app_session(app_db, world.a.tenant, world.a.member) as s:  # A's owner can read it
        n = s.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = :a"), {"a": SELF_ADDED}
        ).scalar_one()
    assert n == 1


def test_a_self_added_administrator_binding_as_owner_is_shown_as_the_administrator(
    world: World, app_db: Database, owner_session: Session
) -> None:
    admin = _admin(owner_session)
    viewer = _role_id(owner_session, "viewer")
    with app_session(app_db, None, admin) as s:
        service.add_platform_admin_to_client(s, CTX, _p(admin, admin=True), world.a.tenant)
    with app_session(app_db, world.a.tenant, admin) as s:  # now an owner of A, acting as one
        service.bind_group(
            _scope(s, world.a.tenant, _p(admin)),
            group_id=world.a.group,
            role_id=viewer,
            scope_type="tenant",
            scope_id=world.a.tenant,
        )
    [bound] = _events(owner_session, world.a.tenant, "authz.group.bound")
    assert bound["by_platform_administrator"] is True
    events = _events(owner_session, world.a.tenant, SELF_ADDED)
    assert len(events) == 2
    [via_group] = [e for e in events if "through_group_id" in e]
    assert via_group["through_group_id"] == str(world.a.group)
    assert _events(owner_session, world.b.tenant, SELF_ADDED) == []
    # an owner who is not a platform administrator: the binding only, as theirs
    _owner_of(owner_session, world.b.tenant, world.b.member)
    with app_session(app_db, world.b.tenant, world.b.member) as s:
        service.bind_group(
            _scope(s, world.b.tenant, _p(world.b.member)),
            group_id=world.b.group,
            role_id=viewer,
            scope_type="tenant",
            scope_id=world.b.tenant,
        )
    [bound_b] = _events(owner_session, world.b.tenant, "authz.group.bound")
    assert bound_b["by_platform_administrator"] is False
    assert _events(owner_session, world.b.tenant, SELF_ADDED) == []


# ------------------------------------------------------------------ item 4
def test_the_pdp_grants_nothing_through_a_group_once_the_membership_is_suspended(
    world: World, app_db: Database, owner_session: Session
) -> None:
    a = world.a
    _bind_group(owner_session, a.tenant, a.group, "viewer")
    ws = ResourceRef(a.tenant, workspace_id=a.ws)

    def allowed() -> bool:
        with app_session(app_db, a.tenant, a.staff) as s:
            return authz.new_pdp(s, _p(a.staff), a.tenant).authorize("workspace:read", ws).allowed

    assert allowed() is True  # group-only staff (no membership)
    owner_member(owner_session, a.tenant, a.staff, status="suspended")
    owner_session.commit()
    assert allowed() is False
    assert _visible(app_db, a.staff) == set()
    owner_session.execute(
        text("UPDATE shifde.memberships SET status = 'active' WHERE user_id = :u"), {"u": a.staff}
    )
    owner_session.commit()
    assert allowed() is True


# ------------------------------------------------------------------ item 5
def _boom(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("injected failure after the client context was set")


def _assert_no_context(s: Session) -> None:
    ctx = db.get_context(s)
    assert ctx.tenant_id is None and not ctx.platform_admin
    row = s.execute(text("SELECT shifde.current_tenant_id(), shifde.is_platform_admin_ctx()")).one()
    assert row[0] is None and row[1] is False


def test_failures_after_the_client_context_is_set_clear_it(
    world: World,
    app_db: Database,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _admin(owner_session)
    admin_email = owner_session.execute(
        text("SELECT primary_email FROM shifde.users WHERE id = :u"), {"u": world.a.member}
    ).scalar_one()
    viewer = _role_id(owner_session, "viewer")
    pa = _p(admin, admin=True)
    slug = f"f-{uuid7().hex[-12:]}"
    with app_session(app_db, None, admin) as s:
        # create_org: fails in the owner's binding, after set_tenant(new client)
        with monkeypatch.context() as m:
            m.setattr(service.authz, "grant_role", _boom)
            with pytest.raises(RuntimeError):
                service.create_org(
                    s, CTX, pa, OrgCreate(slug=slug, name="F", admin_email=admin_email)
                )
        _assert_no_context(s)
        # the self-add and the admin's group binding: fail in the audit write, after set_tenant
        with monkeypatch.context() as m:
            m.setattr(service.audit, "record", _boom)
            with pytest.raises(RuntimeError):
                service.add_platform_admin_to_client(s, CTX, pa, world.a.tenant)
            _assert_no_context(s)
            with pytest.raises(RuntimeError):
                service.bind_group_as_platform_admin(
                    s,
                    CTX,
                    pa,
                    tenant_id=world.a.tenant,
                    group_id=world.a.group,
                    role_id=viewer,
                    scope_type="tenant",
                    scope_id=world.a.tenant,
                )
            _assert_no_context(s)
    # nothing was written
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.tenants WHERE slug = :s"), {"s": slug}
        ).scalar_one()
        == 0
    )
    assert _visible(app_db, admin) == set()
    assert _visible(app_db, world.a.staff) == set()


# ------------------------------------------------------------------ admin isolation review 1
def _self_adds(owner: Session, tenant: UUID) -> list[tuple[UUID, dict[str, object]]]:
    rows = owner.execute(
        text(
            "SELECT target_id, details FROM shifde.audit_events "
            "WHERE tenant_id = :t AND action = :a"
        ),
        {"t": tenant, "a": SELF_ADDED},
    )
    return [(UUID(r[0]), dict(r[1] or {})) for r in rows]


def test_an_owner_binding_a_group_records_a_self_add_for_each_administrator_in_it(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Finding 1 (a), owner path: administrators who joined the group before it was bound reach
    A through the owner's binding; A's log shows each of them, B's shows nothing (B-1407)."""
    first, second = _admin(owner_session), _admin(owner_session)
    for admin in (first, second):
        _join_group(owner_session, world.a.group, admin)
    _owner_of(owner_session, world.a.tenant, world.a.member)
    viewer = _role_id(owner_session, "viewer")
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        service.bind_group(
            _scope(s, world.a.tenant, _p(world.a.member)),
            group_id=world.a.group,
            role_id=viewer,
            scope_type="tenant",
            scope_id=world.a.tenant,
        )
    recorded = _self_adds(owner_session, world.a.tenant)
    assert sorted(t for t, _ in recorded) == sorted([first, second])
    for _, details in recorded:
        assert details["through_group_id"] == str(world.a.group)
        assert details["group_membership"] == "member"
        assert details["by_platform_administrator"] is False
        assert str(world.b.tenant) not in str(details) and world.b.slug not in str(details)
    assert _self_adds(owner_session, world.b.tenant) == []
    with app_session(app_db, world.a.tenant, world.a.member) as s:  # A's owner reads them
        n = s.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = :a"), {"a": SELF_ADDED}
        ).scalar_one()
    assert n == 2


def test_a_platform_administrator_binding_records_every_administrator_in_the_group(
    world: World, app_db: Database, owner_session: Session
) -> None:
    """Finding 1 (a), platform-administrator path: the caller and another administrator in the
    group are each a self-add in A only (B-1407)."""
    caller, other = _admin(owner_session), _admin(owner_session)
    for admin in (caller, other):
        _join_group(owner_session, world.a.group, admin)
    viewer = _role_id(owner_session, "viewer")
    with app_session(app_db, None, caller) as s:
        service.bind_group_as_platform_admin(
            s,
            CTX,
            _p(caller, admin=True),
            tenant_id=world.a.tenant,
            group_id=world.a.group,
            role_id=viewer,
            scope_type="tenant",
            scope_id=world.a.tenant,
        )
    recorded = _self_adds(owner_session, world.a.tenant)
    assert sorted(t for t, _ in recorded) == sorted([caller, other])
    assert all(d["by_platform_administrator"] is True for _, d in recorded)
    assert all(str(world.b.tenant) not in str(d) for _, d in recorded)
    assert _self_adds(owner_session, world.b.tenant) == []


def test_the_group_administrator_definer_answers_only_for_a_group_bound_in_the_current_client(
    world: World, app_db: Database, owner_session: Session
) -> None:
    admin = _admin(owner_session)
    _join_group(owner_session, world.b.group, admin)
    _bind_group(owner_session, world.b.tenant, world.b.group)
    owner_session.commit()
    sql = text("SELECT shifde.shifde_bound_group_admin_ids(:g)")
    with app_session(app_db, world.a.tenant, world.a.member) as s:  # B's group, bound in B only
        assert s.execute(sql, {"g": world.b.group}).scalars().all() == []
    with app_session(app_db, None, admin) as s:  # no client
        assert s.execute(sql, {"g": world.b.group}).scalars().all() == []
    with app_session(app_db, world.b.tenant, world.b.member) as s:
        assert s.execute(sql, {"g": world.b.group}).scalars().all() == [admin]


def test_listing_the_clients_of_a_group_keeps_the_original_error_on_an_aborted_transaction(
    world: World, app_db: Database, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1 item 4 (B-1410): clearing the client after a failed statement must not raise
    `InFailedSqlTransaction` over the error that aborted the transaction."""
    admin = _admin(owner_session)

    def failing(session: Session) -> NoReturn:
        session.execute(text("SELECT 1 / 0"))
        raise AssertionError("unreachable")

    monkeypatch.setattr(authz, "bound_groups", failing)
    with app_session(app_db, None, admin) as s:
        with pytest.raises(DBAPIError) as raised, db.platform_admin_context(s):
            service.clients_binding_group(s, world.a.group)
        assert isinstance(raised.value.orig, psycopg.errors.DivisionByZero)
        assert db.get_context(s).tenant_id is None
        s.rollback()
        assert s.execute(text("SELECT shifde.current_tenant_id()")).scalar_one() is None
