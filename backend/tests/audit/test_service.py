"""Audit service on the worker database, as the schema owner: hash chain, append-only trigger,
chain_seq uniqueness, redaction, listing, and the installation-level (NULL tenant) rule.

SETH tests this through the app (`test_p2_audit.py`, `test_p8_audit.py`); those move with the
modules that own the routes (docs/build/carry_forward.md). These cover the same behaviour
directly."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from shifde.audit import repository
from shifde.audit import service as audit
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database, get_context, set_platform_admin, set_tenant
from shifde.core.errors import ValidationFailed
from shifde.core.ids import uuid7

CTX = RequestContext(request_id="req-1", client_ip="203.0.113.9")


def _record(
    s: Session,
    tenant: UUID | None,
    action: str = "test.event",
    *,
    outcome: str = "success",
    actor_label: str | None = None,
    workspace_id: UUID | None = None,
    details: dict[str, Any] | None = None,  # Any: free-form audit details
) -> None:
    audit.record(
        s,
        CTX,
        action=action,
        outcome=outcome,
        tenant_id=tenant,
        actor_label=actor_label,
        workspace_id=workspace_id,
        details=details,
    )


def _seed(s: Session, tenant: UUID, n: int) -> None:
    for i in range(n):
        _record(s, tenant, f"test.e{i}", details={"i": i})
    s.flush()


def test_events_form_a_hash_chain_that_verifies(owner_session: Session) -> None:
    t = uuid7()
    _seed(owner_session, t, 5)
    rows = owner_session.execute(
        text(
            "SELECT chain_seq, prev_hash, hash FROM shifde.audit_events "
            "WHERE tenant_id = :t ORDER BY chain_seq"
        ),
        {"t": t},
    ).all()
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0][1] is None
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert bytes(cur[1]) == bytes(prev[2]) and len(bytes(cur[2])) == 32
    assert audit.verify_chain(owner_session, t) == audit.VerifyResult(True, 5)


def test_chains_are_per_tenant(owner_session: Session) -> None:
    a, b = uuid7(), uuid7()
    _seed(owner_session, a, 3)
    _seed(owner_session, b, 2)
    assert audit.verify_chain(owner_session, a).checked == 3
    assert audit.verify_chain(owner_session, b).checked == 2
    first = owner_session.execute(
        text("SELECT prev_hash FROM shifde.audit_events WHERE tenant_id = :t AND chain_seq = 1"),
        {"t": b},
    ).scalar_one()
    assert first is None  # B's chain does not continue A's


def test_hash_is_sha256_of_prev_hash_and_canonical_event(owner_session: Session) -> None:
    t = uuid7()
    _seed(owner_session, t, 2)
    rows = list(repository.iter_chain(owner_session, t))
    columns = [
        "id",
        "tenant_id",
        "occurred_at",
        "actor_type",
        "actor_id",
        "actor_ip",
        "auth_method",
        "action",
        "target_type",
        "target_id",
        "workspace_id",
        "project_id",
        "outcome",
        "details",
        "trace_id",
        "correlation_id",
        "chain_seq",
    ]
    fields = {c: getattr(rows[1], c) for c in columns}
    assert rows[1].hash is not None and rows[0].hash is not None
    assert bytes(rows[1].hash) == audit.event_hash(bytes(rows[0].hash), fields)


def test_the_database_refuses_update_and_delete_of_an_event(owner_session: Session) -> None:
    t = uuid7()
    _seed(owner_session, t, 1)
    for sql in (
        "UPDATE shifde.audit_events SET action = 'forged' WHERE tenant_id = :t",
        "DELETE FROM shifde.audit_events WHERE tenant_id = :t",
    ):
        with pytest.raises(DBAPIError), owner_session.begin_nested():
            owner_session.execute(text(sql), {"t": t})
    assert audit.verify_chain(owner_session, t).ok


def test_tampering_is_detected_at_the_changed_event(owner_session: Session) -> None:
    t = uuid7()
    _seed(owner_session, t, 4)
    victim = owner_session.execute(
        text("SELECT id FROM shifde.audit_events WHERE tenant_id = :t AND chain_seq = 3"),
        {"t": t},
    ).scalar_one()
    owner_session.execute(text("ALTER TABLE shifde.audit_events DISABLE TRIGGER USER"))
    owner_session.execute(
        text("UPDATE shifde.audit_events SET details = details || '{\"x\": 1}' WHERE id = :i"),
        {"i": victim},
    )
    assert audit.verify_chain(owner_session, t) == audit.VerifyResult(False, 2, victim)
    owner_session.rollback()  # the DDL and the forgery vanish with the transaction


def test_a_deleted_middle_event_is_detected_at_its_successor(owner_session: Session) -> None:
    t = uuid7()
    _seed(owner_session, t, 5)
    ids = (
        owner_session.execute(
            text("SELECT id FROM shifde.audit_events WHERE tenant_id = :t ORDER BY chain_seq"),
            {"t": t},
        )
        .scalars()
        .all()
    )
    owner_session.execute(text("ALTER TABLE shifde.audit_events DISABLE TRIGGER USER"))
    owner_session.execute(text("DELETE FROM shifde.audit_events WHERE id = :i"), {"i": ids[2]})
    result = audit.verify_chain(owner_session, t)
    assert not result.ok and result.first_broken_id == ids[3]
    owner_session.rollback()


def test_chain_seq_is_unique_per_tenant(owner_session: Session) -> None:
    t = uuid7()
    _seed(owner_session, t, 1)
    sql = (
        "INSERT INTO shifde.audit_events (id, tenant_id, actor_type, action, outcome, chain_seq) "
        "VALUES (gen_random_uuid(), :t, 'system', 'x', 'success', {seq})"
    )
    with pytest.raises(IntegrityError), owner_session.begin_nested():
        owner_session.execute(text(sql.format(seq="1")), {"t": t})
    # another tenant may use sequence 1; so may any number of unchained rows
    owner_session.execute(text(sql.format(seq="1")), {"t": uuid7()})
    owner_session.execute(text(sql.format(seq="NULL")), {"t": t})
    owner_session.execute(text(sql.format(seq="NULL")), {"t": t})


def test_concurrent_appends_keep_one_linear_chain(owner_url: str) -> None:
    t = uuid7()
    db = Database(owner_url, pool_size=8, max_overflow=0, verify_role=False)  # the schema owner

    def work(i: int) -> None:
        with db.session() as s:
            audit.record(
                s,
                RequestContext(request_id=f"req-{i:04d}"),
                action="test.concurrent",
                tenant_id=t,
                details={"i": i},
            )
            s.commit()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(40)))
        with db.session() as s:
            assert audit.verify_chain(s, t) == audit.VerifyResult(True, 40)
            seqs = s.execute(
                text("SELECT chain_seq FROM shifde.audit_events ORDER BY chain_seq")
            ).scalars()
            assert list(seqs) == list(range(1, 41))
    finally:
        db.dispose()


def test_details_are_scrubbed_before_storage(owner_session: Session) -> None:
    _record(
        owner_session,
        uuid7(),
        "test.scrub",
        details={
            "password": "hunter2",
            "api_key": "k",
            "key_id": "ok-to-keep",
            "note": "header was Bearer abcdefghijklmnop",
            "nested": {"client_secret": "zzz"},
        },
    )
    d = owner_session.execute(
        text("SELECT details FROM shifde.audit_events WHERE action = 'test.scrub'")
    ).scalar_one()
    assert d["password"] == "[REDACTED]" and d["api_key"] == "[REDACTED]"
    assert d["key_id"] == "ok-to-keep"
    assert "abcdefghijklmnop" not in d["note"] and d["nested"]["client_secret"] == "[REDACTED]"


def test_the_actor_is_recorded_and_a_service_account_is_told_apart(owner_session: Session) -> None:
    t, uid = uuid7(), uuid7()
    p = Principal(user_id=uid, email="sa@x", display_name="sa", principal_type="service_account")
    audit.record(
        owner_session,
        RequestContext(request_id="r", principal=p, client_ip="10.0.0.1"),
        action="test.actor",
        tenant_id=t,
        target_type="thing",
        target_id=uid,
        target_label="the thing",
    )
    row = owner_session.execute(
        text("SELECT actor_type, actor_id, actor_ip, details FROM shifde.audit_events")
    ).one()
    assert (row[0], row[1], row[2]) == ("service_account", uid, "10.0.0.1")
    assert row[3] == {"actor": "sa@x", "target": "the thing"}


def test_installation_events_are_unchained_and_limited_to_the_closed_list(
    owner_session: Session,
) -> None:
    _record(owner_session, None, "identity.login.failed", outcome="failure")
    seq = owner_session.execute(
        text("SELECT chain_seq FROM shifde.audit_events WHERE tenant_id IS NULL")
    ).scalar_one()
    assert seq is None
    with pytest.raises(audit.NotInstallationAction):
        _record(owner_session, None, "test.install")


def test_the_installation_write_restores_the_callers_admin_flag(app_db: Database) -> None:
    with app_db.session() as s:
        assert not get_context(s).platform_admin
        _record(s, None, "identity.login.failed", outcome="failure")
        assert not get_context(s).platform_admin
        flag = s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one()
        assert flag is False
        s.rollback()
    with app_db.session() as s:
        set_platform_admin(s, True)
        _record(s, None, "egress.default.updated")
        assert get_context(s).platform_admin  # an outer administrator context survives
        s.rollback()


@pytest.mark.parametrize("action", ["egress.default.updated", "identity.group.updated"])
def test_an_installation_configuration_event_is_refused_while_a_client_is_set(
    app_db: Database, action: str
) -> None:
    """Admin isolation review 1 item 3, vendors review 1 follow-up 2 (B-1409): the flag alone is
    not enough; with a client context set, read from the database, no NULL-tenant row."""
    assert action in audit.ADMIN_INSTALLATION_ACTIONS
    with app_db.session() as s:
        set_platform_admin(s, True)
        set_tenant(s, uuid7())
        with pytest.raises(audit.NotInstallationAction):
            _record(s, None, action)
        s.rollback()
    with app_db.session() as s:  # the same call with no client is accepted
        set_platform_admin(s, True)
        _record(s, None, action)
        s.rollback()


def _listed(s: Session, t: UUID, **kw: Any) -> list[audit.EventRow]:  # Any: filter values
    args: dict[str, Any] = {
        "category": None,
        "actor": None,
        "action": None,
        "occurred_from": None,
        "occurred_to": None,
        "before": None,
        "limit": 50,
    }
    args.update(kw)
    return audit.list_events(s, tenant_id=t, **args)


def test_listing_filters_and_pages_newest_first(owner_session: Session) -> None:
    t, other = uuid7(), uuid7()
    _record(owner_session, t, "identity.login.succeeded", actor_label="Ann Admin")
    _record(owner_session, t, "tenancy.workspace.created")
    _record(owner_session, t, "run.started")
    _record(owner_session, other, "run.started")
    all_rows = _listed(owner_session, t)
    ids = [r.id for r in all_rows]
    assert len(ids) == 3 and ids == sorted(ids, reverse=True)
    assert [r.action for r in _listed(owner_session, t, category="security")] == [
        "identity.login.succeeded"
    ]
    assert [r.action for r in _listed(owner_session, t, action="run.started")] == ["run.started"]
    assert len(_listed(owner_session, t, actor="ann admin")) == 1
    assert _listed(owner_session, t, actor="%") == []  # LIKE wildcards are escaped
    page = _listed(owner_session, t, limit=2)
    rest = _listed(owner_session, t, before=page[-1].id)
    assert len(page) == 2 and len(rest) == 1 and rest[0].id < page[-1].id
    soon = utcnow() + timedelta(hours=1)
    assert _listed(owner_session, t, occurred_from=soon) == []
    assert len(_listed(owner_session, t, occurred_to=soon)) == 3
    with pytest.raises(ValidationFailed):
        _listed(owner_session, t, category="nope")


def test_scoped_listing_and_recent_activity(owner_session: Session) -> None:
    t, w1, w2 = uuid7(), uuid7(), uuid7()
    _record(owner_session, t, "agent.created", workspace_id=w1, actor_label="a")
    _record(owner_session, t, "agent.created", workspace_id=w2, actor_label="b")
    _record(owner_session, t, "tenancy.org.updated")
    assert len(_listed(owner_session, t, workspace_ids={w1})) == 1
    assert _listed(owner_session, t, workspace_ids=set(), project_ids=set()) == []
    items = audit.recent_activity(
        owner_session, tenant_id=t, tenant_wide=False, visible_workspace_ids={w2}
    )
    assert [i.actor for i in items] == ["b"]
    wide = audit.recent_activity(
        owner_session, tenant_id=t, tenant_wide=True, visible_workspace_ids=set()
    )
    assert len(wide) == 3
