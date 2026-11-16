"""Runtime part 7 (RT-7): `agent.py` and `action.py`, the agent loop and the ontology action run.

Behaviour: an agent run reads its agent version, prompt and tools through the run's own env
(`run_of`, B-1933), so `AgentOps.assemble` and the tool set see only what the run's version pins;
`limits.max_cost` reaches every model call (B-2142); a tool call's `VersionRef` and the pinned ids
of its `ToolPolicy` come only from the run's pin set (runtime isolation review 5 item 2); in a
run's env the connections and model routes are only those the pinned specs name, at the run's own
workspace and project (runtime review 3 follow-up); an ontology action run reads its tool only
through its own run's env (pin role `action_tool`).

Isolation: as `shifde_app` and as the schema owner, an agent or action run of client A naming B's
agent, ontology, tool version, model route or connection gets the answer an unknown id gets, and
A's own objects give another answer (not vacuous). `runs.agent_id` / `orchestration_id` /
`prompt_version_id` cannot name another client's or another artifact's row (composite keys).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.base import Usage
from shifde.build import repository as build_repo
from shifde.build.kind_ops import AgentOps
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.models_admin import schemas as ms
from shifde.models_admin.service import RunInvoke
from shifde.ontology.service import OntologyService
from shifde.runtime import repository as repo
from shifde.runtime import state, steps
from shifde.runtime.action import OntologyActionRunner
from shifde.runtime.agent import AgentRunner
from shifde.runtime.context import RunContext
from tests._support.runs import add_run
from tests._support.tenants import add_tenants, add_workspace
from tests.build.env_support import add_published
from tests.build.routes import agent_spec, prompt_spec
from tests.build.service_support import add_person
from tests.ledger.util import add_model, add_rate
from tests.runtime.execution import run_context, run_row
from tests.runtime.test_execution import MODES, opened

__all__ = ["MODES", "opened"]

ANSWER = "the final answer"
NONE_TOOL = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
             "side_effect_class": "none", "input_schema": {"type": "object"}}  # fmt: skip


@dataclass(frozen=True)
class Client:
    tenant: UUID
    user: UUID
    token: UUID
    model: UUID
    route: UUID
    conn_named: UUID  # client-level connection the pinned tool names
    conn_far: UUID  # connection in another workspace, named by another pinned tool
    conn_unnamed: UUID  # client-level connection no pinned spec names
    prompt: UUID
    tool: UUID  # pinned by `agent_version`, names `conn_named`
    tool_key: str
    unpinned: UUID  # a published tool of the client, pinned by nothing
    unpinned_key: str
    agent: UUID
    agent_version: UUID
    run: UUID  # running run of `agent_version`
    route_run: UUID  # running run of an agent version whose model is `route`
    foreign_route_run: UUID  # running run of an agent version whose model is the other's route
    ontology: UUID
    ontology_version: UUID  # pins `tool` as its action tool
    action_run: UUID  # running action run of `ontology_version`


def _connection(o: Session, tenant: UUID, workspace: UUID | None = None) -> UUID:
    cid = uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, workspace_id, name, connection_type, "
            "adapter_key) VALUES (:c, :t, :w, :n, 'model_api', 'openai')"
        ),
        {"c": cid, "t": tenant, "w": workspace, "n": f"conn-{cid.hex}"},
    )
    return cid


def _running(o: Session, tenant: UUID, user: UUID, token: UUID, payload: Any, **cols: Any) -> UUID:
    run = add_run(o, tenant, status="running", lease_token=token, started_at=utcnow(),
                  initiated_by_principal_id=user, **cols)  # fmt: skip
    ref = repo.insert_artifact(o, tenant, run, "input", payload)
    o.execute(text("UPDATE shifde.runs SET input_ref = :r WHERE id = :id"), {"r": ref, "id": run})
    return run


def _agent(
    o: Session, tenant: UUID, spec: dict[str, Any], pins: list[tuple[UUID, str]]
) -> tuple[UUID, UUID]:
    agent, version = add_published(o, tenant, "agents", f"agent-{uuid7().hex}", spec)
    build_repo.insert_pins(o, tenant, version, pins)
    return agent, version


def _first(o: Session, t: UUID) -> dict[str, Any]:  # Any: ids of the first phase
    user = add_person(o, t, "runner", "tenant_administrator")
    model = add_model(o, t)
    add_rate(o, t, input_rate="1000", output_rate="1000")
    route = uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.model_routes (id, tenant_id, name, scope_type, scope_id, "
            "primary_model_id) VALUES (:r, :t, :n, 'tenant', :t, :m)"
        ),
        {"r": route, "t": t, "n": f"route-{route.hex}", "m": model},
    )
    named, unnamed = _connection(o, t), _connection(o, t)
    far = _connection(o, t, add_workspace(o, t))
    _, prompt = add_published(o, t, "prompts", f"prompt-{uuid7().hex}", prompt_spec())
    tool_key, unpinned_key = f"tool-{uuid7().hex}", f"unpinned-{uuid7().hex}"
    _, tool = add_published(
        o, t, "tools", tool_key, {**NONE_TOOL, "binding": {"connection_id": str(named)}}
    )
    _, far_tool = add_published(
        o, t, "tools", f"far-{uuid7().hex}", {**NONE_TOOL, "binding": {"connection_id": str(far)}}
    )
    _, unpinned = add_published(o, t, "tools", unpinned_key, NONE_TOOL)
    return {"tenant": t, "user": user, "token": uuid7(), "model": model, "route": route,
            "conn_named": named, "conn_far": far, "conn_unnamed": unnamed, "prompt": prompt,
            "tool": tool, "tool_key": tool_key, "far_tool": far_tool, "unpinned": unpinned,
            "unpinned_key": unpinned_key}  # fmt: skip


def _second(o: Session, x: dict[str, Any], other: dict[str, Any]) -> Client:  # Any: phase 1
    t, user, token = x["tenant"], x["user"], x["token"]
    prompt = str(x["prompt"])
    tools = [str(x["tool"]), str(x["far_tool"]), str(x["unpinned"]), str(other["tool"])]
    agent, version = _agent(
        o,
        t,
        agent_spec(prompt, str(x["model"]), tool_version_ids=tools),
        [(x["prompt"], "prompt"), (x["tool"], "tool"), (x["far_tool"], "tool")],
    )
    payload = {"input": "hello"}
    run = _running(o, t, user, token, payload, agent_id=agent, agent_version_id=version)

    def route_run(route: UUID) -> UUID:
        spec = agent_spec(prompt, str(x["model"]))
        spec["model"] = {"route_id": str(route)}
        art, ver = _agent(o, t, spec, [(x["prompt"], "prompt")])
        return _running(o, t, user, token, payload, agent_id=art, agent_version_id=ver)

    action = {"key": "act", "tool_version_id": str(x["tool"]), "param_mapping": {}}
    ontology, onto_version = add_published(
        o, t, "ontologies", f"onto-{uuid7().hex}", {"actions": [action]}
    )
    build_repo.insert_pins(o, t, onto_version, [(x["tool"], "action_tool")])
    action_run = _running(o, t, user, token, {"object_pk": "1"}, kind="ontology_action",
                          ontology_id=ontology, ontology_version_id=onto_version,
                          action_key="act")  # fmt: skip
    keep = {k: v for k, v in x.items() if k != "far_tool"}
    return Client(**keep, agent=agent, agent_version=version, run=run,
                  route_run=route_run(x["route"]), foreign_route_run=route_run(other["route"]),
                  ontology=ontology, ontology_version=onto_version,
                  action_run=action_run)  # fmt: skip


@pytest.fixture
def pair(owner_db: Database) -> tuple[Client, Client]:
    ta, tb = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, ta, tb)
        xa, xb = _first(o, ta), _first(o, tb)
        a, b = _second(o, xa, xb), _second(o, xb, xa)
        o.commit()
    return a, b


def _ctx(s: Session, settings: Settings, c: Client, run: UUID, **over: Any) -> RunContext:
    return run_context(s, settings, {**run_row(s, c.tenant, run), **over}, token=c.token)


def _script(mp: pytest.MonkeyPatch, ctx: RunContext, answers: list[str]) -> list[UUID]:
    """The model answers `answers` in order; returns the model ids called."""
    called: list[UUID] = []

    def invoke_for_run(
        ref: object, model_id: UUID, body: ms.InvokeIn, *, run_id: UUID, max_deadline: float
    ) -> RunInvoke:
        called.append(model_id)
        out = ms.InvokeOut(
            output=answers[len(called) - 1],
            usage=ms.UsageOut(input_tokens=1, output_tokens=1),
            latency_ms=1,
        )
        assert body.max_tokens is not None
        return RunInvoke(out, Usage(input_tokens=1, output_tokens=1), body.max_tokens)

    mp.setattr(ctx.artifacts.models, "invoke_for_run", invoke_for_run)
    return called


def _spy_tools(mp: pytest.MonkeyPatch) -> list[tuple[steps.ToolCall, steps.ToolPolicy]]:
    seen: list[tuple[steps.ToolCall, steps.ToolPolicy]] = []

    def run_tool(ctx: RunContext, call: steps.ToolCall, policy: steps.ToolPolicy) -> Any:
        seen.append((call, policy))
        return {"ok": True}

    mp.setattr(steps, "run_tool", run_tool)
    return seen


def _calls(*keys: str) -> str:
    return '{"tool_calls": [' + ", ".join(f'{{"name": "{k}"}}' for k in keys) + "]}"


def _outcome(fn: Callable[[], object]) -> str:
    try:
        return f"ok: {fn()}"
    except (state.RunInterrupt, NotFound) as exc:
        return f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ the agent loop
def test_an_agent_run_answers_through_its_own_env_and_passes_max_cost(
    pair: tuple[Client, Client], opened: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SETH's runner never passed `limits.max_cost`; a ceiling below one call's worst case now
    refuses the call before it is sent."""
    a, _ = pair
    with opened(a.tenant) as s:
        # first the ceiling (the refused call writes no answer the next attempt would replay)
        ctx = _ctx(s, settings, a, a.run)
        called = _script(monkeypatch, ctx, [ANSWER])
        runner = AgentRunner(ctx)
        spec = dict(runner._load_agent().spec)
        spec["limits"] = {**spec["limits"], "max_cost": "0.000001"}
        with monkeypatch.context() as m:
            m.setattr(runner, "_load_agent", lambda: _with_spec(runner, spec))
            with pytest.raises(state.RunFailed) as refused:
                runner.run()
        assert refused.value.error_class == state.BUDGET_DENIED
        assert called == []
        s.rollback()
        # the stored spec (no max_cost): answered through the run's env
        ctx = _ctx(s, settings, a, a.run)
        called = _script(monkeypatch, ctx, [ANSWER])
        assert AgentRunner(ctx).run() == ANSWER
        assert called == [a.model]


def _with_spec(runner: AgentRunner, spec: dict[str, Any]) -> Any:  # Any: VersionRef
    ref = runner.env.version(runner.ctx.run["agent_version_id"])
    assert ref is not None
    return dataclasses.replace(ref, spec=spec)


def test_a_tool_call_carries_only_the_runs_pinned_ref_and_ids(
    pair: tuple[Client, Client], opened: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec lists A's pinned tools, A's unpinned tool and B's tool: the call's `VersionRef`
    and the policy's pinned ids are read from the run's pin set only (review 5 item 2)."""
    a, _ = pair
    seen = _spy_tools(monkeypatch)
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, a.run)
        _script(monkeypatch, ctx, [_calls(a.tool_key), ANSWER])
        assert AgentRunner(ctx).run() == ANSWER
    [(call, policy)] = seen
    assert (call.tool.version_id, call.tool.artifact_key) == (a.tool, a.tool_key)
    assert call.tool.kind == "tools" and call.tool.spec["binding"] == {
        "connection_id": str(a.conn_named)
    }
    assert a.tool in policy.pinned_version_ids and a.unpinned not in policy.pinned_version_ids
    assert len(policy.pinned_version_ids) == 2  # the two pinned tools, nothing the spec only names


@pytest.mark.parametrize("which", ["own unpinned tool", "other client's tool", "unknown name"])
def test_a_tool_the_run_does_not_pin_is_denied_like_an_unknown_one(
    which: str,
    pair: tuple[Client, Client],
    opened: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pair
    name = {"own unpinned tool": a.unpinned_key, "other client's tool": b.tool_key,
            "unknown name": "no-such-tool"}[which]  # fmt: skip
    seen = _spy_tools(monkeypatch)
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, a.run)
        _script(monkeypatch, ctx, [_calls(name), ANSWER])
        with pytest.raises(state.GateDenied) as denied:
            AgentRunner(ctx).run()
    assert denied.value.reason == "tool_not_pinned"
    assert seen == []


def test_a_model_route_is_reached_only_when_the_runs_spec_names_it(
    pair: tuple[Client, Client], opened: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = pair
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, a.route_run)
        called = _script(monkeypatch, ctx, [ANSWER])
        assert AgentRunner(ctx).run() == ANSWER
        assert called == [a.model]
        s.rollback()
        # A's agent naming B's route: refused like a route of nobody
        ctx = _ctx(s, settings, a, a.foreign_route_run)
        called = _script(monkeypatch, ctx, [ANSWER])
        with pytest.raises(state.RunFailed, match="model route is not available"):
            AgentRunner(ctx).run()
        assert called == []
        s.rollback()
        # in a run's env, a route of the client the pinned specs do not name answers None
        env = AgentRunner(_ctx(s, settings, a, a.run)).env
        assert env.route_model_ids(a.route) is None
        assert env.route_model_ids(b.route) is None
        route_env = AgentRunner(_ctx(s, settings, a, a.route_run)).env
        assert route_env.route_model_ids(a.route) == [a.model]
        assert route_env.route_model_ids(b.route) is None


def test_a_runs_env_reaches_only_the_connections_its_pinned_specs_name_at_its_scope(
    pair: tuple[Client, Client], opened: Any, settings: Settings
) -> None:
    a, b = pair
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, a.run)
        env = AgentRunner(ctx).env
        assert env.connection(a.conn_named) is not None  # named by a pinned tool, client level
        for cid in (a.conn_far, a.conn_unnamed, b.conn_named, uuid7()):
            assert env.connection(cid) is None, cid
            assert env.schema_discovery(cid) is None, cid
            assert env.mcp_tool(cid, "x").exists is False, cid
        # the caller's own env (the test panel) still reaches the client's other connections
        art = ctx.artifacts.artifact_row("agents", a.agent)
        assert ctx.artifacts.env(art).connection(a.conn_unnamed) is not None


def test_an_agent_run_naming_bs_agent_gets_the_unknown_id_answer(
    pair: tuple[Client, Client], opened: Any, settings: Settings
) -> None:
    a, b = pair
    answers = {}
    with opened(a.tenant) as s:
        for label, agent, version in (
            ("b", b.agent, b.agent_version),
            ("unknown", uuid7(), uuid7()),
            ("mixed", a.agent, b.agent_version),
        ):
            ctx = _ctx(s, settings, a, a.run, agent_id=agent, agent_version_id=version)
            answers[label] = _outcome(lambda c=ctx: AgentRunner(c).run())
            s.rollback()
    assert answers["b"] == answers["unknown"] == answers["mixed"]
    assert answers["b"].startswith("RunFailed: ")


def test_assemble_for_a_run_reads_only_the_runs_pins(
    pair: tuple[Client, Client], opened: Any, settings: Settings
) -> None:
    a, b = pair
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, a.run)
        art = ctx.artifacts.artifact_row("agents", a.agent)
        ops = AgentOps(ctx.artifacts)
        spec = agent_spec(str(a.prompt), str(a.model))
        messages, _ = ops.assemble(art, spec, "hi", {}, run_of=ctx.run)
        assert messages
        with pytest.raises(NotFound, match="run not found"):  # B's run
            ops.assemble(art, spec, "hi", {}, run_of={**ctx.run, "id": b.run})
        # a prompt the run does not pin (B's) is not available, like an unknown one
        for prompt in (b.prompt, uuid7()):
            with pytest.raises(Exception, match="not available"):
                ops.assemble(
                    art, {**spec, "prompt_version_id": str(prompt)}, "hi", {}, run_of=ctx.run
                )


# ------------------------------------------------------------------ ontology actions
def _fake_inputs(mp: pytest.MonkeyPatch, tool: UUID) -> None:
    """The object resolution (G3: no source in tests) answers one object; the action names
    `tool`, as a stored action spec would."""

    def action_inputs(self: OntologyService, *args: Any, **kwargs: Any) -> Any:
        return {"key": "act", "tool_version_id": str(tool), "param_mapping": {}}, {}, {"id": 1}

    mp.setattr(OntologyService, "action_inputs", action_inputs)


def test_an_action_runs_only_the_tool_its_ontology_version_pins(
    pair: tuple[Client, Client], opened: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = pair
    seen = _spy_tools(monkeypatch)
    answers = {}
    with opened(a.tenant) as s:
        for label, tool in (("pinned", a.tool), ("unpinned", a.unpinned), ("b", b.tool),
                            ("unknown", uuid7())):  # fmt: skip
            _fake_inputs(monkeypatch, tool)
            ctx = _ctx(s, settings, a, a.action_run)
            answers[label] = _outcome(lambda c=ctx: OntologyActionRunner(c).run()["action"])
            s.rollback()
    assert answers["pinned"] == "ok: act"
    assert answers["unpinned"] == answers["b"] == answers["unknown"]
    assert answers["b"].startswith("RunFailed: ")
    [(call, policy)] = seen
    assert call.tool.version_id == a.tool and policy.pinned_version_ids == frozenset({a.tool})


def test_an_action_run_naming_bs_ontology_gets_the_unknown_id_answer(
    pair: tuple[Client, Client], opened: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = pair
    seen = _spy_tools(monkeypatch)
    _fake_inputs(monkeypatch, a.tool)
    answers = {}
    with opened(a.tenant) as s:
        for label, onto, version in (
            ("b", b.ontology, b.ontology_version),
            ("unknown", uuid7(), uuid7()),
            ("mixed", a.ontology, b.ontology_version),
        ):
            ctx = _ctx(s, settings, a, a.action_run, ontology_id=onto, ontology_version_id=version)
            answers[label] = _outcome(lambda c=ctx: OntologyActionRunner(c).run())
            s.rollback()
    assert answers["b"] == answers["unknown"] == answers["mixed"]
    assert answers["b"].startswith("RunFailed: ")
    assert seen == []


# ------------------------------------------------------------------ composite keys (B-2125)
@pytest.mark.parametrize("column", ["agent_id", "agent_id_other", "prompt_version_id"])
def test_a_run_cannot_name_another_clients_or_another_artifacts_row(
    column: str, pair: tuple[Client, Client], owner_db: Database
) -> None:
    """As the schema owner (constraints are the only guard there): A's run naming B's agent, an
    artifact of A that is not its version's, or B's prompt version is refused."""
    a, b = pair
    with owner_db.session() as o:
        other_agent, _ = add_published(o, a.tenant, "agents", f"x-{uuid7().hex}", {})
        value = {"agent_id": b.agent, "agent_id_other": other_agent,
                 "prompt_version_id": b.prompt}[column]  # fmt: skip
        name = "agent_id" if column.startswith("agent_id") else column
        with pytest.raises(IntegrityError, match="fk_runs__"):
            add_run(o, a.tenant, agent_version_id=a.agent_version,
                    **({"agent_id": a.agent} if name != "agent_id" else {}),
                    **{name: value})  # fmt: skip
        o.rollback()
        # A's own pairing is accepted (not vacuous)
        add_run(o, a.tenant, agent_id=a.agent, agent_version_id=a.agent_version,
                prompt_version_id=a.prompt)  # fmt: skip
        o.rollback()
