"""P5 review round 1: lease binding between runs and jobs, fenced gate writes, idempotent replay
authorization, reservation identity and separation of duties for controlled principals."""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.core import db
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import lifecycle
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.governance import BudgetScope, TokenBudgetHooks
from shifde.runtime.worker import Worker, _Job
from tests._support.fullapp import World
from tests._support.p2util import login, tenant_id
from tests._support.p3util import ADMIN, A
from tests._support.p5util import (
    BASE_PERMS,
    Stack,
    audit_actions,
    events,
    limited_user,
    start_run,
    thread_worker,
    user_session,
    wait_for,
)


def _live_run_with_stale_job(
    client: TestClient, world: World, owner: Session, *, attempts: int, max_attempts: int = 5
) -> tuple[str, str, str]:
    """A run executing under live job J2 plus an expired, stale job J1 of the same run."""
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner, script=[{"match": "", "reply": "done"}])
    rid = start_run(client, h, stack.agent["id"], "go")["id"]
    j1 = owner.execute(
        text("SELECT id FROM shifde.jobs WHERE run_id = :r"), {"r": rid}
    ).scalar_one()
    owner.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'dead', attempts = :a, "
            "max_attempts = :m, lease_expires_at = now() - interval '1 second' WHERE id = :j"
        ),
        {"a": attempts, "m": max_attempts, "j": j1},
    )
    owner.execute(
        text(
            "INSERT INTO shifde.jobs (id, tenant_id, kind, run_id, dedupe_key, status, "
            "lease_owner, lease_expires_at, attempts) SELECT gen_random_uuid(), tenant_id, "
            "'run.advance', id, 'live-job', 'leased', 'live', now() + interval '1 hour', 1 "
            "FROM shifde.runs WHERE id = :r"
        ),
        {"r": rid},
    )
    j2 = owner.execute(
        text("SELECT id FROM shifde.jobs WHERE dedupe_key = 'live-job'")
    ).scalar_one()
    owner.execute(
        text(
            "UPDATE shifde.runs SET status = 'running', state_version = state_version + 5, "
            "lease_token = gen_random_uuid(), lease_job_id = :j2 WHERE id = :r"
        ),
        {"r": rid, "j2": j2},
    )
    owner.commit()
    return str(rid), str(j1), str(j2)


# ================================================================== finding 1 / 2
def test_reaper_never_takes_a_live_lease_from_a_stale_job(
    client: TestClient, world: World, owner_session: Session
) -> None:
    rid, j1, j2 = _live_run_with_stale_job(client, world, owner_session, attempts=1)
    token = owner_session.execute(
        text("SELECT lease_token FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).scalar_one()
    reaped = owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
    owner_session.commit()
    assert reaped == []  # nothing to audit: the run was not touched
    run = owner_session.execute(
        text("SELECT status, lease_token, lease_job_id FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one()
    assert run[0] == "running" and run[1] == token and str(run[2]) == j2
    jobs = dict(owner_session.execute(text("SELECT id::text, status FROM shifde.jobs")).all())
    assert (
        jobs[j1] == "done" and jobs[j2] == "leased"
    )  # the stale job is closed, the live one stays
    types = [
        r[0]
        for r in owner_session.execute(
            text("SELECT type FROM shifde.run_events WHERE run_id = :r"), {"r": rid}
        )
    ]
    assert "lease_lost" not in types


def test_stale_job_with_exhausted_attempts_does_not_fail_a_live_run(
    client: TestClient, world: World, owner_session: Session
) -> None:
    rid, j1, j2 = _live_run_with_stale_job(client, world, owner_session, attempts=5, max_attempts=5)
    reaped = owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
    owner_session.commit()
    assert reaped == []
    assert owner_session.execute(
        text("SELECT status, error_class FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one() == ("running", None)
    jobs = dict(owner_session.execute(text("SELECT id::text, status FROM shifde.jobs")).all())
    assert jobs[j1] == "done"  # not `dead`: nothing was lost


def test_reaper_still_requeues_a_job_whose_worker_died_before_claiming_the_run(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    rid = start_run(client, h, stack.agent["id"], "go")["id"]
    owner_session.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'dead', attempts = 1, "
            "lease_expires_at = now() - interval '1 second'"
        )
    )
    owner_session.commit()
    owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
    owner_session.commit()
    assert owner_session.execute(text("SELECT status FROM shifde.jobs")).scalar_one() == "queued"
    assert (
        owner_session.execute(
            text("SELECT status FROM shifde.runs WHERE id = :r"), {"r": rid}
        ).scalar_one()
        == "pending"
    )
    # ... and fails the run when the job is out of attempts and nothing else can run it
    owner_session.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', attempts = max_attempts, "
            "lease_expires_at = now() - interval '1 second'"
        )
    )
    owner_session.commit()
    owner_session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
    owner_session.commit()
    assert owner_session.execute(
        text("SELECT status, error_class FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one() == ("failed", "internal")


def test_advance_leaves_a_run_alone_that_runs_under_another_jobs_live_lease(
    client: TestClient, world: World, owner_session: Session, settings: Settings, app_url: str
) -> None:
    rid, j1, _j2 = _live_run_with_stale_job(client, world, owner_session, attempts=1)
    before = owner_session.execute(
        text(
            "SELECT status, lease_token, lease_job_id, attempt_count FROM shifde.runs WHERE id = :r"
        ),
        {"r": rid},
    ).one()
    tenant = tenant_id(owner_session, "demo-a")
    database = Database(app_url, pool_size=2)
    try:
        outcome = lifecycle.advance(
            database,
            settings,
            IntegrationRuntime(settings=settings, registry=AdapterRegistry.load()),
            TokenBudgetHooks(),
            tenant,
            UUID(rid),
            "worker-x",
            threading.Event(),
            threading.Event(),
            UUID(j1),  # the STALE job
        )
    finally:
        database.dispose()
    assert outcome == "skipped"
    owner_session.rollback()
    after = owner_session.execute(
        text(
            "SELECT status, lease_token, lease_job_id, attempt_count FROM shifde.runs WHERE id = :r"
        ),
        {"r": rid},
    ).one()
    assert after == before  # no steal, no recovery, no new attempt


def test_settling_a_failed_advance_closes_a_stale_job_but_keeps_a_needed_lease(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    rid, j1, j2 = _live_run_with_stale_job(client, world, owner_session, attempts=1)
    owner_session.execute(
        text(
            "UPDATE shifde.jobs SET lease_owner = 'w-test', lease_expires_at = now() + interval '1 hour'"
        )
    )
    owner_session.commit()
    tenant = tenant_id(owner_session, "demo-a")
    worker = Worker(settings, worker_id="w-test", concurrency=1)
    try:
        # the run executes under J2: J1 (stale) is finished, J2 (the owner) is left alone
        worker._settle_job(_Job(UUID(j1), tenant, UUID(rid)), "error")
        worker._settle_job(_Job(UUID(j2), tenant, UUID(rid)), "lease_lost")
    finally:
        worker.db.dispose()
    owner_session.rollback()
    jobs = dict(owner_session.execute(text("SELECT id::text, status FROM shifde.jobs")).all())
    assert jobs[j1] == "done"
    assert jobs[j2] == "leased"  # the run still runs under it: its lease must expire, not vanish


# ================================================================== finding 3 / 7
def test_concurrent_create_race_still_checks_the_request_fingerprint(
    client: TestClient, world: World, owner_session: Session, monkeypatch: Any
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    first = start_run(client, h, stack.agent["id"], "same", key="race-1")
    real = repo.run_by_idempotency
    calls = {"n": 0}

    # the repository takes the client second (B-2115); the race being simulated is unchanged
    def blind_first_lookup(session: Any, tenant_id: Any, principal_id: Any, key: str) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else real(session, tenant_id, principal_id, key)

    monkeypatch.setattr(repo, "run_by_idempotency", blind_first_lookup)
    body = {
        "kind": "agent",
        "agent_id": stack.agent["id"],
        "workspace": "main",
        "project": "sample",
    }
    other = client.post(
        f"{A}/runs", headers={**h, "Idempotency-Key": "race-1"}, json={**body, "input": "DIFFERENT"}
    )
    assert other.status_code == 409 and other.json()["code"] == "idempotency_conflict"
    calls["n"] = 0
    same = client.post(
        f"{A}/runs", headers={**h, "Idempotency-Key": "race-1"}, json={**body, "input": "same"}
    )
    assert same.status_code == 200 and same.json()["id"] == first["id"]


def test_idempotent_replay_is_authorized_like_a_fresh_request(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin_h = login(client, world.passwords, ADMIN)
    stack = Stack(client, admin_h, owner_session, script=[])
    _uid, role_id = limited_user(
        client, owner_session, admin_h, world.passwords, "replay@demo-a.local", BASE_PERMS
    )
    uh = user_session(client, world.passwords, "replay@demo-a.local")
    body = {
        "kind": "agent",
        "agent_id": stack.agent["id"],
        "workspace": "main",
        "project": "sample",
        "input": "x",
    }
    first = uh.client.post(f"{A}/runs", headers={**uh, "Idempotency-Key": "rp-1"}, json=body)
    assert first.status_code == 202
    again = uh.client.post(f"{A}/runs", headers={**uh, "Idempotency-Key": "rp-1"}, json=body)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]

    def revoke(perm: str) -> None:
        owner_session.execute(
            text("DELETE FROM shifde.role_permissions WHERE role_id = :r AND permission_key = :p"),
            {"r": role_id, "p": perm},
        )
        owner_session.commit()

    revoke("agent:execute")
    denied = uh.client.post(f"{A}/runs", headers={**uh, "Idempotency-Key": "rp-1"}, json=body)
    assert denied.status_code == 403
    revoke("run:read")
    hidden = uh.client.post(f"{A}/runs", headers={**uh, "Idempotency-Key": "rp-1"}, json=body)
    assert hidden.status_code == 404


# ================================================================== finding 4
def test_reservation_retry_after_a_crash_returns_the_same_hold_that_settles_and_releases(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    rid = UUID(start_run(client, h, stack.agent["id"], "go")["id"])
    tenant = tenant_id(owner_session, "demo-a")
    owner_session.execute(
        text(
            "INSERT INTO shifde.budgets (id, tenant_id, scope_type, scope_id, period, limit_tokens) "
            "VALUES (gen_random_uuid(), :t, 'tenant', :t, 'day', 100000)"
        ),
        {"t": tenant},
    )
    owner_session.commit()
    run = owner_session.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one()
    scope = BudgetScope(tenant, run[0], run[1], None, rid)
    hooks = TokenBudgetHooks()
    with app_db.session() as s:
        db.set_tenant(s, tenant)
        first = hooks.reserve(s, scope, rid, "model:0:1", 50)
        s.commit()
        retry = hooks.reserve(s, scope, rid, "model:0:1", 70)  # same step key after a crash
        s.commit()
        assert first.id is not None and retry.id == first.id
        rows = s.execute(text("SELECT id, amount, status FROM shifde.budget_reservations")).all()
        assert [(r[0], r[1], r[2]) for r in rows] == [(first.id, 70, "held")]
        hooks.settle(s, retry, 40)
        s.commit()
        assert s.execute(
            text("SELECT status, settled_amount FROM shifde.budget_reservations")
        ).one() == (
            "settled",
            40,
        )
        again = hooks.reserve(s, scope, rid, "model:0:1", 30)
        s.commit()
        hooks.release(s, again)
        s.commit()
        assert (
            s.execute(text("SELECT status FROM shifde.budget_reservations")).scalar_one()
            == "released"
        )
        s.rollback()


# ================================================================== finding 5
def test_a_worker_that_lost_the_lease_cannot_append_gate_records(
    client: TestClient, world: World, owner_session: Session, settings: Settings, monkeypatch: Any
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    rid = start_run(client, h, stack.agent["id"], "go")["id"]
    original = RunContext.checkpoint
    state: dict[str, bool] = {"stolen": False}

    def steal_then_pass(self: RunContext) -> None:
        original(self)  # first call: the lease is still ours
        if not state["stolen"]:
            state["stolen"] = True  # another worker takes the run right after the checkpoint
            self.session.execute(
                text("UPDATE shifde.runs SET lease_token = :t WHERE id = :r"),
                {"t": uuid4(), "r": self.run_id},
            )
            self.session.commit()

    monkeypatch.setattr(RunContext, "checkpoint", steal_then_pass)
    with thread_worker(settings) as worker:
        wait_for(lambda: state["stolen"], 30)
        # stop as soon as the robbed advance has ended: the job lease is deliberately left to
        # expire, and a live worker would re-claim the run after that (a different scenario)
        wait_for(lambda: not worker._inflight, 30)
    decisions = owner_session.execute(
        text("SELECT count(*) FROM shifde.policy_decisions WHERE run_id = :r"), {"r": rid}
    ).scalar_one()
    gate_events = [e for e in events(client, h, rid) if e["type"] == "gate_decision"]
    assert decisions == 0 and gate_events == []  # nothing appended after the lease moved
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.run_steps WHERE run_id = :r"), {"r": rid}
        ).scalar_one()
        == 0
    )


# ================================================================== finding 6
def _waiting_run_as_service_account(
    client: TestClient, world: World, owner: Session, owner_user_email: str
) -> tuple[str, str]:
    """A run `initiated` by a service account owned by `owner_user_email`, parked on an approval."""
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner, script=[])
    rid = start_run(client, h, stack.agent["id"], "go")["id"]
    tenant = tenant_id(owner, "demo-a")
    sa = uuid4()
    owner.execute(
        text(
            "INSERT INTO shifde.service_accounts (id, tenant_id, name, owner_principal_id) "
            "VALUES (:i, :t, 'ci-bot', (SELECT id FROM shifde.users WHERE primary_email = :e))"
        ),
        {"i": sa, "t": tenant, "e": owner_user_email},
    )
    owner.execute(
        text(
            "UPDATE shifde.runs SET status = 'running', state_version = state_version + 1, "
            "principal_type = 'service_account', initiated_by_principal_id = :sa WHERE id = :r"
        ),
        {"sa": sa, "r": rid},
    )
    owner.execute(
        text(
            "UPDATE shifde.runs SET status = 'waiting_for_approval', waiting_reason = 'approval', "
            "state_version = state_version + 1 WHERE id = :r"
        ),
        {"r": rid},
    )
    owner.execute(text("UPDATE shifde.jobs SET status = 'done'"))
    owner.execute(
        text(
            "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, project_id, call_ref, "
            "reason, tool_key, tool_version_no, side_effect_class, requested_by, expires_at) "
            "SELECT gen_random_uuid(), tenant_id, id, workspace_id, project_id, '1:0', "
            "'tool_side_effect', 'echo', 1, 'write_non_idempotent', initiated_by_principal_id, "
            "now() + interval '1 day' FROM shifde.runs WHERE id = :r"
        ),
        {"r": rid},
    )
    owner.commit()
    aid = owner.execute(text("SELECT id FROM shifde.approvals")).scalar_one()
    return rid, str(aid)


def test_owner_of_the_service_account_a_run_ran_as_cannot_approve_it(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin_h = login(client, world.passwords, ADMIN)
    perms = ["workspace:read", "project:read", "run:read", "approval:read", "approval:decide"]
    limited_user(client, owner_session, admin_h, world.passwords, "sa-owner@demo-a.local", perms)
    limited_user(
        client,
        owner_session,
        login(client, world.passwords, ADMIN),
        world.passwords,
        "bystander@demo-a.local",
        perms,
    )
    _rid, aid = _waiting_run_as_service_account(
        client, world, owner_session, "sa-owner@demo-a.local"
    )
    oh = user_session(client, world.passwords, "sa-owner@demo-a.local")
    blocked = oh.client.post(
        f"{A}/approvals/{aid}/decide", headers=oh, json={"decision": "approve"}
    )
    assert blocked.status_code == 403 and blocked.json()["code"] == "separation_of_duties"
    bh = user_session(client, world.passwords, "bystander@demo-a.local")
    ok = bh.client.post(f"{A}/approvals/{aid}/decide", headers=bh, json={"decision": "reject"})
    assert ok.status_code == 200
    decided_by = ok.json()["decision"]["decided_by"]
    assert decided_by["type"] == "user" and decided_by["display"]


def _waiting_run_via_api_key(
    client: TestClient, world: World, owner: Session, creator_email: str
) -> str:
    """An admin-started run that ran through an API key CREATED by `creator_email`."""
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner, script=[])
    rid = start_run(client, h, stack.agent["id"], "go")["id"]
    tenant = tenant_id(owner, "demo-a")
    key = uuid4()
    owner.execute(
        text(
            "INSERT INTO shifde.api_keys (id, tenant_id, name, principal_type, principal_id, "
            "prefix, key_hash, scopes, expires_at, created_by) SELECT :k, :t, 'ci key', 'user', "
            "initiated_by_principal_id, 'abcd1234', decode('00', 'hex'), ARRAY['agent:execute'], "
            "now() + interval '1 day', (SELECT id FROM shifde.users WHERE primary_email = :e) "
            "FROM shifde.runs WHERE id = :r"
        ),
        {"k": key, "t": tenant, "r": rid, "e": creator_email},
    )
    owner.execute(
        text(
            "UPDATE shifde.runs SET status = 'running', state_version = state_version + 1, "
            "api_key_id = :k WHERE id = :r"
        ),
        {"k": key, "r": rid},
    )
    owner.execute(
        text(
            "UPDATE shifde.runs SET status = 'waiting_for_approval', waiting_reason = 'approval', "
            "state_version = state_version + 1 WHERE id = :r"
        ),
        {"r": rid},
    )
    owner.execute(text("UPDATE shifde.jobs SET status = 'done'"))
    owner.execute(
        text(
            "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, project_id, call_ref, "
            "reason, tool_key, tool_version_no, side_effect_class, requested_by, expires_at) "
            "SELECT gen_random_uuid(), tenant_id, id, workspace_id, project_id, '1:0', "
            "'tool_side_effect', 'echo', 1, 'write_non_idempotent', initiated_by_principal_id, "
            "now() + interval '1 day' FROM shifde.runs WHERE id = :r"
        ),
        {"r": rid},
    )
    owner.commit()
    return str(owner.execute(text("SELECT id FROM shifde.approvals")).scalar_one())


def test_creator_of_the_api_key_a_run_ran_through_cannot_approve_it(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin_h = login(client, world.passwords, ADMIN)
    perms = ["workspace:read", "project:read", "run:read", "approval:read", "approval:decide"]
    limited_user(client, owner_session, admin_h, world.passwords, "key-maker@demo-a.local", perms)
    limited_user(
        client,
        owner_session,
        login(client, world.passwords, ADMIN),
        world.passwords,
        "other@demo-a.local",
        perms,
    )
    aid = _waiting_run_via_api_key(client, world, owner_session, "key-maker@demo-a.local")
    kh = user_session(client, world.passwords, "key-maker@demo-a.local")
    blocked = kh.client.post(
        f"{A}/approvals/{aid}/decide", headers=kh, json={"decision": "approve"}
    )
    assert blocked.status_code == 403 and blocked.json()["code"] == "separation_of_duties"
    oh = user_session(client, world.passwords, "other@demo-a.local")
    assert (
        oh.client.post(
            f"{A}/approvals/{aid}/decide", headers=oh, json={"decision": "reject"}
        ).status_code
        == 200
    )


# ================================================================== worker bookkeeping
def _leased_job(owner: Session, client: TestClient, world: World) -> tuple[Any, Any, str]:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner, script=[])
    rid = start_run(client, h, stack.agent["id"], "go")["id"]
    owner.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-dup', attempts = 1, "
            "lease_expires_at = now() + interval '1 hour'"
        )
    )
    owner.commit()
    row = owner.execute(
        text("SELECT id, tenant_id, run_id FROM shifde.jobs WHERE run_id = :r"), {"r": rid}
    ).one()
    return row[0], row[1], str(rid)


def test_a_finished_advance_never_drops_a_newer_inflight_entry_of_the_same_job(
    client: TestClient, world: World, owner_session: Session, settings: Settings, monkeypatch: Any
) -> None:
    jid, tid, rid = _leased_job(owner_session, client, world)
    worker = Worker(settings, worker_id="w-dup", concurrency=2)
    try:
        older, newer = _Job(jid, tid, UUID(rid)), _Job(jid, tid, UUID(rid))
        worker._inflight[jid] = newer  # the re-claimed entry that must keep being heartbeated
        monkeypatch.setattr(lifecycle, "advance", lambda *a, **k: "lease_lost")
        worker._work(older)  # the first, robbed advance ends now
        assert worker._inflight.get(jid) is newer
        worker._work(newer)
        assert jid not in worker._inflight
    finally:
        worker.db.dispose()


def test_a_job_claimed_twice_is_not_run_twice_and_its_claim_is_handed_back(
    client: TestClient, world: World, owner_session: Session, settings: Settings, monkeypatch: Any
) -> None:
    jid, tid, rid = _leased_job(owner_session, client, world)
    worker = Worker(settings, worker_id="w-dup", concurrency=2)
    started: list[Any] = []
    release = threading.Event()

    def blocked_work(job: _Job) -> None:
        started.append(job)
        release.wait(30)
        with worker._lock:
            if worker._inflight.get(job.id) is job:
                del worker._inflight[job.id]

    monkeypatch.setattr(worker, "_work", blocked_work)
    try:
        row = {"job_id": jid, "tenant_id": tid, "run_id": UUID(rid)}
        worker._dispatch([row])
        wait_for(lambda: len(started) == 1, 10)
        first = worker._inflight[jid]
        # The reaper requeued J1 and this worker claims it again through the real claim path
        # with exactly one free slot: the duplicate is handed back, delayed, and no re-wake.
        worker.concurrency = 2  # one slot busy (J1), one free
        owner_session.execute(
            text(
                "UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL, "
                "lease_expires_at = NULL, run_after = now() - interval '1 second' WHERE id = :j"
            ),
            {"j": jid},
        )
        owner_session.commit()
        before = owner_session.execute(
            text("SELECT attempts FROM shifde.jobs WHERE id = :j"), {"j": jid}
        ).scalar_one()
        worker.wake.clear()
        worker._claim_and_dispatch()
        assert worker._inflight[jid] is first and len(started) == 1
        assert not worker.wake.is_set()  # no busy loop on a handed-back claim
        owner_session.rollback()
        status, owner_, attempts, delayed = owner_session.execute(
            text(
                "SELECT status, lease_owner, attempts, run_after > now() + interval '1 second' "
                "FROM shifde.jobs WHERE id = :j"
            ),
            {"j": jid},
        ).one()
        assert (status, owner_) == ("queued", None) and delayed  # handed back with a backoff
        assert attempts == before  # the duplicate claim's attempt is not counted
        release.set()
        wait_for(lambda: jid not in worker._inflight, 10)
    finally:
        release.set()
        worker._executor.shutdown(wait=True)
        worker.db.dispose()


# SETH's two `p05_0003` backfill tests are not ported: the unified schema is created in its final
# shape by `runtime_0001` (B-2101), so the upgrade statements they run do not exist (B-2412).


def test_a_run_whose_deadline_passed_before_it_started_is_timed_out_and_audited(
    client: TestClient, world: World, owner_session: Session, settings: Settings, app_url: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=[])
    rid = start_run(client, h, stack.agent["id"], "late")["id"]
    owner_session.execute(
        text("UPDATE shifde.runs SET deadline_at = now() - interval '1 second' WHERE id = :r"),
        {"r": rid},
    )
    job = owner_session.execute(
        text("SELECT id FROM shifde.jobs WHERE run_id = :r"), {"r": rid}
    ).scalar_one()
    owner_session.commit()
    database = Database(app_url, pool_size=2)
    try:
        outcome = lifecycle.advance(
            database,
            settings,
            IntegrationRuntime(settings=settings, registry=AdapterRegistry.load()),
            TokenBudgetHooks(),
            tenant_id(owner_session, "demo-a"),
            UUID(rid),
            "worker-x",
            threading.Event(),
            threading.Event(),
            job,
        )
    finally:
        database.dispose()
    assert outcome == "done"
    owner_session.rollback()
    assert owner_session.execute(
        text("SELECT status, error_class FROM shifde.runs WHERE id = :r"), {"r": rid}
    ).one() == ("timed_out", "timeout")
    # audited in the same transaction as the transition (was missing before)
    assert audit_actions(owner_session, rid).count("run.timed_out") == 1
