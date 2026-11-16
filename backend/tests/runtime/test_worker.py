"""Runtime part 12 (RT-12): `worker.py`, the `shifde-worker` process.

Behaviour: the worker refuses a privileged database role at startup (exit 3, B-2164); it claims
through `shifde_claim_jobs` and advances each claimed run through `lifecycle.advance` with the
`(job_id, tenant_id, run_id)` the claim returned, on its own app-role database (the
`RunContext.db`); it reaps through `shifde_reap_expired_leases` and audits each reaped run in the
run's own client; a job wedged past its run's deadline is no longer heartbeated (ARCH M3).

C1 end to end (charter 5 item 3, B-2151; the equivalent of SETH `test_r1_unknown_outcome.py`
tests 1 and 2, which drive the full app): a worker loop is executing an approved non-idempotent
write when it stops heartbeating (killed / wedged) and loses its lease; after Cancel (or the
deadline) and a reaper pass by another worker, the run is reported `cancelled` (`timed_out`)
with `indeterminate_side_effect`, the write step and invocation `indeterminate`, a pending
unknown-outcome approval reachable through the approvals route, and the
`run.indeterminate_side_effect` audit event; the late return of the stale executor changes
nothing and the write is never repeated.

Isolation: the claimed client is the only client a job touches; a job naming another client's id
(heartbeat, release, finish, reap audit) changes nothing of that client; B's owner sees nothing
of A's run or decision through the routes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.dbrole import UnsafeDatabaseRole
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import lifecycle
from shifde.runtime import worker as worker_mod
from shifde.runtime.context import RunContext
from shifde.runtime.governance import PolicyBudgetHooks
from shifde.runtime.worker import Worker, _Job
from tests._support.runs import add_run
from tests.build.env_support import add_published
from tests.build.service_support import REGISTRY, add_person
from tests.connections.routes import as_user, org
from tests.runtime.conftest import insert
from tests.runtime.execution import client_state
from tests.runtime.routes import runtime_client
from tests.tenancy.conftest import World, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

UNKNOWN = "indeterminate_side_effect"
WRITE_TOOL = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
              "side_effect_class": "write_non_idempotent",
              "input_schema": {"type": "object"}}  # fmt: skip


@dataclass(frozen=True)
class C:
    tenant: UUID
    slug: str
    run: UUID  # pending root agent run with a queued job
    job: UUID
    tool: UUID  # a published write tool version


def _seed(o: Session, tenant: UUID, slug: str, *, queued: bool = True) -> C:
    user = add_person(o, tenant, "runner", "tenant_administrator")
    run = add_run(o, tenant, initiated_by_principal_id=user)
    _, tool = add_published(o, tenant, "tools", f"tool-{uuid7().hex}", WRITE_TOOL)
    job = uuid7()
    insert(o, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=run,
           dedupe_key=f"run:{run}:{job}", status="queued" if queued else "done",
           traceparent=f"00-{run.hex}-{'2' * 16}-01")  # fmt: skip
    return C(tenant, slug, run, job, tool)


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def two(owner_db: Database, world: World, people: People) -> tuple[C, C]:
    with owner_db.session() as o:
        a = _seed(o, world.a.tenant, world.a.slug)
        b = _seed(o, world.b.tenant, world.b.slug, queued=False)
        o.commit()
    return a, b


class Script:
    """Stands in for every executor: `behave(ctx)` is the test's script."""

    behave: Callable[[RunContext], Any] = staticmethod(lambda ctx: {"ok": 1})
    seen: list[RunContext] = []

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def run(self) -> Any:
        Script.seen.append(self.ctx)
        return Script.behave(self.ctx)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> type[Script]:
    Script.seen = []
    Script.behave = staticmethod(lambda ctx: {"ok": 1})
    for name in ("AgentRunner", "GraphRunner", "PipelineRunner", "OntologyActionRunner"):
        monkeypatch.setattr(lifecycle, name, Script)
    return Script


def _worker(settings: Settings, worker_id: str, **kw: Any) -> Worker:
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    opts: dict[str, Any] = {"concurrency": 1, "lease_seconds": 2, "poll_ms": 50,
                            "reap_seconds": 3600.0, **kw}  # fmt: skip
    return Worker(settings, worker_id=worker_id, runtime=runtime, hooks=PolicyBudgetHooks(),
                  **opts)  # fmt: skip


@pytest.fixture
def running(settings: Settings) -> Iterator[Callable[..., Worker]]:
    """Start worker loops in threads; every one is stopped and joined at teardown."""
    started: list[tuple[Worker, threading.Thread]] = []

    def start(worker_id: str = "w-loop", **kw: Any) -> Worker:
        w = _worker(settings, worker_id, **kw)
        t = threading.Thread(target=w.run, daemon=True, name=f"test-{worker_id}")
        t.start()
        started.append((w, t))
        return w

    yield start
    for w, t in started:
        w.request_stop()
        t.join(60)
        assert not t.is_alive(), "a worker loop did not stop"


def _until(check: Callable[[], bool], timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _q(owner_db: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # Any: row values
    with owner_db.session() as o:
        return [tuple(r) for r in o.execute(text(sql), params).all()]


def _status(owner_db: Database, run: UUID) -> str:
    return str(_q(owner_db, "SELECT status FROM shifde.runs WHERE id = :r", r=run)[0][0])


def _state(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        return client_state(o, tenant)


# ------------------------------------------------------------------ startup
def test_the_worker_refuses_a_privileged_database_role_at_startup(
    settings: Settings, owner_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    privileged = settings.model_copy(update={"database_url": owner_url})
    with pytest.raises(SystemExit) as exited:
        worker_mod.refuse_unsafe_role(privileged)
    assert exited.value.code == worker_mod.EXIT_UNSAFE_ROLE == 3
    err = capsys.readouterr().err
    assert "refusing to start" in err and owner_url not in err
    with pytest.raises(UnsafeDatabaseRole):
        _worker(privileged, "w-privileged")  # `Database` refuses it too
    w = _worker(settings, "w-app")  # the application role passes both
    try:
        worker_mod.refuse_unsafe_role(settings)
        with w.db.session() as s:
            assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    finally:
        w._executor.shutdown()
        w.db.dispose()


# ------------------------------------------------------------------ claim and advance
def test_each_claimed_run_is_advanced_in_its_own_client_on_the_workers_database(
    two: tuple[C, C], owner_db: Database, script: type[Script], running: Callable[..., Worker]
) -> None:
    a, b = two
    with owner_db.session() as o:  # B gets a queued job of its own as well
        b_job = uuid7()
        insert(o, "jobs", id=b_job, tenant_id=b.tenant, kind="run.advance", run_id=b.run,
               dedupe_key=f"run:{b.run}:{b_job}")  # fmt: skip
        o.commit()
    w = running("w-two", concurrency=2)
    _until(lambda: _status(owner_db, a.run) == "completed" == _status(owner_db, b.run))
    seen = {ctx.tenant_id: ctx for ctx in Script.seen}
    assert set(seen) == {a.tenant, b.tenant}
    for c in (a, b):
        ctx = seen[c.tenant]
        assert ctx.run["id"] == c.run and ctx.run["tenant_id"] == c.tenant
        assert ctx.db is w.db  # durable usage rows go through the worker's app-role database
    _until(lambda: {r[0] for r in _q(owner_db, "SELECT status FROM shifde.jobs WHERE id IN "
                                     "(:a, :b)", a=a.job, b=b_job)} == {"done"})  # fmt: skip


def test_a_job_naming_another_clients_id_touches_nothing_of_that_client(
    two: tuple[C, C], owner_db: Database, settings: Settings
) -> None:
    """Heartbeat, release and settle name the claimed client: a job carrying A's client id with
    B's job and run (a forged claim row) changes none of B's rows; B's own client id does."""
    a, b = two
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-x', "
                       "lease_expires_at = now() + interval '1 hour' WHERE id = :j"),
                  {"j": b.job})  # fmt: skip
        o.commit()
    before = _state(owner_db, b.tenant)
    w = _worker(settings, "w-x")
    try:
        forged = _Job(b.job, a.tenant, b.run)
        w._inflight[forged.id] = forged
        w._beat()
        assert forged.lease_lost.is_set()  # not B's job in A's client: no heartbeat
        w._release_claim(forged)
        w._settle_job(forged, "done")
        w._settle_job(forged, "released")
        assert _state(owner_db, b.tenant) == before
        own = _Job(b.job, b.tenant, b.run)  # not vacuous: B's own client id reaches the row
        w._inflight = {own.id: own}
        w._beat()
        assert not own.lease_lost.is_set()
        w._settle_job(own, "done")
        assert _q(owner_db, "SELECT status FROM shifde.jobs WHERE id = :j", j=b.job) == [("done",)]
    finally:
        w._executor.shutdown()
        w.db.dispose()


def test_a_job_wedged_past_its_runs_deadline_is_no_longer_heartbeated(
    two: tuple[C, C], owner_db: Database, settings: Settings
) -> None:
    """ARCH M3: the lease of a wedged job expires, so the reaper settles its run."""
    a, _ = two
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-m3', "
                       "lease_expires_at = now() + interval '5 seconds' WHERE id = :j"),
                  {"j": a.job})  # fmt: skip
        o.commit()
    w = _worker(settings, "w-m3")
    try:
        job = _Job(a.job, a.tenant, a.run)
        w._inflight[job.id] = job
        expires = "SELECT lease_expires_at FROM shifde.jobs WHERE id = :j"
        w._beat()  # deadline an hour away: heartbeated
        renewed = _q(owner_db, expires, j=a.job)[0][0]
        assert not job.lease_lost.is_set()
        with owner_db.session() as o:
            o.execute(text("UPDATE shifde.runs SET deadline_at = :d WHERE id = :r"),
                      {"d": utcnow() - w.wedged_after - timedelta(seconds=1), "r": a.run})  # fmt: skip
            o.commit()
        w._beat()
        assert job.lease_lost.is_set()
        assert _q(owner_db, expires, j=a.job)[0][0] == renewed  # no longer renewed
    finally:
        w._executor.shutdown()
        w.db.dispose()


def test_a_reaped_run_is_audited_in_its_own_client_only(
    two: tuple[C, C], owner_db: Database, settings: Settings
) -> None:
    a, b = two
    with owner_db.session() as o:  # A's run was claimed by a worker that died
        o.execute(text("UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-dead', "
                       "lease_expires_at = now() - interval '1 second' WHERE id = :j"),
                  {"j": a.job})  # fmt: skip
        o.execute(text("UPDATE shifde.runs SET status = 'running', lease_token = :t, "
                       "lease_job_id = :j, state_version = 2, attempt_count = 1 WHERE id = :r"),
                  {"t": uuid7(), "j": a.job, "r": a.run})  # fmt: skip
        o.commit()
    count = "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t"
    b_audit = _q(owner_db, count, t=b.tenant)
    before = _state(owner_db, b.tenant)
    w = _worker(settings, "w-reaper")
    try:
        w._reap()
        w._audit_reaped(a.tenant, b.run, "worker_lost")  # B's run named with A's client: nothing
    finally:
        w._executor.shutdown()
        w.db.dispose()
    assert _status(owner_db, a.run) == "pending"  # requeued for another worker
    rows = _q(owner_db, "SELECT tenant_id, action FROM shifde.audit_events WHERE target_id = :r",
              r=str(a.run))  # fmt: skip
    assert rows and all(t == a.tenant for t, _ in rows)
    assert _q(owner_db, count, t=b.tenant) == b_audit and _state(owner_db, b.tenant) == before


# ------------------------------------------------------------------ C1 end to end
@pytest.mark.parametrize("ended_by", ["cancel", "deadline"])
def test_c1_a_worker_that_loses_its_lease_mid_write_leaves_an_unknown_outcome_not_a_plain_end(
    ended_by: str,
    two: tuple[C, C],
    people: People,
    owner_db: Database,
    settings: Settings,
    script: type[Script],
    running: Callable[..., Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = two
    writing, release = threading.Event(), threading.Event()

    def approved_write(ctx: RunContext) -> Any:  # Any: output document
        with owner_db.session() as o:  # the approved POST reached the target
            invocation = uuid7()
            insert(o, "tool_invocations", id=invocation, tenant_id=a.tenant, run_id=a.run,
                   call_ref="w1", tool_version_id=a.tool, side_effect_class="write_non_idempotent",
                   idempotency_key=f"k-{invocation}", status="started", request_hash="h",
                   arguments='{"item": "x"}')  # fmt: skip
            insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=a.run, seq=1,
                   kind="tool_call", name="t", status="running", tool_invocation_id=invocation,
                   tool_version_id=a.tool)  # fmt: skip
            o.commit()
        writing.set()
        release.wait(60)  # the process is gone / wedged: its outcome is unknown to the platform
        return {"ok": 1}

    script.behave = staticmethod(approved_write)
    b_before = _state(owner_db, b.tenant)
    w1 = running("w-c1")
    assert writing.wait(30), "the write never started"
    monkeypatch.setattr(w1, "_beat", lambda: None)  # killed: no more heartbeats
    assert _status(owner_db, a.run) == "running"  # a dead worker still holds it
    with runtime_client(settings) as client:
        h = as_user(client, people.owner_a)
        base = org(a.slug)
        if ended_by == "cancel":
            r = client.post(f"{base}/runs/{a.run}/cancel", headers=h)
            assert r.status_code == 202, r.text
        else:
            with owner_db.session() as o:
                o.execute(text("UPDATE shifde.runs SET deadline_at = now() - interval '1 second' "
                               "WHERE id = :r"), {"r": a.run})  # fmt: skip
                o.commit()
        with owner_db.session() as o:  # the lease runs out
            o.execute(text("UPDATE shifde.jobs SET lease_expires_at = now() - interval '1 second' "
                           "WHERE run_id = :r AND status = 'leased'"), {"r": a.run})  # fmt: skip
            o.commit()
        reaper = _worker(settings, "w-reaper")  # another live worker's reaper pass
        try:
            reaper._reap()
        finally:
            reaper._executor.shutdown()
            reaper.db.dispose()
        release.set()  # the stale executor returns late: its finalize is fenced
        w1.request_stop()
        _until(lambda: not w1._inflight)
        status = "cancelled" if ended_by == "cancel" else "timed_out"
        row = _q(owner_db, "SELECT status, error_class, error_message FROM shifde.runs "
                 "WHERE id = :r", r=a.run)[0]  # fmt: skip
        assert row[0] == status and row[1] == UNKNOWN, row
        assert "unknown" in (row[2] or "").lower()
        steps = _q(owner_db, "SELECT kind, status FROM shifde.run_steps WHERE run_id = :r",
                   r=a.run)  # fmt: skip
        assert ("tool_call", "indeterminate") in steps and all(s != "running" for _, s in steps)
        assert _q(owner_db, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r",
                  r=a.run) == [("indeterminate",)]  # once, never repeated  # fmt: skip
        actions = _q(owner_db, "SELECT action FROM shifde.audit_events WHERE target_id = :r",
                     r=str(a.run))  # fmt: skip
        assert ("run.indeterminate_side_effect",) in actions
        got = client.get(f"{base}/runs/{a.run}", headers=h).json()
        assert got["status"] == status and got["error_class"] == UNKNOWN
        assert got["outcome_unknown"] is True
        pending = client.get(f"{base}/approvals", params={"status": "pending"}, headers=h).json()
        [appr] = [x for x in pending["items"] if x["run_id"] == str(a.run)]
        assert appr["reason"] == UNKNOWN and appr["arguments"] == {"item": "x"}
        assert _state(owner_db, b.tenant) == b_before  # the worker and the reaper left B alone
        # B's owner sees nothing of A's run or decision
        hb = as_user(client, people.owner_b)
        b_signed_in = _state(owner_db, b.tenant)  # signing in is audited in B
        assert client.get(f"{org(b.slug)}/runs/{a.run}", headers=hb).status_code == 404
        assert client.get(f"{org(b.slug)}/approvals/{appr['id']}", headers=hb).status_code == 404
        listed = client.get(f"{org(b.slug)}/approvals", headers=hb).json()["items"]
        assert all(x["run_id"] != str(a.run) for x in listed)
        assert _state(owner_db, b.tenant) == b_signed_in
        # the human decision stays open and can be taken: confirmed delivered, never resent
        h = as_user(client, people.owner_a)
        ok = client.post(f"{base}/approvals/{appr['id']}/decide", headers=h,
                         json={"decision": "mark_succeeded", "comment": "verified at the target"})  # fmt: skip
        assert ok.status_code == 200, ok.text
    assert _q(owner_db, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r",
              r=a.run) == [("resolved_succeeded",)]  # fmt: skip
    assert _status(owner_db, a.run) == status
