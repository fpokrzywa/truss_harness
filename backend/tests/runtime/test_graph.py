"""Runtime part 8 (RT-8): `graph.py`, the orchestration executor.

Behaviour: an orchestration run reads its nodes' tool and agent versions through the run's own env
(`run_of`, B-1933), so a node's version is answered only when the run's pinned orchestration
version pins it and only for the node's kind; a tool node's `ToolCall.tool` and the policy's
pinned ids come from that set (B-2162); an agent node starts a child run in the parent's client,
workspace, project and root (the parent waits for it, the child's output becomes the node's);
approval nodes pause until decided; caps stop the graph; a child that ended with an unknown
outcome fails the orchestration with that class (C1, B-2122).

Isolation, as `shifde_app` and as the schema owner: an orchestration run of client A naming B's
tool, agent or prompt version (or A's own unpinned one, or one of another kind) gets the answer an
unknown id gets and starts nothing; a context naming B's run under A's client id gets the
unknown-run answer and changes none of B's rows; a step can name only a run of its own client
(composite key) and the executor reads it only when it is the run's own child.

SETH's `test_p5_graph.py` drives the API and the worker (deferred, carry_forward "Runtime part 8");
the tests below are direct counterparts of its executor tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.build import repository as build_repo
from shifde.build.executors import ONTOLOGY_QUERY
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ontology.service import OntologyService
from shifde.runtime import repository as repo
from shifde.runtime import state, steps
from shifde.runtime.context import RunContext
from shifde.runtime.graph import GraphRunner
from tests._support.runs import add_run
from tests._support.tenants import add_project, add_tenants, add_workspace
from tests.build.env_support import add_published
from tests.build.routes import agent_spec, prompt_spec
from tests.build.service_support import add_person
from tests.runtime.conftest import insert
from tests.runtime.execution import client_state, run_context, run_row
from tests.runtime.test_execution import MODES, opened

__all__ = ["MODES", "opened"]

NONE_TOOL = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
             "side_effect_class": "none", "input_schema": {"type": "object"}}  # fmt: skip
LIMITS = {"max_nodes_executed": 10, "max_depth": 3, "max_total_tokens": 100000,
          "deadline_s": 120, "max_parallel": 4}  # fmt: skip


@dataclass(frozen=True)
class Client:
    tenant: UUID
    user: UUID
    token: UUID
    prompt: UUID
    agent_art: UUID
    agent: UUID  # pins `prompt`
    loose_agent: UUID  # names `prompt` but pins nothing
    foreign_prompt_agent: UUID  # names the other client's prompt
    tool: UUID
    unpinned_tool: UUID  # a published tool of the client, pinned by no orchestration


def _key(kind: str) -> str:
    return f"{kind}-{uuid7().hex}"


def _first(o: Session, t: UUID) -> dict[str, Any]:  # Any: ids of the first phase
    user = add_person(o, t, "runner", "tenant_administrator")
    _, prompt = add_published(o, t, "prompts", _key("prompt"), prompt_spec())
    model = str(uuid7())  # never called: the child agent runs are not executed here
    agent_art, agent = add_published(o, t, "agents", _key("agent"), agent_spec(str(prompt), model))
    build_repo.insert_pins(o, t, agent, [(prompt, "prompt")])
    _, loose = add_published(o, t, "agents", _key("loose"), agent_spec(str(prompt), model))
    _, tool = add_published(o, t, "tools", _key("tool"), NONE_TOOL)
    _, unpinned = add_published(o, t, "tools", _key("unpinned"), NONE_TOOL)
    return {"tenant": t, "user": user, "token": uuid7(), "prompt": prompt, "agent_art": agent_art,
            "agent": agent, "loose_agent": loose, "tool": tool,
            "unpinned_tool": unpinned}  # fmt: skip


def _second(o: Session, x: dict[str, Any], other: dict[str, Any]) -> Client:  # Any: phase 1
    # pins its own prompt (so only the spec's name is foreign)
    _, foreign = add_published(
        o, x["tenant"], "agents", _key("foreign"), agent_spec(str(other["prompt"]), str(uuid7()))
    )
    build_repo.insert_pins(o, x["tenant"], foreign, [(x["prompt"], "prompt")])
    return Client(**x, foreign_prompt_agent=foreign)


@pytest.fixture
def pair(owner_db: Database) -> tuple[Client, Client]:
    ta, tb = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, ta, tb)
        xa, xb = _first(o, ta), _first(o, tb)
        a, b = _second(o, xa, xb), _second(o, xb, xa)
        o.commit()
    return a, b


def _orch(
    owner_db: Database,
    c: Client,
    nodes: list[dict[str, Any]],  # Any: node JSON
    edges: Sequence[dict[str, str]] = (),
    pins: Sequence[tuple[UUID, str]] = (),
    **limits: int,
) -> UUID:
    """A running run (lease `c.token`) of a published orchestration version of `c` with `nodes`."""
    spec = {"nodes": nodes, "edges": list(edges), "limits": {**LIMITS, **limits}}
    with owner_db.session() as o:
        art, ver = add_published(o, c.tenant, "orchestrations", _key("orch"), spec)
        build_repo.insert_pins(o, c.tenant, ver, list(pins))
        run = add_run(o, c.tenant, kind="orchestration", orchestration_id=art,
                      orchestration_version_id=ver, status="running", lease_token=c.token,
                      started_at=utcnow(), initiated_by_principal_id=c.user)  # fmt: skip
        ref = repo.insert_artifact(o, c.tenant, run, "input", {"input": "hello"})
        o.execute(
            text("UPDATE shifde.runs SET input_ref = :r WHERE id = :id"), {"r": ref, "id": run}
        )
        o.commit()
    return run


def _tool(nid: str, version: UUID) -> dict[str, Any]:  # Any: node JSON
    return {"id": nid, "type": "tool", "ref": {"tool_version_id": str(version)},
            "input_map": {"item": "{{ input }}"}}  # fmt: skip


def _agent(nid: str, version: UUID) -> dict[str, Any]:  # Any: node JSON
    return {"id": nid, "type": "agent", "ref": {"agent_version_id": str(version)},
            "input_map": {"input": "{{ input }}"}}  # fmt: skip


def _ctx(s: Session, settings: Settings, c: Client, run: UUID, **over: Any) -> RunContext:
    return run_context(s, settings, {**run_row(s, c.tenant, run), **over}, token=c.token)


def _spy_tools(mp: pytest.MonkeyPatch) -> list[tuple[steps.ToolCall, steps.ToolPolicy]]:
    seen: list[tuple[steps.ToolCall, steps.ToolPolicy]] = []

    def run_tool(ctx: RunContext, call: steps.ToolCall, policy: steps.ToolPolicy) -> Any:
        seen.append((call, policy))
        return {"ok": True, "item": call.args.get("item")}

    mp.setattr(steps, "run_tool", run_tool)
    return seen


def _children(owner_db: Database, run: UUID) -> list[dict[str, Any]]:  # Any: run rows
    with owner_db.session() as o:
        return [
            dict(r)
            for r in o.execute(
                text("SELECT * FROM shifde.runs WHERE parent_run_id = :r ORDER BY node_id"),
                {"r": run},
            ).mappings()
        ]


def _resume(owner_db: Database, c: Client, run: UUID) -> None:
    """The wake and the worker's claim of a parent, reduced to its transitions: running again
    under `c.token`."""
    with owner_db.session() as o:
        assert repo.transition(o, c.tenant, run, state.WAITING, state.PENDING, waiting_reason=None)
        assert repo.transition(o, c.tenant, run, state.PENDING, state.RUNNING, lease_token=c.token)
        o.commit()


def _end_child(owner_db: Database, c: Client, child: UUID, status: str, **cols: Any) -> None:
    """`child` (pending) runs and ends in `status` with an output."""
    with owner_db.session() as o:
        ref = repo.insert_artifact(o, c.tenant, child, "output", {"output": "child answer"})
        assert repo.transition(o, c.tenant, child, state.PENDING, state.RUNNING)
        assert repo.transition(o, c.tenant, child, state.RUNNING, status, output_ref=ref,
                               finished_at=utcnow(), **cols)  # fmt: skip
        o.commit()


# ------------------------------------------------------------------ tool and condition nodes
def test_a_tool_node_runs_only_the_runs_pinned_tool_and_a_condition_reads_its_output(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    nodes = [
        _tool("t", a.tool),
        {"id": "c", "type": "condition", "condition": "nodes.t.output.ok"},
    ]
    run = _orch(owner_db, a, nodes, [{"from": "t", "to": "c", "when": "always"}],
                [(a.tool, "tool")])  # fmt: skip
    seen = _spy_tools(monkeypatch)
    with opened(a.tenant) as s:
        assert GraphRunner(_ctx(s, settings, a, run)).run() == {"result": True}
    [(call, policy)] = seen
    assert (call.tool.version_id, call.tool.kind, call.args) == (a.tool, "tools", {"item": "hello"})
    assert policy.pinned_version_ids == frozenset({a.tool})
    with owner_db.session() as o:
        recorded = o.execute(
            text("SELECT tenant_id, node_id, status FROM shifde.run_steps WHERE run_id = :r "
                 "ORDER BY seq"), {"r": run}
        ).all()  # fmt: skip
    assert [tuple(r) for r in recorded] == [
        (a.tenant, "t", "succeeded"),
        (a.tenant, "c", "succeeded"),
    ]


TOOL_CASES = ["unknown id", "own unpinned tool", "other client's tool", "own agent as a tool"]
AGENT_CASES = ["unknown id", "other client's agent", "own unpinned agent", "own tool as an agent",
               "agent whose prompt is not pinned", "agent naming the other client's prompt"]  # fmt: skip


@pytest.mark.parametrize(
    ("node", "case"), [("tool", c) for c in TOOL_CASES] + [("agent", c) for c in AGENT_CASES]
)
def test_a_node_naming_what_the_run_does_not_pin_is_refused_like_an_unknown_id(
    node: str,
    case: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pair
    pins = [(a.tool, "tool"), (a.agent, "agent")]
    target = {"unknown id": uuid7(), "own unpinned tool": a.unpinned_tool,
              "other client's tool": b.tool, "own agent as a tool": a.agent,
              "other client's agent": b.agent, "own unpinned agent": a.loose_agent,
              "own tool as an agent": a.tool, "agent whose prompt is not pinned": a.loose_agent,
              "agent naming the other client's prompt": a.foreign_prompt_agent}[case]  # fmt: skip
    if case == "agent whose prompt is not pinned":
        # nothing else in the run's pin set pins `prompt` either
        pins = [(a.tool, "tool"), (a.loose_agent, "agent")]
    if case == "agent naming the other client's prompt":
        pins.append((a.foreign_prompt_agent, "agent"))
    run = _orch(owner_db, a, [(_tool if node == "tool" else _agent)("n", target)], pins=pins)
    seen = _spy_tools(monkeypatch)
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        GraphRunner(_ctx(s, settings, a, run)).run()
    assert (refused.value.error_class, str(refused.value)) == (
        state.VALIDATION,
        f"node n: the {node} is not available",
    )
    assert seen == [] and _children(owner_db, run) == []
    assert client_state_of(owner_db, b.tenant) == before


def client_state_of(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        return client_state(o, tenant)


# ------------------------------------------------------------------ agent nodes (child runs)
def test_an_agent_node_starts_a_child_in_the_parents_client_root_and_scope_only(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    run = _orch(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")])
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s:
        with pytest.raises(state.Waiting) as waiting:
            GraphRunner(_ctx(s, settings, a, run)).run()
        assert (waiting.value.reason, waiting.value.transitioned) == ("child_runs", True)
    [child] = _children(owner_db, run)
    with owner_db.session() as o:
        parent = dict(
            o.execute(text("SELECT * FROM shifde.runs WHERE id = :r"), {"r": run}).mappings().one()
        )
        jobs = o.execute(
            text("SELECT tenant_id FROM shifde.jobs WHERE run_id = :c"), {"c": child["id"]}
        ).scalars().all()  # fmt: skip
        payload = repo.get_artifact(o, a.tenant, child["input_ref"])
    assert (parent["status"], parent["waiting_reason"]) == (state.WAITING, "child_runs")
    same = ("tenant_id", "workspace_id", "project_id", "root_run_id", "initiated_by_principal_id",
            "principal_type", "trace_id", "correlation_id", "deadline_at", "is_test")  # fmt: skip
    assert {k: child[k] for k in same} == {k: parent[k] for k in same}
    assert (child["parent_run_id"], child["depth"], child["kind"], child["status"]) == (
        run, 1, "agent", state.PENDING)  # fmt: skip
    assert (child["agent_id"], child["agent_version_id"], child["prompt_version_id"]) == (
        a.agent_art, a.agent, a.prompt)  # fmt: skip
    assert jobs == [a.tenant] and payload == {"input": "hello", "variables": {}}
    assert client_state_of(owner_db, b.tenant) == before
    # the child finishes; the woken parent records its output as the node's
    _end_child(owner_db, a, child["id"], state.COMPLETED)
    _resume(owner_db, a, run)
    with opened(a.tenant) as s:
        assert GraphRunner(_ctx(s, settings, a, run)).run() == "child answer"


def test_a_child_that_ended_with_an_unknown_outcome_fails_the_orchestration_with_that_class(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """C1 (B-2122): a child cancelled during a write ends `indeterminate_side_effect`; the parent
    reports that class, never a success."""
    a, _ = pair
    run = _orch(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")])
    with opened(a.tenant) as s, pytest.raises(state.Waiting):
        GraphRunner(_ctx(s, settings, a, run)).run()
    [child] = _children(owner_db, run)
    _end_child(owner_db, a, child["id"], state.CANCELLED, error_class=state.INDETERMINATE)
    _resume(owner_db, a, run)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as failed:
        GraphRunner(_ctx(s, settings, a, run)).run()
    assert failed.value.error_class == state.INDETERMINATE
    with owner_db.session() as o:
        done = o.execute(
            text(
                "SELECT count(*) FROM shifde.run_steps WHERE run_id = :r AND status = 'succeeded'"
            ),
            {"r": run},
        ).scalar_one()
    assert done == 0


def test_a_step_is_read_only_through_the_runs_own_child(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """A step naming another run of the same client (finished, with an output) is not this run's
    child: the node fails as with a missing child and the other run's output is not read. A step
    naming B's run cannot exist (composite key)."""
    a, b = pair
    run = _orch(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")])
    with owner_db.session() as o:
        other = add_run(o, a.tenant, agent_id=a.agent_art, agent_version_id=a.agent)
        b_run = add_run(o, b.tenant, agent_id=b.agent_art, agent_version_id=b.agent)
        o.commit()
    _end_child(owner_db, a, other, state.COMPLETED)
    with owner_db.session() as o:
        insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=run, seq=1,
               kind="orchestration_node", name="agent:n", node_id="n", status="running",
               child_run_id=other)  # fmt: skip
        o.commit()
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as failed:
        GraphRunner(_ctx(s, settings, a, run)).run()
    assert (failed.value.error_class, str(failed.value)) == (
        state.INTERNAL,
        "node n: its child run is missing",
    )
    with owner_db.session() as o, pytest.raises(IntegrityError, match="fk_run_steps__child_run"):
        insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=run, seq=2,
               kind="orchestration_node", name="agent:n", node_id="n", status="running",
               child_run_id=b_run)  # fmt: skip


@pytest.mark.parametrize("which", ["other client's run", "unknown run"])
def test_a_context_naming_bs_run_under_as_id_gets_the_unknown_run_answer(
    which: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pair
    a_run = _orch(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")])
    b_run = _orch(owner_db, b, [_tool("t", b.tool)], pins=[(b.tool, "tool")])
    with owner_db.session() as o:
        row = run_row(o, b.tenant, b_run) if which == "other client's run" else {
            **run_row(o, a.tenant, a_run), "id": uuid7()}  # fmt: skip
    seen = _spy_tools(monkeypatch)
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        GraphRunner(run_context(s, settings, {**row, "tenant_id": a.tenant}, token=b.token)).run()
    assert (refused.value.error_class, str(refused.value)) == (
        state.VALIDATION,
        "the pinned orchestration is not available",
    )
    assert seen == []
    assert client_state_of(owner_db, b.tenant) == before


# ------------------------------------------------------------------ approval nodes and caps
def test_an_approval_node_pauses_until_a_human_decides(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, _ = pair
    run = _orch(owner_db, a, [{"id": "m", "type": "approval", "input_map": {"why": "{{ input }}"}}])
    with opened(a.tenant) as s, pytest.raises(state.Waiting) as waiting:
        GraphRunner(_ctx(s, settings, a, run)).run()
    assert waiting.value.reason == "approval"
    with owner_db.session() as o:
        appr = o.execute(
            text("SELECT a.tenant_id, a.workspace_id = r.workspace_id AND a.project_id = "
                 "r.project_id, a.call_ref, a.arguments FROM shifde.approvals a JOIN shifde.runs "
                 "r ON r.id = a.run_id WHERE a.run_id = :r"), {"r": run}
        ).one()  # fmt: skip
        assert tuple(appr) == (a.tenant, True, "node:m", {"why": "hello"})
        o.execute(
            text("UPDATE shifde.approvals SET status = 'approved', decision_note = 'ok', "
                 "decided_by = requested_by, decided_at = now() "
                 "WHERE run_id = :r"), {"r": run}
        )  # fmt: skip
        o.commit()
    with opened(a.tenant) as s:
        assert GraphRunner(_ctx(s, settings, a, run)).run() == {"approved": True, "comment": "ok"}


@pytest.mark.parametrize("cap", ["max_nodes_executed", "max_depth"])
def test_caps_stop_the_graph(
    cap: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    if cap == "max_nodes_executed":
        nodes = [_tool("t1", a.tool), _tool("t2", a.tool)]
        edges = [{"from": "t1", "to": "t2", "when": "always"}]
        run = _orch(owner_db, a, nodes, edges, [(a.tool, "tool")], max_nodes_executed=1)
    else:
        run = _orch(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")], max_depth=0)
    seen = _spy_tools(monkeypatch)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as stopped:
        GraphRunner(_ctx(s, settings, a, run)).run()
    assert stopped.value.error_class == state.BUDGET_DENIED and cap in str(stopped.value)
    assert len(seen) == (1 if cap == "max_nodes_executed" else 0)
    assert _children(owner_db, run) == []


# ------------------------------------------------------------------ runtime isolation review 7
def test_a_pinned_tool_carrying_a_pinned_prompt_is_not_an_agent(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Review 7 item 2 (B-2191): a pinned tool version whose spec names a pinned prompt, used as
    an agent node, is refused like an unknown agent and starts no child (the kind check)."""
    a, b = pair
    with owner_db.session() as o:
        _, tool = add_published(o, a.tenant, "tools", _key("prompted"),
                                {**NONE_TOOL, "prompt_version_id": str(a.prompt)})  # fmt: skip
        build_repo.insert_pins(o, a.tenant, tool, [(a.prompt, "prompt")])
        o.commit()
    run = _orch(owner_db, a, [_agent("n", tool)], pins=[(tool, "tool")])
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        GraphRunner(_ctx(s, settings, a, run)).run()
    assert (refused.value.error_class, str(refused.value)) == (
        state.VALIDATION,
        "node n: the agent is not available",
    )
    assert _children(owner_db, run) == []
    assert client_state_of(owner_db, b.tenant) == before


def test_a_child_takes_its_scope_from_the_stored_run_not_the_contexts_mapping(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Review 7 item 1 (B-2190): a context whose mapping names another workspace, project, root,
    test flag or deadline starts the child where the stored run is."""
    a, _ = pair
    run = _orch(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")])
    with owner_db.session() as o:
        far_ws = add_workspace(o, a.tenant)
        far_proj = add_project(o, a.tenant, far_ws)
        o.commit()
    forged = {"workspace_id": far_ws, "project_id": far_proj, "root_run_id": uuid7(),
              "is_test": True, "deadline_at": utcnow() + timedelta(days=9)}  # fmt: skip
    with opened(a.tenant) as s, pytest.raises(state.Waiting):
        GraphRunner(_ctx(s, settings, a, run, **forged)).run()
    [child] = _children(owner_db, run)
    with owner_db.session() as o:
        parent = run_row(o, a.tenant, run)
    same = ("workspace_id", "project_id", "root_run_id", "is_test", "deadline_at")
    assert {k: child[k] for k in same} == {k: parent[k] for k in same}
    assert child["workspace_id"] != far_ws and child["root_run_id"] == run


def test_an_orchestration_version_other_than_the_stored_runs_is_refused(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Review 7 item 1 (B-2190): the version executed is the stored run's."""
    a, _ = pair
    run = _orch(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")])
    other = _orch(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")])
    with owner_db.session() as o:
        version = run_row(o, a.tenant, other)["orchestration_version_id"]
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        GraphRunner(_ctx(s, settings, a, run, orchestration_version_id=version)).run()
    assert str(refused.value) == "the pinned orchestration is not available"


def test_the_version_executed_is_re_read_from_the_stored_run_when_the_run_starts(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 7 item 1 (B-2190): `run()` executes the stored run's version, re-read when it
    starts, not the context's mapping: a run whose stored row names another orchestration after
    the runner was set up runs nothing."""
    a, _ = pair
    run = _orch(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")])
    other = _orch(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")])
    seen = _spy_tools(monkeypatch)
    with opened(a.tenant) as s:
        runner = GraphRunner(_ctx(s, settings, a, run))
        with owner_db.session() as o:
            moved = run_row(o, a.tenant, other)
            o.execute(
                text("UPDATE shifde.runs SET orchestration_id = :o, orchestration_version_id = :v "
                     "WHERE id = :r"),
                {"o": moved["orchestration_id"], "v": moved["orchestration_version_id"], "r": run},
            )  # fmt: skip
            o.commit()
        with pytest.raises(state.RunFailed) as refused:
            runner.run()
    assert str(refused.value) == "the pinned orchestration is not available"
    assert seen == []


def test_an_ontology_query_tool_in_an_orchestration_run_fails_closed(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Review 7 item 3 (B-2193): the agent-tool rule of `_run_reads` opens only agent runs, so an
    orchestration that pins the `ontology.query` tool but not the ontology version reads nothing:
    the unknown-run answer."""
    a, _ = pair
    ontology_tool = {**NONE_TOOL, "binding": {"function_key": ONTOLOGY_QUERY}}
    with owner_db.session() as o:
        _, tool = add_published(o, a.tenant, "tools", _key("ontq"), ontology_tool)
        onto_art, onto = add_published(o, a.tenant, "ontologies", _key("onto"),
                                       {"object_types": []})  # fmt: skip
        o.commit()
    run = _orch(owner_db, a, [_tool("t", tool)], pins=[(tool, "tool")])
    with opened(a.tenant) as s:
        svc = OntologyService(_ctx(s, settings, a, run).artifacts)
        with pytest.raises(NotFound, match="run not found"):
            svc._artifact(onto_art, api=False, run_id=run, version_id=onto, agent_tool=True)
