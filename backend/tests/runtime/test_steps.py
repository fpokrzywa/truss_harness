"""Runtime part 6 (RT-6): `steps.py`, the journaled model and tool calls of a run.

Behaviour: every model call sends the output ceiling its budget hold covers and names the run
(R3 Items 9 and 15); `limits.max_cost` is checked before each call with the run's known cost
(Item 10); a provider's unreported usage is estimated, labelled so and written durably before
the step's own transaction (Item 11); a lease lost during an approved write is classified
`indeterminate` by the recovering worker, never resent and never a plain cancel (C1, B-2151); an
ontology tool passes `tool_gate` before `tool_query` with the run's own context; a usage event
carries only its model's own `provider_key` / `model_key` (review 3 follow-up).

Isolation: `call_model` and `run_tool` are called with A's context naming B's run (a forged or
stale row) and an unknown run, as `shifde_app` and as the schema owner: both answers are the same,
B's rows are untouched and A's own run gives another answer.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.adapters.base import AdapterFailure, ErrorClass, Usage
from shifde.adapters.httputil import join_url
from shifde.adapters.rest_http import RestHttpAdapter
from shifde.build import tool_runtime
from shifde.build.env import VersionRef
from shifde.build.executors import ONTOLOGY_QUERY
from shifde.core.db import Database
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ledger import estimate, pricing
from shifde.models_admin import schemas as ms
from shifde.models_admin.service import RunInvoke
from shifde.runtime import ontology_exec, state, steps
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from tests._support.runs import add_run
from tests.build.env_support import add_published
from tests.build.service_support import service_for
from tests.ledger.util import add_model, add_rate
from tests.runtime.execution import Seed, client_state, run_context, run_row
from tests.runtime.test_execution import MODES, opened, seeds

__all__ = ["MODES", "opened", "seeds"]  # fixtures shared with the RT-4 tests

MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]
ANSWER = "four words of answer"
WRITE = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
         "side_effect_class": "write_non_idempotent", "input_schema": {"type": "object"}}  # fmt: skip
ONTO = {"executor": "builtin.function", "binding": {"function_key": ONTOLOGY_QUERY},
        "timeout_s": 5, "side_effect_class": "none", "input_schema": {"type": "object"}}  # fmt: skip


@dataclass(frozen=True)
class Extra:
    model: UUID  # priced model of the client (1000 per million tokens each way)
    unpriced: UUID  # model without a rate
    tool: VersionRef  # a write tool of the client
    replay: steps.ToolCall  # a call with a finished delivery on `running`
    output: dict[str, str]  # that delivery's output


def _tool(o: Session, tenant: UUID, spec: dict[str, Any]) -> VersionRef:  # Any: spec
    key = f"tool-{uuid7().hex}"
    art, ver = add_published(o, tenant, "tools", key, spec)
    return VersionRef(ver, art, "tools", 1, "published", spec, None, key, key, "active")


def _extra(o: Session, x: Seed) -> Extra:
    model = add_model(o, x.tenant)
    add_rate(o, x.tenant, input_rate="1000", output_rate="1000")
    unpriced = add_model(o, x.tenant, model_key="m-unpriced")
    tool = _tool(o, x.tenant, WRITE)
    output = {"secret": f"output of {x.tenant}"}
    out_ref = repo.insert_artifact(o, x.tenant, x.running, "intermediate", output)
    step = repo.insert_step(o, {"tenant_id": x.tenant, "run_id": x.running, "seq": 50,
                                "attempt": 1, "kind": "tool_call", "name": tool.artifact_key,
                                "node_id": "replay", "tool_version_id": tool.version_id,
                                "status": "running"})  # fmt: skip
    repo.update_step(o, x.tenant, step["id"], status="succeeded", output_ref=out_ref)
    replay = steps.ToolCall(tool, {}, "node:replay", node_id="replay")
    return Extra(model, unpriced, tool, replay, output)


@pytest.fixture
def world(seeds: tuple[Seed, Seed], owner_db: Database) -> tuple[Seed, Seed, Extra, Extra]:
    a, b = seeds
    with owner_db.session() as o:
        xa, xb = _extra(o, a), _extra(o, b)
        o.commit()
    return a, b, xa, xb


def _q(o: Session, sql: str, **params: Any) -> list[Any]:  # Any: row values
    o.rollback()
    return list(o.execute(text(sql), params).all())


def _fake_model(
    monkeypatch: pytest.MonkeyPatch,
    ctx: RunContext,
    calls: list[dict[str, Any]],  # Any: what each call sent
    reported: Usage,
    *,
    lose_lease: bool = False,
) -> None:
    def invoke_for_run(
        ref: object,
        model_id: UUID,
        body: ms.InvokeIn,
        *,
        run_id: UUID,
        max_deadline: float,
        classification: str | None = None,
    ) -> RunInvoke:
        calls.append({"model_id": model_id, "max_tokens": body.max_tokens, "run_id": run_id})
        if lose_lease:  # the heartbeat lost the lease while the provider answered
            ctx.lease_lost.set()
        out = ms.InvokeOut(
            output=ANSWER, usage=ms.UsageOut(input_tokens=1, output_tokens=1), latency_ms=1
        )
        assert body.max_tokens is not None
        return RunInvoke(out, reported, body.max_tokens)

    monkeypatch.setattr(ctx.artifacts.models, "invoke_for_run", invoke_for_run)


def _no_calls(text_: str) -> None:
    return None


def _model_events(o: Session, run: UUID) -> list[Any]:  # Any: row values
    return _q(
        o,
        "SELECT input_tokens, output_tokens, cost_kind, provider_reported_usage->>'basis', "
        "provider_key, model_key FROM shifde.usage_events "
        "WHERE run_id = :r AND event_type = 'model_call' ORDER BY occurred_at",
        r=run,
    )


# ------------------------------------------------------------------ Items 9 and 15
def test_every_model_call_sends_the_ceiling_its_hold_covers_and_names_the_run(
    world: tuple[Seed, Seed, Extra, Extra],
    opened: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SETH sent no `max_tokens` and held `len(chars) / 4 + reserve`: the call's output had no
    ceiling, so a budget could be overrun by whatever the model returned."""
    a, _, xa, _ = world
    calls: list[dict[str, Any]] = []
    holds: list[tuple[int, Any]] = []
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        reserve = ctx.hooks.reserve

        def spy(*args: Any, **kwargs: Any) -> Any:
            holds.append((args[4], kwargs.get("detail", args[5] if len(args) > 5 else None)))
            return reserve(*args, **kwargs)

        monkeypatch.setattr(ctx.hooks, "reserve", spy)
        _fake_model(monkeypatch, ctx, calls, Usage(input_tokens=7, output_tokens=3))
        result = steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls)
    ceiling = settings.runtime_output_reserve_tokens
    assert result.text == ANSWER
    assert calls == [{"model_id": xa.model, "max_tokens": ceiling, "run_id": a.running}]
    est_input = estimate.estimate_input_tokens(MESSAGES)
    [(held, detail)] = holds
    assert held == est_input + ceiling
    assert (detail.model_id, detail.est_input_tokens, detail.est_output_tokens) == (
        xa.model,
        est_input,
        ceiling,
    )


def test_a_run_call_without_an_output_ceiling_is_refused_by_the_model_service(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    a, _ = seeds
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        body = ms.InvokeIn(messages=[ms.ChatMessageIn(role="user", content="x")])
        with pytest.raises(ValueError, match="output ceiling"):
            ctx.artifacts.models.invoke_for_run(ctx.run_ref(), uuid7(), body, run_id=a.running)


# ------------------------------------------------------------------ Item 11
def test_unreported_usage_is_estimated_labelled_and_survives_a_lost_lease(
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SETH stored the provider's missing counts as 0, priced them `calculated`, and wrote the
    row in the step's transaction, so a worker that lost its lease after the provider answered
    left no usage at all."""
    a, _, xa, _ = world
    from tests.runtime.conftest import app_session

    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running), db=app_db)
        _fake_model(monkeypatch, ctx, [], Usage(), lose_lease=True)
        with pytest.raises(state.LeaseLost):
            steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls)
    with owner_db.session() as o:
        assert _model_events(o, a.running) == [
            (
                estimate.estimate_input_tokens(MESSAGES),
                estimate.estimate_output_tokens(ANSWER),
                "estimated",
                "estimated",
                "openai",
                "m1",
            )
        ]
        # the step's own transaction never committed: the attempt is still open
        assert _q(
            o, "SELECT status FROM shifde.run_steps WHERE run_id = :r AND kind = 'model_call'",
            r=a.running,
        ) == [("running",)]  # fmt: skip


@pytest.mark.parametrize("durable", [True, False])
def test_reported_usage_is_stored_as_reported_and_counted_once(
    durable: bool,
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    from tests.runtime.conftest import app_session

    with app_session(app_db, a.tenant) as s:
        ctx = run_context(
            s, settings, run_row(s, a.tenant, a.running), db=app_db if durable else None
        )
        _fake_model(monkeypatch, ctx, [], Usage(input_tokens=7, output_tokens=3))
        steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls)
    with owner_db.session() as o:
        assert _model_events(o, a.running) == [(7, 3, "calculated", "reported", "openai", "m1")]
        assert _q(
            o, "SELECT input_tokens, output_tokens, model_calls FROM shifde.runs WHERE id = :r",
            r=a.running,
        ) == [(7, 3, 1)]  # fmt: skip
        [(usage,)] = _q(
            o, "SELECT usage FROM shifde.run_steps WHERE run_id = :r AND kind = 'model_call'",
            r=a.running,
        )  # fmt: skip
        assert (usage["input_tokens"], usage["output_tokens"], usage["basis"]) == (
            7,
            3,
            "reported",
        )


# ------------------------------------------------------------------ Item 10
def _spend(o: Session, x: Seed, amount: str) -> None:
    """A known cost already on the run (a priced event of an earlier step)."""
    ledger.record(
        o,
        {"tenant_id": x.tenant, "event_key": f"spent-{uuid7()}", "event_type": "tool_call",
         "run_id": x.running, "root_run_id": x.running, "call_count": 1,
         "costed": pricing.Costed("calculated", Decimal(amount), "USD", None, None)},
    )  # fmt: skip


def _cost_answer(
    s: Session, settings: Settings, x: Seed, model: UUID, limit: object, mp: pytest.MonkeyPatch
) -> str:
    calls: list[dict[str, Any]] = []
    ctx = run_context(s, settings, run_row(s, x.tenant, x.running))
    _fake_model(mp, ctx, calls, Usage(input_tokens=7, output_tokens=3))
    try:
        steps.call_model(ctx, list(MESSAGES), [model], "m", [], _no_calls, max_cost=limit)
    except state.RunFailed as exc:
        assert not calls, "a refused call was sent"
        return exc.error_class
    assert len(calls) == 1
    return "sent"


def test_max_cost_is_enforced_before_each_call_with_the_runs_known_cost(
    world: tuple[Seed, Seed, Extra, Extra],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SETH never compared `limits.max_cost` with anything: every one of these calls was sent.
    The worst case of a call here is (input estimate + 256) * 0.001 USD, about 0.27."""
    a, _, xa, _ = world
    worst = estimate.call_cost
    with opened(a.tenant) as s:
        cost = worst(s, a.tenant, xa.model, estimate.estimate_input_tokens(MESSAGES), 256)
        assert cost is not None and Decimal("0.2") < cost.amount < Decimal("0.3")
        assert _cost_answer(s, settings, a, xa.model, 0.1, monkeypatch) == state.BUDGET_DENIED
        assert _cost_answer(s, settings, a, xa.model, None, monkeypatch) == "sent"
        assert _cost_answer(s, settings, a, xa.model, 5, monkeypatch) == "sent"
    with owner_db.session() as o:
        _spend(o, a, "4.8")
        o.commit()
    with opened(a.tenant) as s:  # 4.8 + 0.02 spent + 0.27 worst case > 5
        assert _cost_answer(s, settings, a, xa.model, 5, monkeypatch) == state.BUDGET_DENIED
        assert _cost_answer(s, settings, a, xa.model, "6", monkeypatch) == "sent"
        # an unpriced model follows the client's unknown-cost policy
        assert _cost_answer(s, settings, a, xa.unpriced, 5, monkeypatch) == "sent"
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.tenants SET settings = settings || '{\"unknown_cost_policy\": "
                 "\"deny\"}'::jsonb WHERE id = :t"),
            {"t": a.tenant},
        )  # fmt: skip
        o.commit()
    with opened(a.tenant) as s:
        assert _cost_answer(s, settings, a, xa.unpriced, 5, monkeypatch) == state.BUDGET_DENIED
        assert _cost_answer(s, settings, a, xa.unpriced, None, monkeypatch) == "sent"
    with owner_db.session() as o:
        denials = _q(
            o,
            "SELECT count(*) FROM shifde.policy_decisions WHERE run_id = :r "
            "AND decision = 'deny' AND reason_codes = ARRAY['max_cost']",
            r=a.running,
        )
        assert denials == [(3,)]


# ------------------------------------------------------------------ C1 (B-2151)
def _approve(o: Session, x: Seed, reason: str) -> None:
    o.execute(
        text(
            "UPDATE shifde.approvals SET status = 'approved', decided_by = :u, decided_at = now() "
            "WHERE run_id = :r AND reason = :why"
        ),
        {"u": x.user, "r": x.running, "why": reason},
    )
    o.commit()


def test_a_lease_lost_during_an_approved_write_is_indeterminate_never_resent_or_cancelled(
    world: tuple[Seed, Seed, Extra, Extra],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    deliveries: list[str] = []
    current: list[RunContext] = []

    def invoke_once(conns: object, spec: object, args: object, **kw: Any) -> object:
        deliveries.append(kw["idempotency_key"])
        current[-1].lease_lost.set()  # the lease is lost while the write is delivered
        return {"written": True}

    monkeypatch.setattr(tool_runtime, "invoke_once", invoke_once)
    call = steps.ToolCall(xa.tool, {"item": "x"}, "node:w", node_id="w")
    policy = steps.ToolPolicy(
        frozenset({"write_non_idempotent"}), None, frozenset({xa.tool.version_id})
    )

    def attempt() -> BaseException:
        with opened(a.tenant) as s:
            current.append(run_context(s, settings, run_row(s, a.tenant, a.running)))
            with pytest.raises(state.RunInterrupt) as info:
                steps.run_tool(current[-1], call, policy)
            return info.value

    first = attempt()
    assert isinstance(first, state.Waiting) and first.reason == "approval"
    with owner_db.session() as o:
        _approve(o, a, "tool_side_effect")
    assert isinstance(attempt(), state.LeaseLost)
    assert len(deliveries) == 1
    with owner_db.session() as o:
        assert _q(o, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r AND "
                  "call_ref = 'node:w'", r=a.running) == [("started",)]  # fmt: skip
        # the user cancels meanwhile; the run's next owner still sees the write first
        o.execute(
            text("UPDATE shifde.runs SET cancel_requested_at = now() WHERE id = :r"),
            {"r": a.running},
        )
        o.commit()
    recovered = attempt()
    assert isinstance(recovered, state.Waiting), recovered
    assert recovered.reason == "indeterminate_side_effect"
    assert len(deliveries) == 1, "the write was resent"
    with owner_db.session() as o:
        assert _q(o, "SELECT status FROM shifde.tool_invocations WHERE run_id = :r AND "
                  "call_ref = 'node:w'", r=a.running) == [("indeterminate",)]  # fmt: skip
        assert _q(
            o, "SELECT status, error_class FROM shifde.run_steps WHERE run_id = :r "
            "AND kind = 'tool_call' AND node_id = 'w'", r=a.running,
        ) == [("indeterminate", state.INDETERMINATE)]  # fmt: skip
        assert _q(
            o, "SELECT status, arguments FROM shifde.approvals WHERE run_id = :r "
            "AND reason = 'indeterminate_side_effect'", r=a.running,
        ) == [("pending", {"item": "x"})]  # fmt: skip
        assert _q(o, "SELECT status FROM shifde.runs WHERE id = :r", r=a.running) == [("running",)]


# ------------------------------------------------------------------ ontology tool gate
@pytest.mark.parametrize("force_approval", [False, True])
def test_an_ontology_tool_passes_its_gate_before_its_query_with_the_runs_own_context(
    force_approval: bool,
    world: tuple[Seed, Seed, Extra, Extra],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    with owner_db.session() as o:
        tool = _tool(o, a.tenant, ONTO)
        o.commit()
    order: list[tuple[str, UUID, UUID]] = []

    def gate(ctx: RunContext, args: object) -> None:
        order.append(("gate", ctx.tenant_id, ctx.run_id))

    def query(ctx: RunContext, args: object, purpose: str) -> object:
        order.append(("query", ctx.tenant_id, ctx.run_id))
        if len([o for o in order if o[0] == "query"]) == 1:  # a retried timeout
            raise AdapterFailure(ErrorClass.TIMEOUT, "slow")
        return {"rows": []}

    monkeypatch.setattr(ontology_exec, "tool_gate", gate)
    monkeypatch.setattr(ontology_exec, "tool_query", query)
    spec = {**ONTO, "retry": {"max_attempts": 2}}
    call = steps.ToolCall(dataclasses.replace(tool, spec=spec), {"ontology_id": "x"}, "node:o",
                          node_id="o")  # fmt: skip
    policy = steps.ToolPolicy(frozenset(), None, frozenset({tool.version_id}), force_approval)
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        if force_approval:
            with pytest.raises(state.Waiting):
                steps.run_tool(ctx, call, policy)
            assert order == [("gate", a.tenant, a.running)]
            with owner_db.session() as o:
                _approve(o, a, "approval_rule")
        assert steps.run_tool(ctx, call, policy) == {"rows": []}
    own = (a.tenant, a.running)
    gates = [("gate", *own)] * (2 if force_approval else 1)
    assert order == [*gates, ("query", *own), ("query", *own)]


# ------------------------------------------------------------------ provider_key (review 3)
def test_a_usage_event_carries_only_its_models_own_keys(
    world: tuple[Seed, Seed, Extra, Extra], opened: Any
) -> None:
    a, _, xa, xb = world

    def event(**cols: Any) -> dict[str, Any]:  # Any: column values
        return {"tenant_id": a.tenant, "event_key": f"k-{uuid7()}", "event_type": "model_call",
                "status": "failed", "input_tokens": 0, "output_tokens": 0, **cols}  # fmt: skip

    refused = {
        "a chosen provider key": event(model_id=xa.model, provider_key="evil", model_key="m1"),
        "a chosen model key": event(model_id=xa.model, provider_key="openai", model_key="evil"),
        "B's model": event(model_id=xb.model, provider_key="openai", model_key="m1"),
        "an unknown model": event(model_id=uuid7(), provider_key="openai", model_key="m1"),
        "no model": event(provider_key="openai", model_key="m1"),
    }
    with opened(a.tenant) as s:
        for name, values in refused.items():
            for write in (ledger.record, repo.insert_usage_event):
                with pytest.raises(ValueError, match="model's own keys"):
                    write(s, values)
                s.rollback()
            assert name
        assert ledger.record(s, event(model_id=xa.model, provider_key="openai", model_key="m1"))
        assert repo.insert_usage_event(s, event(model_id=xa.model))
        s.rollback()


# ------------------------------------------------------------------ cross-client
@dataclass(frozen=True)
class Target:
    running: UUID
    token: UUID


def _forged(s: Session, settings: Settings, a: Seed, t: Target) -> RunContext:
    return run_context(s, settings, {**run_row(s, a.tenant, a.running), "id": t.running},
                       token=t.token)  # fmt: skip


Case = Callable[[Session, Settings, Seed, Extra, Target, pytest.MonkeyPatch], object]


def _call_model(
    s: Session, st: Settings, a: Seed, xa: Extra, t: Target, mp: pytest.MonkeyPatch
) -> object:
    ctx = _forged(s, st, a, t)
    _fake_model(mp, ctx, [], Usage(input_tokens=1, output_tokens=1))
    return steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls, max_cost=100)


CASES: dict[str, Case] = {
    "steps.call_model": _call_model,
    # B's finished delivery of the same call must never be replayed into A's run
    "steps.run_tool": lambda s, st, a, xa, t, mp: steps.run_tool(
        _forged(s, st, a, t),
        xa.replay,
        steps.ToolPolicy(frozenset(), None, frozenset({xa.replay.tool.version_id})),
    ),
}


def _answer(s: Session, case: Case, *args: Any) -> str:
    try:
        result = case(s, *args)
        s.commit()
    except (AppError, state.RunInterrupt) as exc:
        s.rollback()
        return f"{type(exc).__name__}: {exc}"
    return f"{type(result).__name__}: {json.dumps(result, default=str)[:80]}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_context_naming_bs_run_gets_the_unknown_id_answer(
    name: str,
    world: tuple[Seed, Seed, Extra, Extra],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b, xa, xb = world
    case = CASES[name]
    # B's replayable call, as A's context would name it (same call ref, same node)
    xa_b = dataclasses.replace(xa, replay=dataclasses.replace(xa.replay, tool=xb.replay.tool))
    with owner_db.session() as o:
        b_before = client_state(o, b.tenant)
    with opened(a.tenant) as s:
        for_b = _answer(s, case, settings, a, xa_b, Target(b.running, b.token), monkeypatch)
    with owner_db.session() as o:
        assert client_state(o, b.tenant) == b_before, "B's rows changed"
    with opened(a.tenant) as s:
        for_unknown = _answer(s, case, settings, a, xa_b, Target(uuid7(), uuid7()), monkeypatch)
        for_own = _answer(s, case, settings, a, xa, Target(a.running, a.token), monkeypatch)
    assert for_b == for_unknown, (for_b, for_unknown)
    assert for_own != for_unknown, for_own
    assert str(xb.output["secret"]) not in for_b


def test_every_public_step_entry_point_is_a_case() -> None:
    public = {
        n
        for n in dir(steps)
        if not n.startswith("_")
        and callable(getattr(steps, n))
        and getattr(getattr(steps, n), "__module__", "") == steps.__name__
    }
    # pure: JSON, backoff, error classification, the dataclasses; `tool_resource` builds a ref
    pure = {"json_safe", "latest_attempts", "backoff_seconds", "classify_model_error",
            "classify_tool_failure", "tool_resource", "ModelResult", "ToolCall", "ToolPolicy"}  # fmt: skip
    assert public - pure == {n.split(".")[1] for n in CASES}


# ------------------------------------------------------------------ runtime isolation review 4
def test_a_runs_versions_are_those_of_its_own_kind(
    seeds: tuple[Seed, Seed], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Item 1: a pipeline run that also names an agent version executes the pipeline's version
    and its pins, never the agent's (SETH took the first non-null column: the agent's)."""
    a, _ = seeds
    with owner_db.session() as o:
        pipe, pipe_version = add_published(o, a.tenant, "pipelines", f"p-{uuid7().hex}", {})
        run = add_run(o, a.tenant, kind="pipeline", pipeline_id=pipe,
                      pipeline_version_id=pipe_version, agent_id=a.agent,
                      agent_version_id=a.agent_version)  # fmt: skip
        o.commit()
    with opened(a.tenant) as s:
        svc = service_for(s, a.tenant, a.user, settings, set_context=False)
        ids = {v["id"] for v in svc.run_versions(run_row(s, a.tenant, run))}
        assert pipe_version in ids
        assert a.agent_version not in ids and a.prompt not in ids
        agent_ids = {v["id"] for v in svc.run_versions(run_row(s, a.tenant, a.running))}
        assert {a.agent_version, a.prompt, a.composed} <= agent_ids


def test_a_runs_env_refuses_a_pinned_version_paired_with_another_artifact(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings
) -> None:
    """Item 3: `permits` checks that the pinned version is of the artifact the ref names."""
    a, _ = seeds
    with opened(a.tenant) as s:
        svc = service_for(s, a.tenant, a.user, settings, set_context=False)
        art = {"id": a.agent, "tenant_id": a.tenant, "kind": "agents", "workspace_id": None,
               "project_id": None, "key": "", "name": "", "status": "active"}  # fmt: skip
        run_env = svc.env(art, run_of=run_row(s, a.tenant, a.running))
        pinned = run_env.version(a.prompt)
        assert pinned is not None and run_env.permits("prompt:read", pinned)
        forged = dataclasses.replace(pinned, artifact_id=a.unpinned_art)
        assert not run_env.permits("prompt:read", forged)


# ------------------------------------------------------------------ SETH tests (moved)
BAD_BASES = ["https://[bad/x", "https://[::1/x", "https://host:notaport/x"]


@pytest.mark.parametrize("base", BAD_BASES)
def test_a_malformed_base_url_is_a_config_error_not_a_crash(base: str) -> None:
    """SETH `test_r1_round4.py`, assertions unchanged."""
    errs = RestHttpAdapter().validate_config({"base_url": base})
    assert errs and errs[0].path == "base_url"
    with pytest.raises(AdapterFailure) as info:
        join_url(base, "/x")
    assert info.value.error_class is ErrorClass.CONFIG
    # a config failure is a failure before anything was sent, never "possibly delivered"
    cls, retryable, maybe_sent = steps.classify_tool_failure(info.value)
    assert maybe_sent is False and retryable is False, (cls, retryable, maybe_sent)


def test_the_journal_copy_of_arguments_can_always_be_stored(owner_db: Database) -> None:
    """SETH `test_r1_round2.py`, assertions unchanged (SETH's `owner_session` is a session of
    `owner_db` here)."""
    from shifde.runtime.steps import _journal_arguments

    copy = _journal_arguments({"k\u0000": ["a\u0000b", {"n": "x\u0000"}], "n": 1})
    assert copy == {"k�": ["a�b", {"n": "x�"}], "n": 1}
    with owner_db.session() as owner_session:
        stored = owner_session.execute(
            text("SELECT CAST(:j AS jsonb)"), {"j": json.dumps(copy)}
        ).scalar_one()
    assert stored == copy  # jsonb refuses U+0000; the copy has none


def test_a_runs_known_cost_is_its_own_clients(
    seeds: tuple[Seed, Seed], opened: Any, owner_db: Database
) -> None:
    """`repository.run_cost` (Item 10) with B's run id answers like an unknown run."""
    a, b = seeds
    with owner_db.session() as o:
        _spend(o, a, "1.5")
        _spend(o, b, "2.5")
        o.commit()
    with opened(a.tenant) as s:
        assert repo.run_cost(s, a.tenant, b.running, "USD") == Decimal(0)
        assert repo.run_cost(s, a.tenant, uuid7(), "USD") == Decimal(0)
        assert repo.run_cost(s, a.tenant, a.running, "USD") == Decimal("1.5")
