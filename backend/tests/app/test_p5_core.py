"""P5 building blocks: state machine (also in the database), safe expressions, tool-call parsing,
body validation, reaper, queue isolation and the carried egress / SQL timeout items."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core import db
from shifde.core.db import Database
from shifde.runtime import state
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, ADMIN_B, A, activate, connection, pg_body, secret
from tests._support.p4util import art, publish, tenant_scope
from tests._support.p5util import Stack, start_run


# ================================================================== state machine
def test_allowed_transitions_match_the_database_table(owner_session: Session) -> None:
    table = {
        (a, b)
        for a, b in owner_session.execute(
            text("SELECT from_status, to_status FROM shifde.run_state_transitions")
        )
    }
    code = {(a, b) for a, targets in state.ALLOWED.items() for b in targets}
    assert table == code
    assert set(state.ALLOWED) == set(state.STATUSES)


def _set(owner: Session, run_id: str, status: str, bump: bool = True) -> None:
    owner.execute(
        text(
            "UPDATE shifde.runs SET status = :s"
            + (", state_version = state_version + 1" if bump else "")
            + " WHERE id = :r"
        ),
        {"s": status, "r": run_id},
    )


def test_database_rejects_illegal_transitions_and_terminal_changes(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    run = start_run(client, h, stack.agent["id"], "x")
    rid = run["id"]
    with pytest.raises(DBAPIError, match="illegal run transition"):
        _set(owner_session, rid, "completed")  # pending -> completed
    owner_session.rollback()
    with pytest.raises(DBAPIError, match="advance state_version"):
        _set(owner_session, rid, "running", bump=False)
    owner_session.rollback()
    _set(owner_session, rid, "running")
    _set(owner_session, rid, "completed")
    owner_session.commit()
    for stmt in (
        "UPDATE shifde.runs SET status = 'running', state_version = state_version + 1",
        "UPDATE shifde.runs SET error_message = 'tamper'",
    ):
        with pytest.raises(DBAPIError, match="terminal"):
            owner_session.execute(text(stmt + " WHERE id = :r"), {"r": rid})
        owner_session.rollback()
    # run events and usage rows are append-only
    with pytest.raises(DBAPIError):
        owner_session.execute(text("UPDATE shifde.run_events SET type = 'x'"))
    owner_session.rollback()


def test_app_role_cannot_bypass_the_guard_or_touch_foreign_tenant_jobs(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    start_run(client, h, stack.agent["id"], "x")
    tenant_b = owner_session.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = 'demo-b'")
    ).scalar_one()
    with app_db.session() as s:
        db.set_tenant(s, tenant_b)
        assert s.execute(text("SELECT count(*) FROM shifde.jobs")).scalar_one() == 0
        assert s.execute(text("SELECT count(*) FROM shifde.runs")).scalar_one() == 0
        # unset context sees nothing either (fail closed)
        db.set_tenant(s, None)
        assert s.execute(text("SELECT count(*) FROM shifde.jobs")).scalar_one() == 0
        # the claim function exposes ids only
        cols = s.execute(text("SELECT * FROM shifde.shifde_claim_jobs('w', 1, 30, 0)")).keys()
        assert set(cols) == {"job_id", "tenant_id", "kind", "run_id"}
        s.rollback()


# ================================================================== reaper
def _running_run_with_job(
    client: TestClient, world: World, owner: Session, attempts: int, max_attempts: int = 3
) -> str:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner, script=[])
    run = start_run(client, h, stack.agent["id"], "x")
    rid = run["id"]
    _set(owner, rid, "running")
    owner.execute(
        text("UPDATE shifde.runs SET lease_token = gen_random_uuid() WHERE id = :r"), {"r": rid}
    )
    owner.execute(
        text(
            "UPDATE shifde.runs SET lease_job_id = (SELECT id FROM shifde.jobs WHERE run_id = :r) "
            "WHERE id = :r"
        ),
        {"r": rid},
    )
    owner.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'dead', attempts = :a, "
            "max_attempts = :m, lease_expires_at = now() - interval '1 second' WHERE run_id = :r"
        ),  # the run's own job (the stack's connection test is a job too, ARCH M2)
        {"a": attempts, "m": max_attempts, "r": rid},
    )
    owner.commit()
    return str(rid)


def test_reaper_requeues_an_expired_lease_and_returns_the_run_to_pending(
    client: TestClient, world: World, owner_session: Session
) -> None:
    rid = _running_run_with_job(client, world, owner_session, attempts=1)
    reaped = owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
    owner_session.commit()
    assert [(str(r[1]), r[2]) for r in reaped] == [(rid, "lease_lost")]
    assert owner_session.execute(
        text("SELECT status, lease_owner FROM shifde.jobs WHERE run_id = :r"), {"r": rid}
    ).one() == ("queued", None)
    status = owner_session.execute(
        text("SELECT status, lease_token FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one()
    assert status == ("pending", None)
    types = [
        r[0]
        for r in owner_session.execute(
            text("SELECT type FROM shifde.run_events WHERE run_id = :r ORDER BY seq"), {"r": rid}
        )
    ]
    assert types[-1] == "lease_lost"


def test_reaper_fails_the_run_when_the_job_has_no_attempts_left(
    client: TestClient, world: World, owner_session: Session
) -> None:
    rid = _running_run_with_job(client, world, owner_session, attempts=3, max_attempts=3)
    reaped = owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
    owner_session.commit()
    assert [(str(r[1]), r[2]) for r in reaped] == [(rid, "worker_lost")]
    run = owner_session.execute(
        text("SELECT status, error_class FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one()
    assert run == ("failed", "internal")
    assert (
        owner_session.execute(
            text("SELECT status FROM shifde.jobs WHERE run_id = :r"), {"r": rid}
        ).scalar_one()
        == "dead"
    )


def test_reaper_times_out_runs_past_their_deadline_and_finalizes_cancellation(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    late = start_run(client, h, stack.agent["id"], "late")["id"]
    flagged = start_run(client, h, stack.agent["id"], "flagged")["id"]
    owner_session.execute(
        text("UPDATE shifde.runs SET deadline_at = now() - interval '1 second' WHERE id = :r"),
        {"r": late},
    )
    owner_session.execute(
        text("UPDATE shifde.runs SET cancel_requested_at = now() WHERE id = :r"), {"r": flagged}
    )
    owner_session.commit()
    reaped = {
        (str(r[1]), r[2])
        for r in owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()"))
    }
    owner_session.commit()
    assert reaped == {(late, "timed_out"), (flagged, "cancelled")}
    got = dict(owner_session.execute(text("SELECT id::text, status FROM shifde.runs")).all())
    assert got == {late: "timed_out", flagged: "cancelled"}


def test_orchestration_cross_tenant_runs_are_not_found(
    client: TestClient, world: World, owner_session: Session
) -> None:
    hb = login(client, world.passwords, ADMIN_B)
    r = client.post(
        "/api/v1/orgs/demo-b/runs",
        headers=hb,
        json={
            "kind": "orchestration",
            "orchestration_id": "01900000-0000-7000-8000-0000000000aa",
            "workspace": "main",
            "project": "sample",
            "input": "x",
        },
    )
    assert r.status_code == 404


# Ported in part 2c (B-2422) over `sample_db_url` (B-2420).
def test_sql_tool_honours_the_tool_timeout_not_only_the_statement_timeout(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    pg_password = sample_db_url.split(":")[2].split("@")[0]
    conn = connection(client, h, pg_body(sample_db_url, secret(client, h, "pg-pass", pg_password)))
    activate(client, h, conn["id"])  # statement_timeout_ms stays at its 15 s default
    spec = {
        "executor": "connection.sql",
        "binding": {"connection_id": conn["id"], "sql_template": "SELECT pg_sleep(8)"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 1,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    tool = art(client, h, "tools", "sleepy", spec, tenant_scope(owner_session))
    version = publish(client, h, "tools", tool["id"])
    started = time.monotonic()
    r = client.post(
        f"{A}/tools/{tool['id']}/test", headers=h, json={"version_id": version["id"], "args": {}}
    )
    elapsed = time.monotonic() - started
    assert r.status_code == 502 and "timeout" in r.json()["detail"].lower(), r.text
    assert elapsed < 5, f"the tool's timeout_s=1 was not honoured ({elapsed:.1f}s)"
