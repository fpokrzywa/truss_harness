"""Runtime part 10 (RT-10): `lifecycle.py`, one advance of a run.

Behaviour: the advance claims the run under a new lease token with `lease_job_id` set to the job
it was called with, hands the executors the claimed row re-read with the client id, persists the
outcome (completed with an output artifact of the run, waiting, failed, cancelled, timed out),
times out a run (or a child, whose deadline is inherited) whose deadline passed before it started,
writes `recovered` only after a lost or handed-back lease (E2E m3) and does not move a run the
executor already parked (`Waiting(transitioned=True)`). C1 (B-2151): an exception while a write is
in flight ends the run with `error_class=indeterminate_side_effect`, the write `indeterminate` and
an unknown-outcome approval, never a plain `failed/internal`.

Isolation, as `shifde_app` (`advance` on the app database) and as the schema owner (`advance_in`
on an owner session, no row-level security): a job of client B (or of another run of A, or an
unknown id) drives none of A's runs; B's run named under A's client answers like an unknown run;
a member of B only cannot run A's run; advancing A's run changes none of B's runs, jobs, steps,
invocations, approvals, events, artifacts or audit rows. A privileged database is refused for the
advance and for `RunContext.db`.

The SETH tests that call `lifecycle` drive the API and the worker (`World`, `TestClient`): deferred
(carry_forward "Runtime part 10"); the tests below are direct counterparts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.dbrole import UnsafeDatabaseRole
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import context as context_mod
from shifde.runtime import lifecycle, state
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.governance import PolicyBudgetHooks
from tests._support.runs import add_run
from tests._support.tenants import add_tenants
from tests.build.env_support import add_published
from tests.build.service_support import REGISTRY, add_person
from tests.runtime.conftest import insert
from tests.runtime.execution import client_state

MODES = ["app-role", "schema-owner"]
WORKER = "w-life"


@dataclass(frozen=True)
class C:
    tenant: UUID
    user: UUID
    run: UUID  # pending root agent run initiated by `user`
    job: UUID  # leased by WORKER for `run`
    tool: UUID  # published tool version


def add_job(o: Session, tenant: UUID, run: UUID, *, status: str = "leased",
            owner: str | None = WORKER) -> UUID:  # fmt: skip
    job = uuid7()
    insert(o, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=run,
           dedupe_key=f"run:{run}:{job}", status=status, lease_owner=owner,
           lease_expires_at=utcnow() + timedelta(hours=1) if status == "leased" else None,
           traceparent=f"00-{run.hex}-{'2' * 16}-01")  # fmt: skip
    return job


def write_in_flight(o: Session, tenant: UUID, run: UUID, tool: UUID) -> None:
    invocation = uuid7()
    insert(o, "tool_invocations", id=invocation, tenant_id=tenant, run_id=run, call_ref="w1",
           tool_version_id=tool, side_effect_class="write_non_idempotent",
           idempotency_key=f"k-{invocation}", status="started", request_hash="h",
           arguments='{"item": "x"}')  # fmt: skip
    insert(o, "run_steps", id=uuid7(), tenant_id=tenant, run_id=run, seq=1, kind="tool_call",
           name="t", status="running", tool_invocation_id=invocation,
           tool_version_id=tool)  # fmt: skip


def _seed(o: Session, tenant: UUID) -> C:
    user = add_person(o, tenant, "runner", "tenant_administrator")
    run = add_run(o, tenant, initiated_by_principal_id=user)
    _, tool = add_published(o, tenant, "tools", f"tool-{uuid7().hex}", {})
    write_in_flight(o, tenant, run, tool)
    # the write of the seed is in flight only for the C1 test; the others settle it as completed
    o.execute(text("UPDATE shifde.tool_invocations SET status = 'succeeded' WHERE run_id = :r"),
              {"r": run})  # fmt: skip
    o.execute(text("UPDATE shifde.run_steps SET status = 'succeeded' WHERE run_id = :r"),
              {"r": run})  # fmt: skip
    return C(tenant, user, run, add_job(o, tenant, run), tool)


@pytest.fixture
def two(owner_db: Database) -> tuple[C, C]:
    a, b = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, a, b)
        pair = _seed(o, a), _seed(o, b)
        o.commit()
    return pair


class Fake:
    """Stands in for every executor: `behave(ctx)` is the test's script."""

    behave: Callable[[RunContext], Any] = staticmethod(lambda ctx: {"ok": 1})
    seen: list[RunContext] = []

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def run(self) -> Any:
        Fake.seen.append(self.ctx)
        return Fake.behave(self.ctx)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[Fake]:
    Fake.seen = []
    Fake.behave = staticmethod(lambda ctx: {"ok": 1})
    for name in ("AgentRunner", "GraphRunner", "PipelineRunner", "OntologyActionRunner"):
        monkeypatch.setattr(lifecycle, name, Fake)
    return Fake


@dataclass
class Advance:
    mode: str
    go: Callable[..., str]

    def __call__(self, tenant: UUID, run: UUID, job: UUID, worker: str = WORKER) -> str:
        return self.go(tenant, run, job, worker)


@pytest.fixture(params=MODES)
def advance(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database, settings: Settings
) -> Advance:
    """`advance(tenant, run, job)` as the worker does it: on the app database, or on a schema
    owner session (no row-level security: the code's own client predicates are all there is)."""
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)

    def go(tenant: UUID, run: UUID, job: UUID, worker: str = WORKER) -> str:
        args = (settings, runtime, PolicyBudgetHooks(), tenant, run, worker, threading.Event(),
                threading.Event(), job)  # fmt: skip
        if request.param == "app-role":
            return lifecycle.advance(app_db, *args)
        s = owner_db.session()
        db.init_context(s, "test")
        try:
            return lifecycle.advance_in(s, None, *args)
        finally:
            s.close()

    return Advance(request.param, go)


def _state(o: Session, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    """`client_state` plus the client's run artifacts."""
    arts = _q(
        o,
        "SELECT id, run_id, kind FROM shifde.run_artifacts WHERE tenant_id = :t ORDER BY id",
        t=tenant,
    )
    return (*client_state(o, tenant), tuple(arts))


def _q(o: Session, sql: str, **params: Any) -> list[Any]:  # Any: row values
    o.rollback()
    return [tuple(r) for r in o.execute(text(sql), params).all()]


def _run(o: Session, run: UUID) -> dict[str, Any]:  # Any: run row
    o.rollback()
    return dict(
        o.execute(text("SELECT * FROM shifde.runs WHERE id = :r"), {"r": run}).mappings().one()
    )


def _events(o: Session, run: UUID) -> list[str]:
    return [
        r[0]
        for r in _q(o, "SELECT type FROM shifde.run_events WHERE run_id = :r ORDER BY seq", r=run)
    ]


# ------------------------------------------------------------------ own run
def test_an_advance_completes_its_own_run_and_leaves_the_other_client_alone(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database, app_db: Database
) -> None:
    a, b = two
    seen: dict[str, Any] = {}

    def behave(ctx: RunContext) -> Any:
        seen["run"] = dict(ctx.run)
        seen["db"] = ctx.db
        held = repo.get_run(ctx.session, a.tenant, a.run)
        assert held is not None
        seen["lease_job_id"] = held["lease_job_id"]
        ctx.session.commit()
        return {"answer": 42}

    fake.behave = staticmethod(behave)
    with owner_db.session() as o:
        b_before = _state(o, b.tenant)
    assert advance(a.tenant, a.run, a.job) == "done"
    # the claimed row re-read with the client id, the lease bound to the claim's own job
    assert seen["run"]["tenant_id"] == a.tenant and seen["run"]["id"] == a.run
    assert seen["run"]["status"] == "running" and seen["lease_job_id"] == a.job
    assert seen["db"] is (app_db if advance.mode == "app-role" else None)
    with owner_db.session() as o:
        row = _run(o, a.run)
        assert (row["status"], row["lease_token"], row["lease_job_id"]) == ("completed", None, None)
        assert _q(o, "SELECT tenant_id, run_id, kind, content FROM shifde.run_artifacts "
                     "WHERE id = :i", i=row["output_ref"]) == [
            (a.tenant, a.run, "output", {"output": {"answer": 42}})
        ]  # fmt: skip
        assert "recovered" not in _events(o, a.run)
        actions = [
            r[0]
            for r in _q(
                o,
                "SELECT action FROM shifde.audit_events WHERE target_id = :r ORDER BY occurred_at",
                r=str(a.run),
            )
        ]
        assert actions.count("run.started") == 1 and actions.count("run.completed") == 1
        assert _state(o, b.tenant) == b_before


# ------------------------------------------------------------------ jobs and ids of B
def test_a_job_of_another_client_or_run_or_an_unknown_id_drives_nothing(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    with owner_db.session() as o:
        other_run = add_run(o, a.tenant, initiated_by_principal_id=a.user)
        other_job = add_job(o, a.tenant, other_run)
        o.commit()
        before = _state(o, a.tenant), _state(o, b.tenant)
    for tenant, run, job in (
        (a.tenant, a.run, b.job),  # B's job for A's run
        (a.tenant, b.run, b.job),  # B's run and job named under A's client
        (a.tenant, b.run, a.job),
        (a.tenant, a.run, other_job),  # A's job of another run
        (a.tenant, uuid7(), uuid7()),  # unknown ids answer alike
        (a.tenant, a.run, uuid7()),
    ):
        assert advance(tenant, run, job) == "skipped"
    assert fake.seen == []
    with owner_db.session() as o:
        assert (_state(o, a.tenant), _state(o, b.tenant)) == before


def test_a_job_not_leased_by_this_worker_claims_nothing(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    with owner_db.session() as o:
        queued = add_job(o, a.tenant, a.run, status="queued", owner=None)
        o.commit()
        before = _state(o, a.tenant), _state(o, b.tenant)
    assert advance(a.tenant, a.run, queued) == "skipped"
    assert advance(a.tenant, a.run, a.job, "another-worker") == "skipped"
    assert fake.seen == []
    with owner_db.session() as o:
        assert (_state(o, a.tenant), _state(o, b.tenant)) == before


def test_b_job_never_recovers_a_running_run_of_a_but_its_own_job_does(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'running', lease_token = :t, state_version = 2, "
                "attempt_count = 1 WHERE id = :r"
            ),
            {"t": uuid7(), "r": a.run},
        )
        o.commit()
        before = _state(o, a.tenant), _state(o, b.tenant)
    assert advance(a.tenant, a.run, b.job) == "skipped"
    with owner_db.session() as o:
        assert (_state(o, a.tenant), _state(o, b.tenant)) == before
    assert advance(a.tenant, a.run, a.job) == "done"
    with owner_db.session() as o:
        assert _run(o, a.run)["status"] == "completed"
        assert _events(o, a.run).count("recovered") == 1  # recovered from an abandoned lease
        assert _state(o, b.tenant) == before[1]


def test_recovered_is_written_after_a_lost_lease_only_not_after_a_wait(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    with owner_db.session() as o:
        woken = add_run(o, a.tenant, initiated_by_principal_id=a.user, attempt_count=1)
        reaped = add_run(o, a.tenant, initiated_by_principal_id=a.user, attempt_count=1)
        for run, type_, frm in ((woken, "state_change", "waiting_for_approval"),
                                (reaped, "lease_lost", "running")):  # fmt: skip
            insert(o, "run_events", id=uuid7(), tenant_id=a.tenant, run_id=run, seq=1,
                   type=type_, from_status=frm, to_status="pending")  # fmt: skip
            o.execute(text("UPDATE shifde.runs SET event_seq = 1 WHERE id = :r"), {"r": run})
        jobs = add_job(o, a.tenant, woken), add_job(o, a.tenant, reaped)
        o.commit()
    assert advance(a.tenant, woken, jobs[0]) == "done"
    assert advance(a.tenant, reaped, jobs[1]) == "done"
    with owner_db.session() as o:
        assert "recovered" not in _events(o, woken)  # E2E m3: an ordinary re-advance
        assert _events(o, reaped).count("recovered") == 1


# ------------------------------------------------------------------ principal of B
def test_a_member_of_the_other_client_only_cannot_run_a_run(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    with owner_db.session() as o:
        run = add_run(o, a.tenant, initiated_by_principal_id=b.user)
        job = add_job(o, a.tenant, run)
        o.commit()
        b_before = _state(o, b.tenant)
    assert advance(a.tenant, run, job) == "done"
    assert fake.seen == []
    with owner_db.session() as o:
        row = _run(o, run)
        assert (row["status"], row["error_class"]) == ("failed", state.AUTHZ_DENIED)
        assert _state(o, b.tenant) == b_before


# ------------------------------------------------------------------ deadline and parent
def test_a_child_whose_inherited_deadline_passed_is_timed_out_and_audited(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    past = utcnow() - timedelta(seconds=1)
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.runs SET deadline_at = :d WHERE id IN (:a, :b)"),
                  {"d": past, "a": a.run, "b": b.run})  # fmt: skip
        child = add_run(o, a.tenant, initiated_by_principal_id=a.user, parent_run_id=a.run,
                        root_run_id=a.run, depth=1, deadline_at=past)  # fmt: skip
        job = add_job(o, a.tenant, child)
        o.commit()
        b_before = _state(o, b.tenant)
    assert advance(a.tenant, child, job) == "done"
    assert advance(a.tenant, b.run, b.job) == "skipped"  # B's late run is not A's to time out
    with owner_db.session() as o:
        row = _run(o, child)
        assert (row["status"], row["error_class"]) == ("timed_out", state.TIMEOUT)
        assert _q(
            o,
            "SELECT count(*) FROM shifde.audit_events WHERE action = 'run.timed_out' "
            "AND target_id = :r AND tenant_id = :t",
            r=str(child),
            t=a.tenant,
        ) == [(1,)]
        assert _state(o, b.tenant) == b_before


def test_a_child_of_an_ended_pipeline_is_cancelled_when_claimed(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two
    with owner_db.session() as o:
        pipe, version = add_published(o, a.tenant, "pipelines", f"p-{uuid7().hex}", {})
        parent = add_run(
            o,
            a.tenant,
            kind="pipeline",
            pipeline_id=pipe,
            pipeline_version_id=version,
            status="failed",
            finished_at=utcnow(),
        )
        child = add_run(o, a.tenant, initiated_by_principal_id=a.user, parent_run_id=parent,
                        root_run_id=parent, depth=1)  # fmt: skip
        job = add_job(o, a.tenant, child)
        o.commit()
        b_before = _state(o, b.tenant)
    assert advance(a.tenant, child, job) == "done"
    assert fake.seen == []
    with owner_db.session() as o:
        assert _run(o, child)["status"] == "cancelled"
        assert _state(o, b.tenant) == b_before


# ------------------------------------------------------------------ outcomes
def test_a_run_the_executor_parked_itself_is_not_moved_again(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two

    def park(ctx: RunContext) -> Any:
        with ctx.txn():
            repo.transition(
                ctx.session,
                ctx.tenant_id,
                ctx.run_id,
                state.RUNNING,
                state.WAITING,
                token=ctx.token,
                clear_lease=True,
                waiting_reason="child_runs",
            )
        raise state.Waiting("child_runs", transitioned=True)

    fake.behave = staticmethod(park)
    with owner_db.session() as o:
        b_before = _state(o, b.tenant)
    assert advance(a.tenant, a.run, a.job) == "done"
    with owner_db.session() as o:
        row = _run(o, a.run)
        assert (row["status"], row["waiting_reason"]) == ("waiting_for_approval", "child_runs")
        moves = _q(o, "SELECT from_status, to_status FROM shifde.run_events WHERE run_id = :r "
                      "AND type = 'state_change' ORDER BY seq", r=a.run)  # fmt: skip
        assert moves == [("pending", "running"), ("running", "waiting_for_approval")]
        assert _state(o, b.tenant) == b_before


def test_an_exception_during_a_write_ends_as_an_unknown_outcome_not_failed_internal(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    """C1 Python half (B-2151): any exception while an approved write is in flight."""
    a, b = two
    with owner_db.session() as o:
        for c in (a, b):  # both clients have a write in flight
            o.execute(text("DELETE FROM shifde.run_steps WHERE run_id = :r"), {"r": c.run})
            o.execute(text("DELETE FROM shifde.tool_invocations WHERE run_id = :r"), {"r": c.run})
            write_in_flight(o, c.tenant, c.run, c.tool)
        o.commit()
        b_before = _state(o, b.tenant)

    def crash(ctx: RunContext) -> Any:
        raise RuntimeError("bug after the write was sent")

    fake.behave = staticmethod(crash)
    assert advance(a.tenant, a.run, a.job) == "done"
    with owner_db.session() as o:
        row = _run(o, a.run)
        assert (row["status"], row["error_class"]) == ("failed", state.INDETERMINATE)
        assert _q(o, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r", r=a.run) == [
            ("indeterminate",)
        ]
        assert _q(o, "SELECT status FROM shifde.run_steps WHERE run_id = :r", r=a.run) == [
            ("indeterminate",)
        ]
        assert _q(o, "SELECT reason, status, arguments FROM shifde.approvals WHERE run_id = :r",
                  r=a.run) == [("indeterminate_side_effect", "pending", {"item": "x"})]  # fmt: skip
        assert _events(o, a.run).count("outcome_unknown") == 1
        assert _state(o, b.tenant) == b_before  # B's write stays started, nothing settled


def test_a_cancel_requested_meanwhile_ends_cancelled_and_settles_only_the_own_run(
    two: tuple[C, C], advance: Advance, fake: type[Fake], owner_db: Database
) -> None:
    a, b = two

    def cancelled(ctx: RunContext) -> Any:
        raise state.Cancelled()

    fake.behave = staticmethod(cancelled)
    with owner_db.session() as o:
        b_before = _state(o, b.tenant)
    assert advance(a.tenant, a.run, a.job) == "done"
    with owner_db.session() as o:
        row = _run(o, a.run)
        assert (row["status"], row["error_class"]) == ("cancelled", state.CANCELLED_CLASS)
        assert _state(o, b.tenant) == b_before


# ------------------------------------------------------------------ privileged role
def test_a_privileged_database_is_refused_for_the_advance_and_for_usage(
    two: tuple[C, C], fake: type[Fake], owner_db: Database, settings: Settings
) -> None:
    a, b = two
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    args = (settings, runtime, PolicyBudgetHooks(), a.tenant, a.run, WORKER, threading.Event(),
            threading.Event(), a.job)  # fmt: skip
    with owner_db.session() as o:
        before = _state(o, a.tenant), _state(o, b.tenant)
    with pytest.raises(UnsafeDatabaseRole):
        lifecycle.advance(owner_db, *args)
    with owner_db.session() as s, pytest.raises(UnsafeDatabaseRole):
        lifecycle.advance_in(s, owner_db, *args)
    assert fake.seen == []
    with owner_db.session() as o:
        assert (_state(o, a.tenant), _state(o, b.tenant)) == before


# ------------------------------------------------------------------ client unavailable (B-2243)
def test_a_run_whose_client_is_not_available_fails_before_it_starts(
    two: tuple[C, C],
    advance: Advance,
    fake: type[Fake],
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime isolation review 9 finding 2: when the client's header cannot be read, the run
    fails before any executor starts (`authz_denied`), its lease is cleared and B is untouched."""
    a, b = two
    with owner_db.session() as o:
        b_before = _state(o, b.tenant)
    monkeypatch.setattr(context_mod.tenancy, "tenant_header", lambda session, tenant_id: None)
    assert advance(a.tenant, a.run, a.job) == "done"
    assert fake.seen == []
    with owner_db.session() as o:
        row = _run(o, a.run)
        assert (row["status"], row["error_class"]) == ("failed", state.AUTHZ_DENIED)
        assert (row["lease_token"], row["lease_job_id"]) == (None, None)
        assert _state(o, b.tenant) == b_before
