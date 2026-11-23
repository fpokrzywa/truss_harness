"""Runtime part 12 (RT-12): `worker.py`, the `shifde-worker` process.

Behaviour: the worker refuses a privileged database role at startup (exit 3, B-2164); it claims
through `shifde_claim_jobs` and advances each claimed run through `lifecycle.advance` with the
`(job_id, tenant_id, run_id)` the claim returned, on its own app-role database (the
`RunContext.db`); it reaps through `shifde_reap_expired_leases` and audits each reaped run in the
run's own client; a job wedged past its run's deadline is no longer heartbeated (ARCH M3).

C1 end to end (charter 5 item 3, B-2151; the equivalent of SETH `test_r1_unknown_outcome.py`
tests 1 and 2, which drive the full app): a worker loop runs the real tool step
(`steps.run_tool`), which asks for approval; once it is approved through the approvals route it
journals the non-idempotent write and is delivering it (a blocking `invoke_once`) when it stops
heartbeating (killed / wedged) and loses its lease; after Cancel (or the
deadline) and a reaper pass by another worker, the run is reported `cancelled` (`timed_out`)
with `indeterminate_side_effect`, the write step and invocation `indeterminate`, a pending
unknown-outcome approval reachable through the approvals route, and the
`run.indeterminate_side_effect` audit event; the late return of the stale executor changes
nothing and the write is never repeated.

ARCH M3 remainder (B-2590): one worker loop with a single slot runs that approved write and its
`invoke_once` blocks; once the run is past its deadline by `wedged_after` the heartbeat abandons
the job, and the same worker's reaper settles the run `timed_out` with
`indeterminate_side_effect` (an indeterminate invocation and a pending unknown-outcome approval,
not `cancelled`). The abandoned thread no longer takes the slot: the worker claims and completes
a second run, and stops, while the thread is still blocked; its late return changes none of the
run's rows and the write was delivered once.

Abandoned-thread caps (issue #44): with a per-client share of 1, once client A's wedged write is
abandoned the worker hands A's next claimed job back (its `run_after` pushed out, never run here)
while it still completes a client-B run, the abandoned thread holds no pooled connection while it
blocks, and A's wedged run still ends `timed_out` with `indeterminate_side_effect` after one
delivery; with a per-worker cap of 1 the worker stops claiming once the write is abandoned (a B
job made claimable then stays queued), `Worker.run()` returns 1 while the thread is still
blocked, and the wedged run is settled `timed_out` with `indeterminate_side_effect` by a reaper,
never cancelled; `shifde-worker` (`run`) exits with the worker's exit code.

Isolation: the claimed client is the only client a job touches; a job naming another client's id
(heartbeat, release, finish, reap audit) changes nothing of that client; B's owner sees nothing
of A's run or decision through the routes.
"""

from __future__ import annotations

import json
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
from shifde.adapters.testservers import RestTestServer
from shifde.build import tool_runtime
from shifde.build.env import VersionRef
from shifde.connections import service as connections
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.dbrole import UnsafeDatabaseRole
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.models_admin import service as models_admin
from shifde.runtime import lifecycle, steps
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
MODES = ["app-role", "schema-owner"]
WRITE_TOOL = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
              "side_effect_class": "write_non_idempotent",
              "input_schema": {"type": "object"}}  # fmt: skip


@dataclass(frozen=True)
class C:
    tenant: UUID
    slug: str
    run: UUID  # pending root agent run with a queued job
    job: UUID
    tool: VersionRef  # a published write tool of the client


def _seed(o: Session, tenant: UUID, slug: str, *, queued: bool = True) -> C:
    user = add_person(o, tenant, "runner", "tenant_administrator")
    run = add_run(o, tenant, initiated_by_principal_id=user)
    key = f"tool-{uuid7().hex}"
    art, ver = add_published(o, tenant, "tools", key, WRITE_TOOL)
    tool = VersionRef(ver, art, "tools", 1, "published", WRITE_TOOL, None, key, key, "active")
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

    def start(worker_id: str = "w-loop", *, update: dict[str, Any] | None = None,
              **kw: Any) -> Worker:  # fmt: skip
        w = _worker(settings.model_copy(update=update or {}), worker_id, **kw)
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


def _on_owner(w: Worker, owner_url: str) -> Worker:
    """`w` on the schema owner's database (no row-level security): the worker's own client
    predicates are all that keeps a forged client id away from another client's rows."""
    w.db.dispose()
    w.db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, deliberately
    return w


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
        w.db.dispose()


def test_the_worker_process_exits_3_under_a_privileged_role_before_anything_is_claimed(
    two: tuple[C, C],
    owner_db: Database,
    settings: Settings,
    owner_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`shifde-worker` itself (`run`), not only `refuse_unsafe_role`: exit 3 with one line on
    stderr, no worker registered and A's queued job still queued (runtime isolation review 11
    finding 2)."""
    a, _ = two
    privileged = settings.model_copy(update={"database_url": owner_url})
    monkeypatch.setattr(worker_mod, "settings_or_exit", lambda: privileged)
    claimed: list[object] = []
    monkeypatch.setattr(
        worker_mod.repo, "claim_jobs", lambda *args, **kw: claimed.append(args) or []
    )
    workers = "SELECT count(*) FROM shifde.workers"
    registered = _q(owner_db, workers)
    with pytest.raises(SystemExit) as exited:
        worker_mod.run([])
    assert exited.value.code == worker_mod.EXIT_UNSAFE_ROLE == 3
    err = capsys.readouterr().err
    assert "shifde-worker: refusing to start" in err and owner_url not in err
    assert claimed == [] and _q(owner_db, workers) == registered
    assert _q(owner_db, "SELECT status FROM shifde.jobs WHERE id = :j", j=a.job) == [("queued",)]


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


@pytest.mark.parametrize("mode", MODES)
def test_a_job_naming_another_clients_id_touches_nothing_of_that_client(
    mode: str, two: tuple[C, C], owner_db: Database, settings: Settings, owner_url: str
) -> None:
    """Heartbeat, release and settle (every outcome: done, released, lease_lost, error) name the
    claimed client: a job carrying A's client id with B's job and run (a forged claim row)
    changes none of B's rows; B's own client id does. As `shifde_app` and as the schema owner
    (no row-level security: the repository predicates alone, review 11 finding 5)."""
    a, b = two
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-x', "
                       "lease_expires_at = now() + interval '1 hour' WHERE id = :j"),
                  {"j": b.job})  # fmt: skip
        o.commit()
    before = _state(owner_db, b.tenant)
    w = _worker(settings, "w-x")
    if mode == "schema-owner":
        _on_owner(w, owner_url)
    try:
        forged = _Job(b.job, a.tenant, b.run)
        w._inflight[forged.id] = forged
        w._beat()
        assert forged.lease_lost.is_set()  # not B's job in A's client: no heartbeat
        w._release_claim(forged)
        assert _state(owner_db, b.tenant) == before
        for outcome in ("lease_lost", "error", "done", "released"):
            w._settle_job(forged, outcome)  # lease_lost / error: B's run reads as no run
            assert _state(owner_db, b.tenant) == before, outcome
        own = _Job(b.job, b.tenant, b.run)  # not vacuous: B's own client id reaches the row
        w._inflight = {own.id: own}
        w._beat()
        assert not own.lease_lost.is_set()
        w._settle_job(own, "done")
        assert _q(owner_db, "SELECT status FROM shifde.jobs WHERE id = :j", j=b.job) == [("done",)]
    finally:
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
        w.db.dispose()


def test_a_reaped_run_is_audited_in_its_own_client_only(
    two: tuple[C, C], owner_db: Database, settings: Settings, owner_url: str
) -> None:
    """A reaped run is audited in its own client; B's run named with A's client id is audited
    nowhere, as `shifde_app` and on a worker whose database is the schema owner (no row-level
    security: `get_run`'s client predicate alone, runtime isolation review 11 finding 1)."""
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
    about = "SELECT tenant_id, action FROM shifde.audit_events WHERE target_id = :r"
    b_audit = _q(owner_db, count, t=b.tenant)
    before = _state(owner_db, b.tenant)
    assert _q(owner_db, about, r=str(b.run)) == []
    w = _worker(settings, "w-reaper")
    try:
        w._reap()
        w._audit_reaped(a.tenant, b.run, "worker_lost")  # B's run named with A's client: nothing
    finally:
        w.db.dispose()
    assert _status(owner_db, a.run) == "pending"  # requeued for another worker
    rows = _q(owner_db, about, r=str(a.run))
    assert rows and all(t == a.tenant for t, _ in rows)
    assert _q(owner_db, about, r=str(b.run)) == []  # in no client at all
    owner = _on_owner(_worker(settings, "w-reaper-owner"), owner_url)
    try:
        owner._audit_reaped(a.tenant, b.run, "worker_lost")  # the predicate alone stops it
        assert _q(owner_db, about, r=str(b.run)) == []
        owner._audit_reaped(a.tenant, a.run, "timed_out")  # not vacuous: A's own run is audited
        assert (a.tenant, "run.timed_out") in _q(owner_db, about, r=str(a.run))
    finally:
        owner.db.dispose()
    assert _q(owner_db, about, r=str(b.run)) == []
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
    deliveries: list[str] = []

    def invoke_once(conns: object, spec: object, args: object, **kw: Any) -> object:
        deliveries.append(kw["idempotency_key"])  # the approved POST reached the target
        writing.set()
        release.wait(60)  # the process is gone / wedged: its outcome is unknown to the platform
        return {"written": True}

    monkeypatch.setattr(tool_runtime, "invoke_once", invoke_once)
    call = steps.ToolCall(a.tool, {"item": "x"}, "w1", node_id="w1")
    policy = steps.ToolPolicy(frozenset({"write_non_idempotent"}), None,
                              frozenset({a.tool.version_id}))  # fmt: skip
    # the executor is the real tool step: gate, approval, write-ahead journal, one delivery
    script.behave = staticmethod(lambda ctx: steps.run_tool(ctx, call, policy))
    b_before = _state(owner_db, b.tenant)
    w1 = running("w-c1")
    _until(lambda: _status(owner_db, a.run) == "waiting_for_approval")
    assert deliveries == [] and not writing.is_set()  # nothing is sent before the decision
    with runtime_client(settings) as client:
        h = as_user(client, people.owner_a)
        base = org(a.slug)
        asked = client.get(f"{base}/approvals", params={"status": "pending"}, headers=h).json()
        [first] = [x for x in asked["items"] if x["run_id"] == str(a.run)]
        assert first["reason"] == "tool_side_effect"
        ok = client.post(f"{base}/approvals/{first['id']}/decide", headers=h,
                         json={"decision": "approve"})  # fmt: skip
        assert ok.status_code == 200, ok.text
        assert writing.wait(30), "the approved write never started"
        assert len(deliveries) == 1
        # write-ahead: the invocation was committed `started` before the call was made
        assert _q(owner_db, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r",
                  r=a.run) == [("started",)]  # fmt: skip
        monkeypatch.setattr(w1, "_beat", lambda: None)  # killed: no more heartbeats
        assert _status(owner_db, a.run) == "running"  # a dead worker still holds it
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
            reaper.db.dispose()
        release.set()  # the stale executor returns late: its finalize is fenced
        w1.request_stop()
        _until(lambda: not w1._inflight)
        status = "cancelled" if ended_by == "cancel" else "timed_out"
        row = _q(owner_db, "SELECT status, error_class, error_message FROM shifde.runs "
                 "WHERE id = :r", r=a.run)[0]  # fmt: skip
        assert row[0] == status and row[1] == UNKNOWN, row
        assert "unknown" in (row[2] or "").lower()
        journal = _q(owner_db, "SELECT kind, status FROM shifde.run_steps WHERE run_id = :r",
                   r=a.run)  # fmt: skip
        assert ("tool_call", "indeterminate") in journal and all(s != "running" for _, s in journal)
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
    assert len(deliveries) == 1, "the write was resent"


# ------------------------------------------------------------------ ARCH M3: a wedged run thread
def _run_rows(owner_db: Database, run: UUID) -> tuple[Any, ...]:  # Any: row values
    """Every row of `run`: the run, its steps, tool invocations, approvals and jobs."""
    return (
        _q(owner_db, "SELECT * FROM shifde.runs WHERE id = :r", r=run),
        *(_q(owner_db, f"SELECT * FROM shifde.{t} WHERE run_id = :r ORDER BY id", r=run)
          for t in ("run_steps", "tool_invocations", "approvals", "jobs")),
    )  # fmt: skip


def _thread(name: str) -> threading.Thread:
    [t] = [t for t in threading.enumerate() if t.name == name]
    return t


def test_a_wedged_run_thread_gives_up_its_slot_and_the_worker_claims_new_work(
    two: tuple[C, C],
    people: People,
    owner_db: Database,
    settings: Settings,
    script: type[Script],
    running: Callable[..., Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARCH M3 (B-2590): with one slot, a run thread wedged in an approved write is abandoned;
    the same worker's reaper settles its run as an unknown outcome (not cancelled), the worker
    claims and completes new work and stops while that thread is still blocked, and the thread's
    late return changes nothing."""
    a, b = two
    with owner_db.session() as o:  # A's second run, claimable only once the slot should be free
        user = add_person(o, a.tenant, "runner2", "tenant_administrator")
        second = add_run(o, a.tenant, initiated_by_principal_id=user)
        second_job = uuid7()
        insert(o, "jobs", id=second_job, tenant_id=a.tenant, kind="run.advance", run_id=second,
               dedupe_key=f"run:{second}:{second_job}",
               run_after=utcnow() + timedelta(hours=1))  # fmt: skip
        o.commit()
    writing, release = threading.Event(), threading.Event()
    deliveries: list[str] = []

    def invoke_once(conns: object, spec: object, args: object, **kw: Any) -> object:
        deliveries.append(kw["idempotency_key"])
        writing.set()
        release.wait(300)  # wedged until the test lets it return
        return {"written": True}

    monkeypatch.setattr(tool_runtime, "invoke_once", invoke_once)
    call = steps.ToolCall(a.tool, {"item": "x"}, "w1", node_id="w1")
    policy = steps.ToolPolicy(frozenset({"write_non_idempotent"}), None,
                              frozenset({a.tool.version_id}))  # fmt: skip
    script.behave = staticmethod(
        lambda ctx: steps.run_tool(ctx, call, policy) if ctx.run["id"] == a.run else {"ok": 1}
    )
    b_before = _state(owner_db, b.tenant)
    w = running("w-m3-slot", concurrency=1, lease_seconds=2, reap_seconds=1.0)
    w.wedged_after = timedelta(seconds=1)
    loop = _thread("test-w-m3-slot")
    try:
        _until(lambda: _status(owner_db, a.run) == "waiting_for_approval")
        with runtime_client(settings) as client:
            h = as_user(client, people.owner_a)
            base = org(a.slug)
            asked = client.get(f"{base}/approvals", params={"status": "pending"}, headers=h).json()
            [first] = [x for x in asked["items"] if x["run_id"] == str(a.run)]
            ok = client.post(f"{base}/approvals/{first['id']}/decide", headers=h,
                             json={"decision": "approve"})  # fmt: skip
            assert ok.status_code == 200, ok.text
        assert writing.wait(60), "the approved write never started"
        with w._lock:
            [job] = list(w._inflight.values())
        wedged = _thread(f"shifde-run-{job.id}")
        assert job.run_id == a.run and not job.abandoned.is_set()
        with owner_db.session() as o:  # the run is now well past its deadline + `wedged_after`
            o.execute(text("UPDATE shifde.runs SET deadline_at = now() - interval '5 seconds' "
                           "WHERE id = :r"), {"r": a.run})  # fmt: skip
            o.commit()
        # 1. the heartbeat abandons the job: it no longer takes the only slot
        assert job.abandoned.wait(60), "the wedged job was never abandoned"
        assert job.lease_lost.is_set()
        with w._lock:
            assert w._inflight.get(job.id) is job and w._active() == []
        with owner_db.session() as o:  # A's second run becomes claimable
            o.execute(text("UPDATE shifde.jobs SET run_after = now() WHERE id = :j"),
                      {"j": second_job})  # fmt: skip
            o.commit()
        # 2. this worker's own reaper settles the wedged run as an unknown outcome
        _until(lambda: _status(owner_db, a.run) == "timed_out", 60)
        row = _q(owner_db, "SELECT status, error_class FROM shifde.runs WHERE id = :r",
                 r=a.run)[0]  # fmt: skip
        assert row == ("timed_out", UNKNOWN) and row[0] != "cancelled"
        assert _q(owner_db, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r",
                  r=a.run) == [("indeterminate",)]  # fmt: skip
        assert ("pending", UNKNOWN) in _q(owner_db, "SELECT status, reason FROM shifde.approvals "
                                          "WHERE run_id = :r", r=a.run)  # fmt: skip
        # 3. the same worker claims and completes the second run while the thread is blocked
        _until(lambda: _status(owner_db, second) == "completed", 60)
        assert [ctx.run["id"] for ctx in Script.seen].count(second) == 1
        done = "SELECT status, lease_owner FROM shifde.jobs WHERE id = :j"
        _until(lambda: _q(owner_db, done, j=second_job) == [("done", "w-m3-slot")])
        assert wedged.is_alive() and not release.is_set()
        # 4. the worker stops without waiting for the blocked thread
        w.request_stop()
        loop.join(60)
        assert not loop.is_alive(), "the worker waited for the wedged thread"
        assert wedged.is_alive() and not release.is_set()
        before = _run_rows(owner_db, a.run)
        # the stuck call returns late: fenced, nothing it touches changes, nothing is resent
        release.set()
        wedged.join(60)
        assert not wedged.is_alive()
        _until(lambda: job.id not in w._inflight)
        assert _run_rows(owner_db, a.run) == before
        assert len(deliveries) == 1, "the write was resent"
        assert _status(owner_db, a.run) == "timed_out"
        assert _state(owner_db, b.tenant) == b_before  # the worker and its reaper left B alone
    finally:
        release.set()


# ------------------------------------------------------------------ caps on abandoned threads
def _wedge_writes(
    a: C, script: type[Script], monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Event, threading.Event, list[str]]:
    """A's run delivers an approved write whose `invoke_once` blocks until `release` is set;
    every other run completes at once. Returns (writing, release, deliveries)."""
    writing, release = threading.Event(), threading.Event()
    deliveries: list[str] = []

    def invoke_once(conns: object, spec: object, args: object, **kw: Any) -> object:
        deliveries.append(kw["idempotency_key"])
        writing.set()
        release.wait(300)  # wedged until the test lets it return
        return {"written": True}

    monkeypatch.setattr(tool_runtime, "invoke_once", invoke_once)
    call = steps.ToolCall(a.tool, {"item": "x"}, "w1", node_id="w1")
    policy = steps.ToolPolicy(frozenset({"write_non_idempotent"}), None,
                              frozenset({a.tool.version_id}))  # fmt: skip
    script.behave = staticmethod(
        lambda ctx: steps.run_tool(ctx, call, policy) if ctx.run["id"] == a.run else {"ok": 1}
    )
    return writing, release, deliveries


def _approve_and_abandon(
    w: Worker,
    a: C,
    people: People,
    owner_db: Database,
    settings: Settings,
    writing: threading.Event,
) -> _Job:
    """Approve A's write, wait until it blocks, move A's run past its deadline and wait until the
    heartbeat abandons the job; return it."""
    _until(lambda: _status(owner_db, a.run) == "waiting_for_approval")
    with runtime_client(settings) as client:
        h = as_user(client, people.owner_a)
        base = org(a.slug)
        asked = client.get(f"{base}/approvals", params={"status": "pending"}, headers=h).json()
        [first] = [x for x in asked["items"] if x["run_id"] == str(a.run)]
        ok = client.post(f"{base}/approvals/{first['id']}/decide", headers=h,
                         json={"decision": "approve"})  # fmt: skip
        assert ok.status_code == 200, ok.text
    assert writing.wait(60), "the approved write never started"
    with w._lock:
        [job] = [j for j in w._inflight.values() if j.run_id == a.run]
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.runs SET deadline_at = now() - interval '5 seconds' "
                       "WHERE id = :r"), {"r": a.run})  # fmt: skip
        o.commit()
    assert job.abandoned.wait(60), "the wedged job was never abandoned"
    return job


def _later_job(o: Session, tenant: UUID, run: UUID | None = None) -> tuple[UUID, UUID]:
    """A `run.advance` job of `tenant` claimable in an hour, for a new run unless `run` is
    given; returns (run, job)."""
    if run is None:
        user = add_person(o, tenant, "later", "tenant_administrator")
        run = add_run(o, tenant, initiated_by_principal_id=user)
    job = uuid7()
    insert(o, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=run,
           dedupe_key=f"run:{run}:{job}", run_after=utcnow() + timedelta(hours=1))  # fmt: skip
    return run, job


def _claimable(owner_db: Database, job: UUID) -> None:
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.jobs SET run_after = now() WHERE id = :j"), {"j": job})
        o.commit()


def _settled_unknown(owner_db: Database, run: UUID) -> None:
    """`run` ended `timed_out` with `indeterminate_side_effect` (not cancelled), its one
    invocation indeterminate."""
    row = _q(owner_db, "SELECT status, error_class FROM shifde.runs WHERE id = :r", r=run)[0]
    assert row == ("timed_out", UNKNOWN) and row[0] != "cancelled"
    assert _q(owner_db, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r",
              r=run) == [("indeterminate",)]  # fmt: skip


def test_a_client_at_its_abandoned_share_has_its_claimed_jobs_handed_back(
    two: tuple[C, C],
    people: People,
    owner_db: Database,
    settings: Settings,
    script: type[Script],
    running: Callable[..., Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #44, per-client share 1 of 2: once A's write is abandoned, A's next job is handed
    back and never run here, B's run completes on the same worker, and A's wedged run still ends
    an unknown outcome after one delivery."""
    a, b = two
    with owner_db.session() as o:
        second, second_job = _later_job(o, a.tenant)
        _, b_job = _later_job(o, b.tenant, b.run)
        o.commit()
    writing, release, deliveries = _wedge_writes(a, script, monkeypatch)
    caps = {"worker_max_abandoned": 2, "worker_max_abandoned_per_client": 1}
    w = running("w-share", update=caps, concurrency=2, reap_seconds=1.0)
    assert (w.max_abandoned, w.max_abandoned_per_client) == (2, 1)
    w.wedged_after = timedelta(seconds=1)
    released: list[tuple[UUID, bool]] = []  # (job, its `run_after` pushed out)
    release_claim = w._release_claim
    pushed = "SELECT status = 'queued' AND run_after > now() FROM shifde.jobs WHERE id = :j"

    def spy(job: _Job) -> None:
        release_claim(job)
        released.append((job.id, _q(owner_db, pushed, j=job.id) == [(True,)]))

    monkeypatch.setattr(w, "_release_claim", spy)
    try:
        job = _approve_and_abandon(w, a, people, owner_db, settings, writing)
        wedged = _thread(f"shifde-run-{job.id}")
        # the blocked call holds no pooled connection (the worker's pool sizing relies on it)
        ctx = [c for c in Script.seen if c.run["id"] == a.run][-1]
        assert not ctx.session.in_transaction()
        _until(lambda: w.db.engine.pool.checkedout() == 0)  # type: ignore[attr-defined]
        # 1. A's next job is claimed and handed back with its `run_after` pushed out
        _claimable(owner_db, second_job)
        _until(lambda: (second_job, True) in released)
        # 2. a B run claimed afterwards completes on this worker
        _claimable(owner_db, b_job)
        done = "SELECT status, lease_owner FROM shifde.jobs WHERE id = :j"
        _until(lambda: _q(owner_db, done, j=b_job) == [("done", "w-share")], 60)
        assert _status(owner_db, b.run) == "completed"
        assert all(j != b_job for j, _ in released)
        # 3. A's wedged run is settled by the reaper as an unknown outcome, never cancelled
        _until(lambda: _status(owner_db, a.run) == "timed_out", 60)
        _settled_unknown(owner_db, a.run)
        assert wedged.is_alive() and not release.is_set()
        assert _status(owner_db, second) == "pending"
        assert second not in [c.run["id"] for c in Script.seen]
        assert _q(owner_db, done, j=second_job)[0][0] != "done"
        assert w.exit_code == 0  # a client's share never stops the worker
    finally:
        release.set()
    assert len(deliveries) == 1, "the write was resent"


def test_a_worker_at_its_abandoned_cap_stops_claiming_and_exits_1(
    two: tuple[C, C],
    people: People,
    owner_db: Database,
    settings: Settings,
    script: type[Script],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #44, per-worker cap 1: once the write is abandoned the worker claims nothing more
    and `Worker.run()` returns `EXIT_ABANDONED_CAP` while the thread is still blocked; a reaper
    settles the wedged run as an unknown outcome, never cancelled."""
    a, b = two
    with owner_db.session() as o:
        _, b_job = _later_job(o, b.tenant, b.run)
        o.commit()
    writing, release, deliveries = _wedge_writes(a, script, monkeypatch)
    w = _worker(settings.model_copy(update={"worker_max_abandoned": 1}), "w-cap", concurrency=2)
    assert (w.max_abandoned, w.max_abandoned_per_client) == (1, 1)  # the share is capped too
    w.wedged_after = timedelta(seconds=1)
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-w-cap")
    loop.start()
    try:
        job = _approve_and_abandon(w, a, people, owner_db, settings, writing)
        wedged = _thread(f"shifde-run-{job.id}")
        _claimable(owner_db, b_job)  # claimable once the cap is reached: never run here
        loop.join(60)
        assert not loop.is_alive(), "the worker waited for the wedged thread"
        assert exits == [worker_mod.EXIT_ABANDONED_CAP] == [1]
        assert wedged.is_alive() and not release.is_set()
        assert _q(owner_db, "SELECT status, lease_owner FROM shifde.jobs WHERE id = :j",
                  j=b_job) == [("queued", None)]  # fmt: skip
        assert _status(owner_db, b.run) == "pending"
        assert b.run not in [c.run["id"] for c in Script.seen]
        assert _q(owner_db, "SELECT status FROM shifde.workers WHERE id = :w",
                  w="w-cap") == [("stopped",)]  # fmt: skip
        # the wedged run's lease expires; another worker's reaper settles it as an unknown outcome
        reaper = _worker(settings, "w-cap-reaper")
        try:
            _until(lambda: reaper._reap() or _status(owner_db, a.run) == "timed_out", 60)
        finally:
            reaper.db.dispose()
        _settled_unknown(owner_db, a.run)
    finally:
        release.set()
        loop.join(60)
    assert len(deliveries) == 1, "the write was resent"


def test_shifde_worker_exits_with_the_workers_exit_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class Stub:
        def __init__(self, settings: object, **kw: Any) -> None:
            built.append(kw)

        def run(self) -> int:
            return worker_mod.EXIT_ABANDONED_CAP

    monkeypatch.setattr(worker_mod, "Worker", Stub)
    monkeypatch.setattr(worker_mod, "settings_or_exit", lambda: settings)
    monkeypatch.setattr(worker_mod, "refuse_unsafe_role", lambda settings: None)
    monkeypatch.setattr(worker_mod, "configure_logging", lambda *a: None)
    monkeypatch.setattr(worker_mod.tracing, "configure_from_settings", lambda *a: None)
    monkeypatch.setattr(worker_mod, "_install_signals", lambda worker: None)
    assert worker_mod.run(["--concurrency", "2"]) == worker_mod.EXIT_ABANDONED_CAP == 1
    assert built[0]["concurrency"] == 2


# ------------------------------------------------------------------ run-less jobs (ARCH M2)
@dataclass(frozen=True)
class T:
    tenant: UUID
    connection: UUID
    job: UUID
    test_id: UUID


def _test_job(o: Session, tenant: UUID, server: RestTestServer) -> T:
    """A queued `connection.test` job of `tenant` for a REST connection to `server`, requested by
    a client administrator (the payload the connections route writes)."""
    user = add_person(o, tenant, "tester", "tenant_administrator")
    cid, job, test_id = uuid7(), uuid7(), uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, name, connection_type, adapter_key, "
            "config, auth_method, access_mode) VALUES (:i, :t, :n, 'rest', 'rest.http', "
            "CAST(:c AS jsonb), 'none', 'read')"
        ),
        {"i": cid, "t": tenant, "n": f"conn-{cid.hex}",
         "c": json.dumps({"base_url": server.base_url, "health_path": "/health"})},
    )  # fmt: skip
    payload = {"connection_id": str(cid), "test_id": str(test_id), "principal_id": str(user),
               "principal_type": "user", "api_key_id": None}  # fmt: skip
    o.execute(
        text(
            "INSERT INTO shifde.jobs (id, tenant_id, kind, payload, dedupe_key) "
            "VALUES (:i, :t, :k, CAST(:p AS jsonb), :dk)"
        ),
        {"i": job, "t": tenant, "k": connections.TEST_JOB_KIND, "p": json.dumps(payload),
         "dk": f"connection-test:{cid}"},
    )  # fmt: skip
    return T(tenant, cid, job, test_id)


@pytest.fixture
def server() -> Iterator[RestTestServer]:
    with RestTestServer() as s:
        yield s


def _tests_of(owner_db: Database, tenant: UUID) -> list[tuple[Any, ...]]:
    return _q(owner_db, "SELECT id, connection_id, outcome, tested_by FROM shifde.connection_tests "
              "WHERE tenant_id = :t ORDER BY id", t=tenant)  # fmt: skip


def test_a_worker_loop_runs_a_connection_test_job_in_its_own_client(
    world: World, owner_db: Database, server: RestTestServer, running: Callable[..., Worker]
) -> None:
    with owner_db.session() as o:
        t = _test_job(o, world.a.tenant, server)
        o.commit()
    b_before = _state(owner_db, world.b.tenant)
    running("w-conn")
    job = "SELECT status, last_error, run_id FROM shifde.jobs WHERE id = :j"
    _until(lambda: _q(owner_db, job, j=t.job) == [("done", None, None)])
    [(test_id, cid, outcome, _)] = _tests_of(owner_db, world.a.tenant)
    assert (test_id, cid, outcome) == (t.test_id, t.connection, "pass")
    assert any(r["path"] == "/health" for r in server.requests)
    health = "SELECT health_status FROM shifde.connections WHERE id = :c"
    assert _q(owner_db, health, c=t.connection) == [("healthy",)]
    assert _tests_of(owner_db, world.b.tenant) == []
    assert _state(owner_db, world.b.tenant) == b_before


def _provider_jobs(
    o: Session,
    tenant: UUID,
    *,
    provider_tenant: UUID | None = None,
    principal_tenant: UUID | None = None,
) -> tuple[UUID, UUID, UUID]:
    """A fake model provider of `tenant` and a queued `model.health_check` and `model.discover`
    job for it, requested by a client administrator (the payload the models route writes).
    `provider_tenant` / `principal_tenant` put the provider / the requester in another client
    instead (a forged job row)."""
    user = add_person(o, principal_tenant or tenant, "checker", "tenant_administrator")
    cid, pid, health, discover = uuid7(), uuid7(), uuid7(), uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, name, connection_type, adapter_key, "
            "status) VALUES (:i, :t, :n, 'model_api', 'fake', 'active')"
        ),
        {"i": cid, "t": provider_tenant or tenant, "n": f"conn-{cid.hex}"},
    )
    o.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, connection_id) "
            "VALUES (:i, :t, :n, 'fake', :c)"
        ),
        {"i": pid, "t": provider_tenant or tenant, "n": f"prov-{pid.hex}", "c": cid},
    )
    payload = json.dumps({"provider_id": str(pid), "principal_id": str(user),
                          "principal_type": "user", "api_key_id": None})  # fmt: skip
    for job, kind in ((health, models_admin.HEALTH_JOB_KIND),
                      (discover, models_admin.DISCOVER_JOB_KIND)):  # fmt: skip
        o.execute(
            text(
                "INSERT INTO shifde.jobs (id, tenant_id, kind, payload, dedupe_key) "
                "VALUES (:i, :t, :k, CAST(:p AS jsonb), :dk)"
            ),
            {"i": job, "t": tenant, "k": kind, "p": payload,
             "dk": f"{models_admin.JOB_PREFIX[kind]}:{pid}"},
        )  # fmt: skip
    return pid, health, discover


def test_a_worker_loop_runs_provider_jobs_in_their_own_client(
    world: World, owner_db: Database, running: Callable[..., Worker]
) -> None:
    """ARCH M2 (issue #47): a `model.health_check` and a `model.discover` job run to `done` in
    their own client and write their result events there; the other client is untouched."""
    with owner_db.session() as o:
        pid, health, discover = _provider_jobs(o, world.a.tenant)
        o.commit()
    b_before = _state(owner_db, world.b.tenant)
    running("w-model")
    job = "SELECT status, last_error, run_id FROM shifde.jobs WHERE id = :j"
    _until(lambda: _q(owner_db, job, j=health) == [("done", None, None)])
    _until(lambda: _q(owner_db, job, j=discover) == [("done", None, None)])
    provider = "SELECT health_status FROM shifde.model_providers WHERE id = :p"
    assert _q(owner_db, provider, p=pid) == [("healthy",)]
    events = ("SELECT tenant_id, action, correlation_id FROM shifde.audit_events "
              "WHERE target_id = :p ORDER BY action")  # fmt: skip
    assert _q(owner_db, events, p=str(pid)) == [
        (world.a.tenant, "model.provider.health_checked", f"model-health-check-{health}"),
        (world.a.tenant, "model.provider.models_discovered", f"model-discover-{discover}"),
    ]
    assert _state(owner_db, world.b.tenant) == b_before


_HEALTH_COLUMNS = ("SELECT health_status, health_checked_at, health_detail "
                  "FROM shifde.model_providers WHERE id = :p")  # fmt: skip


def test_a_forged_provider_job_naming_another_clients_provider_ends_dead(
    world: World, owner_db: Database, running: Callable[..., Worker]
) -> None:
    """Issue #134: `model.health_check` / `model.discover` jobs of client A whose payload names an
    A administrator but B's provider end `dead` (`not_found`, B's provider is not visible in A);
    B's provider is never checked and nothing of B changes."""
    with owner_db.session() as o:
        pid, health, discover = _provider_jobs(o, world.a.tenant, provider_tenant=world.b.tenant)
        o.commit()
    b_before = _state(owner_db, world.b.tenant)
    columns = _q(owner_db, _HEALTH_COLUMNS, p=pid)
    running("w-forged-provider")
    job = "SELECT status, last_error FROM shifde.jobs WHERE id = :j"
    _until(lambda: _q(owner_db, job, j=health) == [("dead", "not_found")])
    _until(lambda: _q(owner_db, job, j=discover) == [("dead", "not_found")])
    assert _q(owner_db, _HEALTH_COLUMNS, p=pid) == columns
    events = "SELECT tenant_id, action FROM shifde.audit_events WHERE target_id = :p"
    assert _q(owner_db, events, p=str(pid)) == []  # neither in A nor in B
    assert _state(owner_db, world.b.tenant) == b_before


def test_a_forged_provider_job_naming_another_clients_principal_ends_dead(
    world: World, owner_db: Database, running: Callable[..., Worker]
) -> None:
    """Issue #134: provider jobs of client A naming A's provider but a user of client B end
    `dead` (`authz_denied`, the principal cannot be re-derived in A); A's provider is never
    checked and nothing of B changes."""
    with owner_db.session() as o:
        pid, health, discover = _provider_jobs(o, world.a.tenant, principal_tenant=world.b.tenant)
        o.commit()
    b_before = _state(owner_db, world.b.tenant)
    columns = _q(owner_db, _HEALTH_COLUMNS, p=pid)
    running("w-forged-principal")
    job = "SELECT status, last_error FROM shifde.jobs WHERE id = :j"
    _until(lambda: _q(owner_db, job, j=health) == [("dead", "authz_denied")])
    _until(lambda: _q(owner_db, job, j=discover) == [("dead", "authz_denied")])
    assert _q(owner_db, _HEALTH_COLUMNS, p=pid) == columns
    events = "SELECT action FROM shifde.audit_events WHERE target_id = :p AND action = ANY(:a)"
    actions = list(models_admin.JOB_ACTION.values())
    assert _q(owner_db, events, p=str(pid), a=actions) == []
    assert _state(owner_db, world.b.tenant) == b_before


def test_a_job_of_an_unknown_kind_ends_dead_and_is_never_run(
    world: World,
    owner_db: Database,
    running: Callable[..., Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(worker_mod.lifecycle, "advance", lambda *a, **kw: ran.append(a) or "done")
    monkeypatch.setattr(worker_mod.connections, "run_test_job", lambda *a: ran.append(a))
    monkeypatch.setattr(worker_mod.connections, "run_invoke_job", lambda *a: ran.append(a))
    monkeypatch.setattr(worker_mod.models_admin, "run_provider_job", lambda *a: ran.append(a))
    job = uuid7()
    with owner_db.session() as o:
        insert(o, "jobs", id=job, tenant_id=world.a.tenant, kind="mystery.kind",
               dedupe_key=f"mystery:{job}")  # fmt: skip
        o.commit()
    running("w-kind")
    done = "SELECT status, last_error FROM shifde.jobs WHERE id = :j"
    _until(lambda: _q(owner_db, done, j=job) == [("dead", worker_mod.UNKNOWN_KIND)])
    assert ran == []


def test_a_connection_invoke_job_is_dispatched_in_its_own_client(
    world: World,
    owner_db: Database,
    running: Callable[..., Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #190: a `connection.invoke` job runs `connections.service.run_invoke_job` with the
    claimed row's client and job id, and ends `done` when it returns None."""
    ran: list[tuple[object, ...]] = []
    monkeypatch.setattr(worker_mod.connections, "run_invoke_job", lambda *a: ran.append(a))
    job = uuid7()
    with owner_db.session() as o:
        insert(o, "jobs", id=job, tenant_id=world.a.tenant, kind=connections.INVOKE_JOB_KIND,
               dedupe_key=f"connection-invoke:{job}")  # fmt: skip
        o.commit()
    running("w-invoke")
    done = "SELECT status, last_error FROM shifde.jobs WHERE id = :j"
    _until(lambda: _q(owner_db, done, j=job) == [("done", None)])
    assert [(a[2], a[3]) for a in ran] == [(world.a.tenant, job)]


@pytest.mark.parametrize("mode", MODES)
def test_a_connection_test_job_naming_another_clients_id_touches_nothing_of_that_client(
    mode: str,
    world: World,
    owner_db: Database,
    settings: Settings,
    owner_url: str,
    server: RestTestServer,
) -> None:
    """A run-less job carrying A's client id with B's job id (a forged claim row): running it,
    heartbeating it and finishing it (done, dead, error) change none of B's rows and never test
    B's connection; B's own client id does. As `shifde_app` and as the schema owner."""
    with owner_db.session() as o:
        b = _test_job(o, world.b.tenant, server)
        o.execute(text("UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-y', "
                       "lease_expires_at = now() + interval '1 hour' WHERE id = :j"),
                  {"j": b.job})  # fmt: skip
        o.commit()
    before, tests_before = _state(owner_db, b.tenant), _tests_of(owner_db, b.tenant)
    w = _worker(settings, "w-y")
    if mode == "schema-owner":
        _on_owner(w, owner_url)
    try:
        forged = _Job(b.job, world.a.tenant, None, connections.TEST_JOB_KIND)
        w._inflight[forged.id] = forged
        w._beat()
        assert forged.lease_lost.is_set()  # not B's job in A's client: no heartbeat
        assert w._execute(forged) == "dead" and forged.error == "not_found"
        for outcome in ("dead", "done", "error", "lease_lost"):
            w._settle_job(forged, outcome)
            assert _state(owner_db, b.tenant) == before, outcome
        assert _tests_of(owner_db, b.tenant) == tests_before == []
        assert server.requests == []  # B's connection was never driven
        own = _Job(b.job, b.tenant, None, connections.TEST_JOB_KIND)  # not vacuous
        w._inflight = {own.id: own}
        w._beat()
        assert not own.lease_lost.is_set()
        assert w._execute(own) == "done" and own.error is None
        w._settle_job(own, "done")
        assert _q(owner_db, "SELECT status FROM shifde.jobs WHERE id = :j", j=b.job) == [("done",)]
        assert [r[0] for r in _tests_of(owner_db, b.tenant)] == [b.test_id]
    finally:
        w.db.dispose()
