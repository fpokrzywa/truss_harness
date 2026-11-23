"""Issue #224 (B-3001): `shifde_app` may drop the queued request from an ended connection invoke
job's payload, and nothing else (`trg_jobs_payload_guard`, migration runtime_0007).

Accepted: `payload - 'request' - 'write'` on a `connection.invoke` job whose new status is `done`
or `dead` (also in the statement that ends it); ids and `request_hash` stay. Refused (23514), as
`shifde_app` and as the schema owner: the same scrub on a queued or leased job, on another kind,
any other change of a key, an added key, dropping only one of the two keys or `request_hash`.
Updates that leave `payload` alone behave as before.

Isolation: in client A's context an UPDATE naming B's ended invoke job touches zero rows and B's
payload stays byte-for-byte; A's own job is scrubbed in the same session (not vacuous).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.tenants import add_tenants
from tests.runtime.conftest import app_session, insert

INVOKE = "connection.invoke"
SCRUB = "UPDATE shifde.jobs SET payload = payload - 'request' - 'write' WHERE id = :j"


def _payload() -> dict[str, Any]:  # Any: JSON values
    return {
        "invocation_id": str(uuid7()),
        "connection_id": str(uuid7()),
        "request": {"method": "POST", "query": {"q": "secret"}, "body": {"b": 1},
                    "headers": {"X-Trace": "t-1"}},
        "write": True,
        "request_hash": "h-1",
        "principal_id": str(uuid7()),
        "principal_type": "user",
        "api_key_id": None,
    }  # fmt: skip


def _job(o: Session, tenant: UUID, status: str, kind: str = INVOKE) -> UUID:
    job = uuid7()
    insert(o, "jobs", id=job, tenant_id=tenant, kind=kind, status=status,
           payload=json.dumps(_payload()), dedupe_key=f"k-{job}")  # fmt: skip
    return job


def _read(owner_db: Database, job: UUID) -> Any:  # Any: the stored JSON object
    with owner_db.session() as o:
        return o.execute(text("SELECT payload FROM shifde.jobs WHERE id = :j"), {"j": job}).scalar()


def _touched(s: Session, sql: str, job: UUID) -> int:
    """How many rows `sql` updated (through RETURNING, so row-level security decides)."""
    return len(s.execute(text(f"{sql} RETURNING id"), {"j": job}).all())


def _openers(app_db: Database, owner_db: Database, tenant: UUID) -> list[Callable[[], Session]]:
    return [lambda: app_session(app_db, tenant), owner_db.session]


def test_the_app_role_holds_the_payload_grant_and_the_guard_is_an_invoker_function(
    owner_db: Database,
) -> None:
    with owner_db.session() as o:
        assert o.execute(
            text("SELECT has_column_privilege('shifde_app', 'shifde.jobs', 'payload', 'UPDATE')")
        ).scalar_one()
        row = o.execute(
            text(
                "SELECT p.prosecdef, has_function_privilege('public', p.oid, 'EXECUTE'), "
                "p.proconfig, pg_get_userbyid(p.proowner) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'shifde' AND p.proname = 'jobs_payload_guard'"
            )
        ).one()
        trigger = o.execute(
            text(
                "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t "
                "WHERE t.tgrelid = 'shifde.jobs'::regclass AND t.tgname = 'trg_jobs_payload_guard'"
            )
        ).scalar_one()
        rls = o.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'shifde.jobs'::regclass"
            )
        ).one()
    assert tuple(row) == (False, False, ["search_path=pg_catalog, pg_temp"], "shifde_owner")
    assert trigger == (
        "CREATE TRIGGER trg_jobs_payload_guard BEFORE UPDATE ON shifde.jobs "
        "FOR EACH ROW EXECUTE FUNCTION shifde.jobs_payload_guard()"
    )
    assert tuple(rls) == (True, True)


@pytest.mark.parametrize("status", ["done", "dead"])
def test_the_scrub_of_an_ended_invoke_job_is_accepted(
    app_db: Database, owner_db: Database, status: str
) -> None:
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        job = _job(o, tenant, status)
        o.commit()
    before = _read(owner_db, job)
    with app_session(app_db, tenant) as s:
        assert _touched(s, SCRUB, job) == 1
        s.commit()
    after = _read(owner_db, job)
    assert after == {k: v for k, v in before.items() if k not in ("request", "write")}
    assert after["request_hash"] == "h-1" and after["invocation_id"] == before["invocation_id"]


def test_the_scrub_is_accepted_in_the_statement_that_ends_the_job(
    app_db: Database, owner_db: Database
) -> None:
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        job = _job(o, tenant, "leased")
        o.commit()
    with app_session(app_db, tenant) as s:
        s.execute(
            text(
                "UPDATE shifde.jobs SET status = 'done', finished_at = now(), "
                "payload = payload - 'request' - 'write' WHERE id = :j"
            ),
            {"j": job},
        )
        s.commit()
    assert set(_read(owner_db, job)) == {"invocation_id", "connection_id", "request_hash",
                                         "principal_id", "principal_type", "api_key_id"}  # fmt: skip


def test_every_other_change_of_a_job_payload_is_refused(
    app_db: Database, owner_db: Database
) -> None:
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        queued, leased = _job(o, tenant, "queued"), _job(o, tenant, "leased")
        done = _job(o, tenant, "done")
        other_kind = _job(o, tenant, "done", kind="connection.test")
        o.commit()
    refused = [
        (SCRUB, queued),
        (SCRUB, leased),
        (SCRUB, other_kind),
        # the scrub while setting a non-terminal status on an ended job
        ("UPDATE shifde.jobs SET status = 'queued', payload = payload - 'request' - 'write' "
         "WHERE id = :j", done),
        # another key changes as well
        ("UPDATE shifde.jobs SET payload = jsonb_set(payload - 'request' - 'write', "
         "'{request_hash}', '\"h-2\"') WHERE id = :j", done),
        ("UPDATE shifde.jobs SET payload = jsonb_set(payload, '{request_hash}', '\"h-2\"') "
         "WHERE id = :j", done),
        # a key is added
        ("UPDATE shifde.jobs SET payload = (payload - 'request' - 'write') "
         "|| '{\"extra\": 1}' WHERE id = :j", done),
        ("UPDATE shifde.jobs SET payload = payload || '{\"extra\": 1}' WHERE id = :j", done),
        # only one of the two keys, another key, everything
        ("UPDATE shifde.jobs SET payload = payload - 'request' WHERE id = :j", done),
        ("UPDATE shifde.jobs SET payload = payload - 'write' WHERE id = :j", done),
        ("UPDATE shifde.jobs SET payload = payload - 'request' - 'write' - 'request_hash' "
         "WHERE id = :j", done),
        ("UPDATE shifde.jobs SET payload = '{}' WHERE id = :j", done),
        # the request replaced instead of dropped
        ("UPDATE shifde.jobs SET payload = jsonb_set(payload, '{request}', '{}') WHERE id = :j",
         done),
    ]  # fmt: skip
    before = {j: _read(owner_db, j) for j in (queued, leased, done, other_kind)}
    for opener in _openers(app_db, owner_db, tenant):
        for sql, job in refused:
            with opener() as s:
                with pytest.raises(DBAPIError) as err:
                    s.execute(text(sql), {"j": job})
                assert getattr(err.value.orig, "sqlstate", None) == "23514", sql
    assert {j: _read(owner_db, j) for j in before} == before


def test_updates_that_leave_the_payload_alone_behave_as_before(
    app_db: Database, owner_db: Database
) -> None:
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        invoke, other = _job(o, tenant, "queued"), _job(o, tenant, "queued", kind="run.advance")
        o.commit()
    before = {j: _read(owner_db, j) for j in (invoke, other)}
    with app_session(app_db, tenant) as s:
        for job in (invoke, other):
            s.execute(
                text(
                    "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w', attempts = 1 "
                    "WHERE id = :j"
                ),
                {"j": job},
            )
            s.execute(text("UPDATE shifde.jobs SET payload = payload WHERE id = :j"), {"j": job})
            s.execute(
                text("UPDATE shifde.jobs SET status = 'done', finished_at = now() WHERE id = :j"),
                {"j": job},
            )
        s.commit()
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT id, status, payload FROM shifde.jobs WHERE id IN (:a, :b)"),
            {"a": invoke, "b": other},
        ).all()
    assert {r[0]: (r[1], r[2]) for r in rows} == {j: ("done", before[j]) for j in before}


def test_a_client_cannot_scrub_another_clients_job(app_db: Database, owner_db: Database) -> None:
    a, b = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, a, b)
        job_a, job_b = _job(o, a, "done"), _job(o, b, "done")
        o.commit()
    before_b = _read(owner_db, job_b)
    with app_session(app_db, a) as s:
        assert _touched(s, SCRUB, job_b) == 0
        assert _touched(s, "UPDATE shifde.jobs SET payload = '{}' WHERE id = :j", job_b) == 0
        assert (
            s.execute(
                text("SELECT count(*) FROM shifde.jobs WHERE id = :j"), {"j": job_b}
            ).scalar_one()
            == 0
        )
        assert _touched(s, SCRUB, job_a) == 1
        s.commit()
    assert _read(owner_db, job_b) == before_b
    assert "request" in before_b and "request" not in _read(owner_db, job_a)
    with app_session(app_db, None) as s:  # no client context: no rows at all
        assert _touched(s, SCRUB, job_b) == 0
        s.commit()
    assert _read(owner_db, job_b) == before_b
