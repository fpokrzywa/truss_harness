"""Runtime part 4 (RT-4): `context.py`, `settle.py`, `gate.py` and the run's `Env` (B-1933).

Behaviour: the Python settle path reports a write in flight at a cancel as an unknown outcome,
not a plain cancel (C1, B-2151), on the app role as the worker and the API run it; the cancel of
open children does the same for a child mid-write; a finished last child wakes its waiting parent;
the checkpoint, fence and gate act on the run's own rows; a budget hold without an output ceiling
is refused (R3 Item 9); the run's `Env` answers only for the run's pinned version and its pins.

Isolation: every function that takes a run id (directly or in a run row) is called in client A's
context with client B's ids and with unknown ids, as `shifde_app` and as the schema owner (no
row-level security); both answers must be the same, B's rows untouched, and A's own ids must give
another answer (not vacuous).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.authz.service import ResourceRef
from shifde.build import repository as build_repo
from shifde.core.db import Database
from shifde.core.errors import AppError, NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ledger.enforcement import ReserveDetail
from shifde.runtime import gate, lifecycle, settle, state
from shifde.runtime import repository as runtime_repo
from shifde.runtime.agent import AgentRunner
from shifde.runtime.context import RunContext
from tests._support.tenants import add_tenants
from tests.build.service_support import service_for
from tests.runtime.conftest import app_session
from tests.runtime.execution import Seed, client_state, run_context, run_row, seed_client

MODES = ["app-role", "schema-owner"]


@pytest.fixture
def seeds(owner_db: Database) -> tuple[Seed, Seed]:
    a, b = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, a, b)
        pair = seed_client(o, a), seed_client(o, b)
        o.commit()
    return pair


@pytest.fixture(params=MODES)
def opened(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], Any]:
    """`opened(client)`: a session of the parametrized kind (the app role with the client's
    context, or the schema owner)."""

    @contextmanager
    def session(tenant: UUID) -> Iterator[Session]:
        cm = app_session(app_db, tenant) if request.param == "app-role" else owner_db.session()
        with cm as s:
            yield s

    return session


def _agent_art(tenant: UUID, agent: UUID) -> dict[str, Any]:  # Any: artifact row
    """The artifact row an executor builds from its run row (SETH `agent.py`)."""
    return {"id": agent, "tenant_id": tenant, "kind": "agents", "workspace_id": None,
            "project_id": None, "key": "", "name": "", "status": "active"}  # fmt: skip


def _q(o: Session, sql: str, **params: Any) -> list[Any]:  # Any: row values
    o.rollback()
    return list(o.execute(text(sql), params).all())


# ------------------------------------------------------------------ settle.py (C1, B-2151)
def test_a_cancel_during_a_write_is_settled_as_an_unknown_outcome_not_a_plain_cancel(
    seeds: tuple[Seed, Seed], opened: Any, owner_db: Database
) -> None:
    a, b = seeds
    with owner_db.session() as o:
        b_before = client_state(o, b.tenant)
    with opened(a.tenant) as s:
        settle.settle_terminal(s, run_row(s, a.tenant, a.ended), state.CANCELLED)
        s.commit()
    with owner_db.session() as o:
        [(status, error_class)] = _q(
            o, "SELECT status, error_class FROM shifde.runs WHERE id = :r", r=a.ended
        )
        assert (status, error_class) == ("cancelled", state.INDETERMINATE)
        assert _q(o, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r", r=a.ended) == [
            ("indeterminate",)
        ]
        assert _q(o, "SELECT status FROM shifde.run_steps WHERE run_id = :r", r=a.ended) == [
            ("indeterminate",)
        ]
        approvals = _q(
            o,
            "SELECT reason, status, arguments FROM shifde.approvals WHERE run_id = :r "
            "ORDER BY reason",
            r=a.ended,
        )
        assert approvals == [
            ("indeterminate_side_effect", "pending", {"item": "x"}),
            ("tool_side_effect", "cancelled", {}),
        ]
        assert _q(
            o, "SELECT count(*) FROM shifde.run_events WHERE run_id = :r AND type = 'outcome_unknown'",
            r=a.ended,
        ) == [(1,)]  # fmt: skip
        [(outcome, actor, details)] = _q(
            o,
            "SELECT outcome, actor_id, details FROM shifde.audit_events "
            "WHERE action = 'run.indeterminate_side_effect' AND target_id = :r",
            r=str(a.ended),
        )
        assert (outcome, actor) == ("failure", None)
        assert details["ended_as"] == "cancelled" and details["open_decisions"] == 1
        assert client_state(o, b.tenant) == b_before


def test_cancelling_open_children_reports_a_child_mid_write_as_an_unknown_outcome(
    seeds: tuple[Seed, Seed], opened: Any, owner_db: Database
) -> None:
    a, b = seeds
    with owner_db.session() as o:
        b_before = client_state(o, b.tenant)
    with opened(a.tenant) as s:
        settle.cancel_open_children(s, a.tenant, a.running, a.user)
    with owner_db.session() as o:
        [row] = _q(
            o,
            "SELECT status, error_class, cancel_requested_by FROM shifde.runs WHERE id = :r",
            r=a.child,
        )
        assert tuple(row) == ("cancelled", state.INDETERMINATE, a.user)
        assert _q(
            o, "SELECT status FROM shifde.approvals WHERE run_id = :r "
            "AND reason = 'indeterminate_side_effect'", r=a.child,
        ) == [("pending",)]  # fmt: skip
        assert client_state(o, b.tenant) == b_before


def test_the_last_finished_child_wakes_its_waiting_parent(
    seeds: tuple[Seed, Seed], opened: Any, owner_db: Database
) -> None:
    a, _ = seeds
    with opened(a.tenant) as s:
        settle.wake_parent(s, run_row(s, a.tenant, a.finished_child))
        s.commit()
    with owner_db.session() as o:
        assert _q(
            o, "SELECT status, waiting_reason FROM shifde.runs WHERE id = :r", r=a.waiting
        ) == [("pending", None)]
        assert _q(o, "SELECT status FROM shifde.jobs WHERE run_id = :r", r=a.waiting) == [
            ("queued",)
        ]


@pytest.mark.parametrize("cancel", [True, False], ids=["cancel-requested", "no-cancel"])
def test_a_graceful_shutdown_ends_a_cancel_requested_run_cancelled_not_pending(
    cancel: bool,
    seeds: tuple[Seed, Seed],
    opened: Any,
    settings: Settings,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-2440: the worker stops (graceful shutdown) while its run has a cancel request (the API,
    or a fail_fast pipeline's `cancel_open_children`, which already finished the run's jobs): the
    run ends `cancelled`; handing it back left it `pending` with no job. Without a cancel request
    the shutdown still hands the run back."""
    a, b = seeds
    with owner_db.session() as o:
        b_before = client_state(o, b.tenant)

    def stopped(runner: AgentRunner) -> object:
        runner.ctx.stop.set()
        runner.ctx.checkpoint()  # the next control point after the stop
        raise AssertionError("the checkpoint must raise")

    monkeypatch.setattr(AgentRunner, "run", stopped)
    with opened(a.tenant) as s:
        if cancel:
            runtime_repo.request_cancel(s, a.tenant, a.running, a.user)
            s.commit()
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        outcome = lifecycle._run(ctx)
    with owner_db.session() as o:
        [(status, error_class)] = _q(
            o, "SELECT status, error_class FROM shifde.runs WHERE id = :r", r=a.running
        )
        assert client_state(o, b.tenant) == b_before
    if cancel:
        assert (outcome, status, error_class) == ("done", "cancelled", state.CANCELLED_CLASS)
    else:
        assert (outcome, status) == ("released", "pending")


# ------------------------------------------------------------------ context.py and gate.py
def test_the_checkpoint_fence_and_budget_scope_act_on_the_runs_own_rows(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    a, _ = seeds
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        ctx.checkpoint()
        with ctx.txn():
            ctx.audit("run.test", commit=False)
        assert ctx.reload_run()["id"] == a.running
        child = run_context(s, settings, run_row(s, a.tenant, a.pipeline_child))
        with pytest.raises(state.Cancelled):  # an agent under a failed pipeline stops
            child.checkpoint()
        assert (
            gate.budget_scope(child).pipeline_id == run_row(s, a.tenant, a.pipeline)["pipeline_id"]
        )


def test_the_gate_records_its_decision_and_refuses_a_hold_without_an_output_ceiling(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings, owner_db: Database
) -> None:
    """R3 Item 9: a hold that cannot cover the call's output is refused before anything is
    held; with a ceiling the call is allowed (no budget applies here)."""
    a, _ = seeds
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        ref = ctx.run_ref()
        with pytest.raises(state.GateDenied) as denied:
            gate.check(ctx, "model:invoke", ref, label="m", reserve=("m:1", 10))
        assert (denied.value.error_class, denied.value.reason) == (
            state.BUDGET_DENIED,
            "no_output_ceiling",
        )
        no_ceiling = ReserveDetail(model_id=None, est_input_tokens=5, est_output_tokens=0)
        with pytest.raises(state.GateDenied):
            gate.check(ctx, "model:invoke", ref, reserve=("m:1", 10), reserve_detail=no_ceiling)
        ok = gate.check(
            ctx,
            "model:invoke",
            ref,
            label="m",
            reserve=("m:1", 10),
            reserve_detail=ReserveDetail(model_id=None, est_input_tokens=5, est_output_tokens=5),
        )
        assert ok.principal.principal_id == a.user
    with owner_db.session() as o:
        decisions = _q(
            o,
            "SELECT decision, reason_codes FROM shifde.policy_decisions WHERE run_id = :r "
            "ORDER BY at, decision",
            r=a.running,
        )
        assert [d[0] for d in decisions] == ["deny", "deny", "allow"]
        assert decisions[0][1] == ["no_output_ceiling"]
        assert _q(
            o, "SELECT count(*) FROM shifde.budget_reservations WHERE run_id = :r", r=a.running
        ) == [(0,)]


# ------------------------------------------------------------------ the run's Env (B-1933)
def test_a_runs_env_answers_for_its_pinned_version_and_pins_only(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    a, b = seeds
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        env = ctx.env(_agent_art(a.tenant, a.agent))
        assert not env.reveal_names and not env.check_visibility
        for pinned in (a.agent_version, a.prompt, a.composed):
            found = env.version(pinned)
            assert found is not None and found.version_id == pinned
        for hidden in (a.unpinned, a.other_version, b.agent_version, b.prompt, uuid7()):
            assert env.version(hidden) is None
        assert env.latest(a.agent) is None


def test_a_runs_env_refuses_another_clients_run_and_a_run_of_another_artifact(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    a, b = seeds
    with opened(a.tenant) as s:
        svc = service_for(s, a.tenant, a.user, settings, set_context=False)
        own = run_row(s, a.tenant, a.running)
        art = _agent_art(a.tenant, a.agent)
        assert svc.env(art, run_of=own).version(a.prompt) is not None
        b_run = {**own, "id": b.running, "tenant_id": b.tenant}
        refusals = {
            "a run of B": lambda: svc.env(art, run_of=b_run),
            "B's run named in A": lambda: svc.env(art, run_of={**own, "id": b.running}),
            "B's art and run named in A": lambda: svc.env(
                _agent_art(a.tenant, b.agent), run_of={**own, "id": b.running}
            ),
            "B's art": lambda: svc.env(_agent_art(b.tenant, b.agent), run_of=b_run),
            "unknown run": lambda: svc.env(art, run_of={**own, "id": uuid7()}),
            "a run of another artifact": lambda: svc.env(
                art, run_of=run_row(s, a.tenant, a.other_run)
            ),
            "another artifact for the run": lambda: svc.env(
                _agent_art(a.tenant, a.other_agent), run_of=own
            ),
            "a kind runs do not execute": lambda: svc.env({**art, "kind": "prompts"}, run_of=own),
        }
        for name, call in refusals.items():
            with pytest.raises(NotFound, match="not found"):
                call()
            s.rollback()
            assert svc.env(art, run_of=own).version(a.prompt) is not None, name
        with pytest.raises(ValueError):
            svc.env(art, run_of=own, approval_of={"id": uuid7()})
        assert build_repo.pin_closure(s, a.tenant, b.agent_version) == set()


def test_a_runs_env_has_no_latest_and_permits_only_the_versions_the_run_executes(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    """Runtime isolation review 3 items 1 and 2: in a run's env `latest()` answers None (a run
    executes pinned versions) and `permits` is False for a version outside the run's pins; the
    same calls in a publisher's env answer, so neither assertion is vacuous."""
    a, _ = seeds
    with opened(a.tenant) as s:
        svc = service_for(s, a.tenant, a.user, settings, set_context=False)
        art = _agent_art(a.tenant, a.agent)
        free = svc.env(art)
        assert free.latest(a.agent) is not None and free.latest(a.unpinned_art) is not None
        run_env = svc.env(art, run_of=run_row(s, a.tenant, a.running))
        assert run_env.latest(a.agent) is None
        assert run_env.latest(a.unpinned_art) is None
        unpinned = free.version(a.unpinned)
        pinned = run_env.version(a.prompt)
        assert unpinned is not None and pinned is not None
        assert free.permits("prompt:read", unpinned) and free.permits("prompt:read", pinned)
        assert run_env.permits("prompt:read", pinned)
        assert not run_env.permits("prompt:read", unpinned)
        assert run_env.version(a.unpinned) is None


def test_a_runs_env_holds_the_stored_artifact_row_not_the_callers_mapping(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    """Runtime isolation review 3 item 3: `_run_env` re-reads the artifact; a mapping forged with
    another workspace, project, key or status does not reach the env (its scope feeds the model
    checks)."""
    a, b = seeds
    with opened(a.tenant) as s:
        svc = service_for(s, a.tenant, a.user, settings, set_context=False)
        own = run_row(s, a.tenant, a.running)
        stored = build_repo.get_artifact(s, a.tenant, a.agent)
        assert stored is not None
        forged = {**_agent_art(a.tenant, a.agent), "workspace_id": own["workspace_id"],
                  "project_id": own["project_id"], "key": "forged", "status": "archived"}  # fmt: skip
        assert (stored["workspace_id"], stored["project_id"]) != (
            forged["workspace_id"],
            forged["project_id"],
        )
        env = svc.env(forged, run_of=own)
        assert dict(env.art) == dict(stored)
        assert svc.scope_ref_of(env.art) == svc.scope_ref_of(stored)
        # a mapping naming B's artifact id under A's client is still the unknown run
        with pytest.raises(NotFound, match="run not found"):
            svc.env({**forged, "id": b.agent}, run_of=own)


# ------------------------------------------------------------------ cross-client cases
@dataclass(frozen=True)
class Target:
    """The ids a case names: B's, or unknown ones."""

    agent: UUID
    running: UUID
    token: UUID
    waiting: UUID
    pipeline: UUID
    ended: UUID


def _target(x: Seed) -> Target:
    return Target(x.agent, x.running, x.token, x.waiting, x.pipeline, x.ended)


def _forged(s: Session, settings: Settings, a: Seed, t: Target) -> RunContext:
    """A's context whose run row names `t.running` (a forged or stale id) with `t`'s token."""
    return run_context(s, settings, {**run_row(s, a.tenant, a.running), "id": t.running},
                       token=t.token)  # fmt: skip


Case = Callable[[Session, Settings, Seed, Target], object]
CASES: dict[str, Case] = {
    "settle.settle_terminal": lambda s, st, a, t: settle.settle_terminal(
        s, {**run_row(s, a.tenant, a.ended), "id": t.ended}, state.CANCELLED
    ),
    "settle.wake_parent": lambda s, st, a, t: settle.wake_parent(
        s, {**run_row(s, a.tenant, a.finished_child), "parent_run_id": t.waiting}
    ),
    "settle.cancel_open_children": lambda s, st, a, t: settle.cancel_open_children(
        s, a.tenant, t.running, a.user
    ),
    "context.checkpoint (run)": lambda s, st, a, t: _forged(s, st, a, t).checkpoint(),
    "context.control": lambda s, st, a, t: _forged(s, st, a, t).control(),
    "context.checkpoint (parent)": lambda s, st, a, t: run_context(
        s, st, {**run_row(s, a.tenant, a.pipeline_child), "parent_run_id": t.pipeline}
    ).checkpoint(),
    "context._parent_is_pipeline": lambda s, st, a, t: run_context(
        s, st, run_row(s, a.tenant, a.running)
    )._parent_is_pipeline(t.pipeline),
    "context.reload_run": lambda s, st, a, t: _reloaded(_forged(s, st, a, t)),
    "context.txn": lambda s, st, a, t: _txn(_forged(s, st, a, t)),
    "context.audit": lambda s, st, a, t: _forged(s, st, a, t).audit("run.test"),
    "context.env": lambda s, st, a, t: _forged(s, st, a, t).env(_agent_art(a.tenant, t.agent)),
    "build.env(run_of)": lambda s, st, a, t: service_for(
        s, a.tenant, a.user, st, set_context=False
    ).env(_agent_art(a.tenant, t.agent), run_of={"id": t.running, "tenant_id": a.tenant}),
    "gate.budget_scope": lambda s, st, a, t: (
        gate.budget_scope(
            run_context(
                s, st, {**run_row(s, a.tenant, a.pipeline_child), "root_run_id": t.pipeline}
            )
        ).pipeline_id
    ),
    "gate.record_decision": lambda s, st, a, t: gate.record_decision(
        _forged(s, st, a, t), "model:invoke", None, True, ["allowed"]
    ),
    "gate.check": lambda s, st, a, t: gate.check(
        _forged(s, st, a, t), "model:invoke", ResourceRef(a.tenant)
    ),
}


def _reloaded(ctx: RunContext) -> object:
    """What a reload makes of a row whose workspace is blanked: the stored row's, or nothing."""
    ctx.run = {**ctx.run, "workspace_id": None}
    return ctx.reload_run()["workspace_id"]


def _txn(ctx: RunContext) -> None:
    with ctx.txn():
        ctx.audit("run.test", commit=False)


def _answer(s: Session, settings: Settings, case: Case, a: Seed, t: Target) -> str:
    try:
        result = case(s, settings, a, t)
        s.commit()
    except (AppError, state.RunFailed, state.LeaseLost, state.Cancelled) as exc:
        s.rollback()
        return f"{type(exc).__name__}: {exc}"
    return "ok" if result is None else f"{type(result).__name__}: {result!r}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_naming_bs_run_gets_the_unknown_id_answer(
    name: str, seeds: tuple[Seed, Seed], opened: Any, settings: Settings, owner_db: Database
) -> None:
    a, b = seeds
    case = CASES[name]
    unknown = Target(uuid7(), uuid7(), uuid7(), uuid7(), uuid7(), uuid7())
    with owner_db.session() as o:
        b_before = client_state(o, b.tenant)
        a_before = client_state(o, a.tenant)
    with opened(a.tenant) as s:
        for_b = _answer(s, settings, case, a, _target(b))
    with owner_db.session() as o:
        assert client_state(o, b.tenant) == b_before, "B's rows changed"
        a_after_b = client_state(o, a.tenant)
    with opened(a.tenant) as s:
        for_unknown = _answer(s, settings, case, a, unknown)
    assert for_b == for_unknown
    with owner_db.session() as o:
        a_after_unknown = client_state(o, a.tenant)
    # not vacuous: A's own ids give another answer or change A's rows
    with opened(a.tenant) as s:
        for_own = _answer(s, settings, case, a, _target(a))
    with owner_db.session() as o:
        changed = client_state(o, a.tenant) != a_after_unknown
    assert for_own != for_unknown or changed, (for_own, for_unknown)
    assert a_after_b == a_before or name in _A_SIDE_EFFECTS, "B's ids changed A's rows"


# a refused fence or an unknown target still leaves A's own trace (none today): listed if needed
_A_SIDE_EFFECTS: frozenset[str] = frozenset()


def test_every_function_taking_a_run_is_a_case() -> None:
    public = {
        f"{mod.__name__.rsplit('.', 1)[1]}.{n}"
        for mod in (settle, gate)
        for n in getattr(mod, "__all__", dir(mod))
        if not n.startswith("_")
        and callable(getattr(mod, n))
        and getattr(getattr(mod, n), "__module__", "") == mod.__name__
    }
    covered = {n.split(" ")[0] for n in CASES}
    # pure or row-free: `uuid_of` parses a string; `append_decision` writes inside a caller's
    # fenced `ctx.txn()` (the fence is the `context.txn` case); `_deny` goes through
    # `record_decision` and `ctx.audit`
    assert public - covered <= {"gate.uuid_of", "gate.append_decision", "gate.GateResult"}
    methods = {
        f"context.{m}" for m in ("checkpoint", "control", "reload_run", "txn", "audit", "env")
    }
    assert methods <= covered
