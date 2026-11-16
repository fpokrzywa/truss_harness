"""Runtime part 9 (RT-9): `pipeline.py`, the pipeline executor.

Behaviour: a pipeline run re-validates its pinned spec through the run's own env (`run_of`,
B-1933), so a tool, agent or prompt node is answered only for a version the run's pinned pipeline
version pins, of the node's kind; a tool node's `ToolCall.tool` and the policy's pinned ids come
from that set (B-2162); an agent node starts a child run in the stored run's client, workspace,
project and root with a prompt from the run's pin set; a connection node reaches only a connection
the pinned specs name at the client level or the run's own workspace and project, and the session
re-reads it at that scope; a write node caught mid-write ends with an open decision (C1).

Isolation, as `shifde_app` and as the schema owner: B's run, version, connection, tool, agent or
prompt answers like an unknown id and changes none of B's rows; a step names only a run of its own
client and the executor reads it only when it is the run's own child; lineage and artifacts can
only be written for a run of the writer's client (composite keys).

SETH's pipeline tests drive the API and the worker (deferred, carry_forward "Runtime part 9"); the
two that need only ported files are moved below with their assertions unchanged.
"""

from __future__ import annotations

import inspect
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
from shifde.build.service import ArtifactService
from shifde.connections import repository as conn_repo
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ontology.service import OntologyService
from shifde.runtime import repository as repo
from shifde.runtime import state, steps
from shifde.runtime.context import RunContext
from shifde.runtime.pipeline import MAX_DEPTH, PipelineRunner
from shifde.runtime.pipeline_ops import sanitize_json
from tests._support.runs import add_run
from tests._support.tenants import add_project, add_tenants, add_workspace
from tests.build.env_support import add_published
from tests.build.routes import agent_spec, prompt_spec
from tests.build.service_support import add_person
from tests.connections.conftest import connection_values
from tests.runtime.conftest import insert
from tests.runtime.execution import client_state, run_context, run_row
from tests.runtime.test_execution import MODES, opened

__all__ = ["MODES", "opened"]

NONE_TOOL = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
             "side_effect_class": "none", "input_schema": {"type": "object"}}  # fmt: skip
PARAMS = {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "doc": {"type": "object"}},
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Client:
    tenant: UUID
    user: UUID
    token: UUID
    workspace: UUID
    project: UUID
    prompt: UUID
    agent_art: UUID
    agent: UUID  # pins `prompt`
    loose_agent: UUID  # names `prompt`, pins nothing
    tool: UUID
    unpinned_tool: UUID
    conn: UUID  # client-level REST connection (read_write)
    far_conn: UUID  # REST connection in another workspace of the client


def _key(kind: str) -> str:
    return f"{kind}-{uuid7().hex}"


def _connection(o: Session, tenant: UUID, **over: Any) -> UUID:
    values = connection_values(tenant, access_mode="read_write", **over)
    conn_repo.insert_connection(o, values)
    o.execute(
        text("UPDATE shifde.connections SET status = 'active' WHERE id = :i"), {"i": values["id"]}
    )
    return UUID(str(values["id"]))


def _client(o: Session, t: UUID) -> Client:
    user = add_person(o, t, "runner", "tenant_administrator")
    ws = add_workspace(o, t)
    proj = add_project(o, t, ws)
    far_ws = add_workspace(o, t)
    _, prompt = add_published(o, t, "prompts", _key("prompt"), prompt_spec())
    model = str(uuid7())  # never called: child agent runs are not executed here
    agent_art, agent = add_published(o, t, "agents", _key("agent"), agent_spec(str(prompt), model))
    build_repo.insert_pins(o, t, agent, [(prompt, "prompt")])
    _, loose = add_published(o, t, "agents", _key("loose"), agent_spec(str(prompt), model))
    _, tool = add_published(o, t, "tools", _key("tool"), NONE_TOOL)
    _, unpinned = add_published(o, t, "tools", _key("unpinned"), NONE_TOOL)
    conn = _connection(o, t)
    far = _connection(o, t, workspace_id=far_ws)
    return Client(t, user, uuid7(), ws, proj, prompt, agent_art, agent, loose, tool, unpinned,
                  conn, far)  # fmt: skip


@pytest.fixture
def pair(owner_db: Database) -> tuple[Client, Client]:
    ta, tb = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, ta, tb)
        a, b = _client(o, ta), _client(o, tb)
        o.commit()
    return a, b


def _spec(nodes: list[dict[str, Any]], edges: Sequence[dict[str, str]] = ()) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "params": PARAMS,
        "policies": {"max_parallel": 2, "deadline_seconds": 600, "on_failure": "fail_fast"},
        "nodes": nodes,
        "edges": list(edges),
    }


def _pipe(
    owner_db: Database,
    c: Client,
    nodes: list[dict[str, Any]],  # Any: node JSON
    edges: Sequence[dict[str, str]] = (),
    pins: Sequence[tuple[UUID, str]] = (),
    params: dict[str, Any] | None = None,  # Any: run params
) -> UUID:
    """A running run (lease `c.token`) of a published pipeline version of `c` with `nodes`, in
    `c`'s workspace and project."""
    with owner_db.session() as o:
        art, ver = add_published(o, c.tenant, "pipelines", _key("pipe"), _spec(nodes, edges))
        build_repo.insert_pins(o, c.tenant, ver, list(pins))
        run = add_run(o, c.tenant, kind="pipeline", pipeline_id=art, pipeline_version_id=ver,
                      workspace_id=c.workspace, project_id=c.project, status="running",
                      lease_token=c.token, started_at=utcnow(),
                      initiated_by_principal_id=c.user)  # fmt: skip
        ref = repo.insert_artifact(o, c.tenant, run, "input", {"params": params or {}})
        o.execute(
            text("UPDATE shifde.runs SET input_ref = :r WHERE id = :id"), {"r": ref, "id": run}
        )
        o.commit()
    return run


def _ctx(s: Session, settings: Settings, c: Client, run: UUID, **over: Any) -> RunContext:
    return run_context(s, settings, {**run_row(s, c.tenant, run), **over}, token=c.token)


def _tool(nid: str, version: UUID) -> dict[str, Any]:  # Any: node JSON
    return {"id": nid, "type": "tool", "config": {"tool_version_id": str(version)},
            "inputs": {"args": "$params.doc"}}  # fmt: skip


def _agent(nid: str, version: UUID) -> dict[str, Any]:  # Any: node JSON
    return {"id": nid, "type": "agent", "config": {"agent_version_id": str(version)},
            "inputs": {"input": "$params.doc"}}  # fmt: skip


def _api(nid: str, conn: UUID, method: str = "GET") -> dict[str, Any]:  # Any: node JSON
    return {"id": nid, "type": "api_request",
            "config": {"connection_id": str(conn), "path_template": "/x", "method": method}}  # fmt: skip


def _spy_tools(mp: pytest.MonkeyPatch) -> list[tuple[steps.ToolCall, steps.ToolPolicy]]:
    seen: list[tuple[steps.ToolCall, steps.ToolPolicy]] = []

    def run_tool(ctx: RunContext, call: steps.ToolCall, policy: steps.ToolPolicy) -> Any:
        seen.append((call, policy))
        return {"ok": True}

    mp.setattr(steps, "run_tool", run_tool)
    return seen


def _rows(owner_db: Database, sql: str, **params: Any) -> list[dict[str, Any]]:  # Any: rows
    with owner_db.session() as o:
        return [dict(r) for r in o.execute(text(sql), params).mappings()]


def _children(owner_db: Database, run: UUID) -> list[dict[str, Any]]:  # Any: run rows
    return _rows(owner_db, "SELECT * FROM shifde.runs WHERE parent_run_id = :r", r=run)


def client_state_of(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        return client_state(o, tenant)


# ------------------------------------------------------------------ pure nodes and artifacts
def test_a_pipeline_runs_its_pinned_spec_and_stores_its_artifacts_in_its_own_client(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    nodes = [
        {"id": "c", "type": "condition", "config": {"expression": "params.n > 1"}},
        {
            "id": "s",
            "type": "sink",
            "config": {"target": "run_artifact"},
            "inputs": {"document": "$params.doc"},
        },  # fmt: skip
    ]
    run = _pipe(owner_db, a, nodes, [{"from": "c", "to": "s", "when": "true"}],
                params={"n": 2, "doc": {"k": "v"}})  # fmt: skip
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s:
        out = PipelineRunner(_ctx(s, settings, a, run)).run()
    artifact = UUID(out["artifacts"]["s"])
    [row] = _rows(owner_db, "SELECT tenant_id, run_id, content FROM shifde.run_artifacts "
                  "WHERE id = :i", i=artifact)  # fmt: skip
    assert (row["tenant_id"], row["run_id"], row["content"]) == (a.tenant, run, {"k": "v"})
    tenants = _rows(owner_db, "SELECT DISTINCT tenant_id FROM shifde.run_steps WHERE run_id = :r",
                    r=run)  # fmt: skip
    assert tenants == [{"tenant_id": a.tenant}]
    assert client_state_of(owner_db, b.tenant) == before


# ------------------------------------------------------------------ tool nodes
def test_a_tool_node_runs_only_the_runs_pinned_tool(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    run = _pipe(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")], params={"doc": {}})
    seen = _spy_tools(monkeypatch)
    with opened(a.tenant) as s:
        PipelineRunner(_ctx(s, settings, a, run)).run()
    [(call, policy)] = seen
    assert call.tool.version_id == a.tool and call.tool.kind == "tools"
    assert policy.pinned_version_ids == frozenset({a.tool}) and policy.agent_id is None


CASES = ["unknown id", "own unpinned tool", "other client's tool", "own pinned agent as a tool",
         "other client's agent", "own unpinned agent"]  # fmt: skip


@pytest.mark.parametrize("case", CASES)
def test_a_node_naming_what_the_run_does_not_pin_is_refused_like_an_unknown_id(
    case: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pair
    target = {"unknown id": uuid7(), "own unpinned tool": a.unpinned_tool,
              "other client's tool": b.tool, "own pinned agent as a tool": a.agent,
              "other client's agent": b.agent, "own unpinned agent": a.loose_agent}[case]  # fmt: skip
    node = (
        _agent("n", target) if "agent" in case and "as a tool" not in case else _tool("n", target)
    )
    pins = [(a.tool, "tool"), (a.agent, "agent")]
    run = _pipe(owner_db, a, [node], pins=pins, params={"doc": {}})
    seen = _spy_tools(monkeypatch)
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        PipelineRunner(_ctx(s, settings, a, run)).run()
    assert refused.value.error_class == state.VALIDATION
    assert "node n" in str(refused.value)
    assert seen == [] and _children(owner_db, run) == []
    assert client_state_of(owner_db, b.tenant) == before


@pytest.mark.parametrize("case", ["own pinned agent as a tool", "own pinned tool as an agent"])
def test_a_node_of_another_kind_is_refused_even_when_validation_misses_it(
    case: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor's own kind check (B-2162), not only the validator's: with the validation
    report emptied, a tool node naming a pinned agent version (or an agent node naming a pinned
    tool version) is still refused like an unknown id."""
    a, b = pair
    node = _tool("n", a.agent) if case == "own pinned agent as a tool" else _agent("n", a.tool)
    run = _pipe(owner_db, a, [node], pins=[(a.tool, "tool"), (a.agent, "agent")],
                params={"doc": {}})  # fmt: skip
    prepare = ArtifactService.prepare

    def unchecked(self: ArtifactService, *args: Any, **kwargs: Any) -> Any:
        prepared = prepare(self, *args, **kwargs)
        prepared.report.errors.clear()
        return prepared

    monkeypatch.setattr(ArtifactService, "prepare", unchecked)
    seen = _spy_tools(monkeypatch)
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        PipelineRunner(_ctx(s, settings, a, run)).run()
    assert (refused.value.error_class, str(refused.value)) == (
        state.VALIDATION,
        "node n: its version is not available",
    )
    assert seen == [] and _children(owner_db, run) == []
    assert client_state_of(owner_db, b.tenant) == before


# ------------------------------------------------------------------ agent nodes (child runs)
def test_an_agent_node_starts_a_child_in_the_stored_runs_client_root_and_scope_only(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """The child's scope comes from the stored run, never from the context's mapping (runtime
    isolation review 7 item 1): a context whose mapping names another workspace, project, root,
    test flag, depth or deadline still starts the child where the stored run is (the depth
    checked against MAX_DEPTH is the stored run's too)."""
    a, b = pair
    run = _pipe(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")],
                params={"doc": {"q": 1}})  # fmt: skip
    with owner_db.session() as o:
        far_ws = add_workspace(o, a.tenant)
        far_proj = add_project(o, a.tenant, far_ws)
        o.commit()
    forged = {"workspace_id": far_ws, "project_id": far_proj, "root_run_id": uuid7(),
              "is_test": True, "depth": MAX_DEPTH,
              "deadline_at": utcnow() + timedelta(days=9)}  # fmt: skip
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.Waiting) as waiting:
        PipelineRunner(_ctx(s, settings, a, run, **forged)).run()
    assert (waiting.value.reason, waiting.value.transitioned) == ("child_runs", True)
    [child] = _children(owner_db, run)
    [parent] = _rows(owner_db, "SELECT * FROM shifde.runs WHERE id = :r", r=run)
    same = ("tenant_id", "workspace_id", "project_id", "root_run_id", "initiated_by_principal_id",
            "principal_type", "trace_id", "correlation_id", "deadline_at", "is_test")  # fmt: skip
    assert {k: child[k] for k in same} == {k: parent[k] for k in same}
    assert (child["workspace_id"], child["project_id"]) == (a.workspace, a.project)
    assert child["depth"] == parent["depth"] + 1
    assert (child["agent_id"], child["agent_version_id"], child["prompt_version_id"]) == (
        a.agent_art, a.agent, a.prompt)  # fmt: skip
    assert (parent["status"], parent["waiting_reason"]) == (state.WAITING, "child_runs")
    assert client_state_of(owner_db, b.tenant) == before


@pytest.mark.parametrize("case", ["prompt not pinned", "other client's prompt"])
def test_an_agent_whose_prompt_is_not_in_the_runs_pin_set_starts_no_child(
    case: str, pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    if case == "prompt not pinned":
        agent = a.loose_agent
    else:
        with owner_db.session() as o:
            _, agent = add_published(o, a.tenant, "agents", _key("foreign"),
                                     agent_spec(str(b.prompt), str(uuid7())))  # fmt: skip
            build_repo.insert_pins(o, a.tenant, agent, [(a.prompt, "prompt")])
            o.commit()
    run = _pipe(owner_db, a, [_agent("n", agent)], pins=[(agent, "agent")], params={"doc": {}})
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as failed:
        PipelineRunner(_ctx(s, settings, a, run)).run()
    assert failed.value.error_class == state.VALIDATION
    assert "the agent's prompt is not available" in str(failed.value)
    assert _children(owner_db, run) == []
    assert client_state_of(owner_db, b.tenant) == before


def test_a_step_is_read_only_through_the_runs_own_child(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """A step naming another finished run of the same client is not this run's child: the node
    fails as with a missing child and that run's output is not read. A step naming B's run cannot
    exist (composite key)."""
    a, b = pair
    run = _pipe(owner_db, a, [_agent("n", a.agent)], pins=[(a.agent, "agent")], params={"doc": {}})
    with owner_db.session() as o:
        other = add_run(o, a.tenant, agent_id=a.agent_art, agent_version_id=a.agent)
        ref = repo.insert_artifact(o, a.tenant, other, "output", {"output": "not yours"})
        assert repo.transition(o, a.tenant, other, state.PENDING, state.RUNNING)
        assert repo.transition(o, a.tenant, other, state.RUNNING, state.COMPLETED, output_ref=ref,
                               finished_at=utcnow())  # fmt: skip
        b_run = add_run(o, b.tenant)
        insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=run, seq=1,
               kind="pipeline_node", name="agent:n", node_id="n", status="running",
               child_run_id=other)  # fmt: skip
        o.commit()
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as failed:
        PipelineRunner(_ctx(s, settings, a, run)).run()
    assert (failed.value.error_class, str(failed.value)) == (
        state.INTERNAL,
        "node n failed: its child run is missing",
    )
    with owner_db.session() as o, pytest.raises(IntegrityError, match="fk_run_steps__child_run"):
        insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=run, seq=2,
               kind="pipeline_node", name="agent:n", node_id="n", status="running",
               child_run_id=b_run)  # fmt: skip


# ------------------------------------------------------------------ the run itself
@pytest.mark.parametrize("which", ["other client's run", "unknown run", "another version"])
def test_a_context_naming_a_run_the_client_does_not_hold_gets_the_unknown_run_answer(
    which: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pair
    a_run = _pipe(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")], params={"doc": {}})
    b_run = _pipe(owner_db, b, [_tool("t", b.tool)], pins=[(b.tool, "tool")], params={"doc": {}})
    other = _pipe(owner_db, a, [_tool("t", a.tool)], pins=[(a.tool, "tool")], params={"doc": {}})
    with owner_db.session() as o:
        row = {"other client's run": run_row(o, b.tenant, b_run),
               "unknown run": {**run_row(o, a.tenant, a_run), "id": uuid7()},
               "another version": {**run_row(o, a.tenant, a_run),
                                   "pipeline_version_id": run_row(o, a.tenant, other)[
                                       "pipeline_version_id"]}}[which]  # fmt: skip
    seen = _spy_tools(monkeypatch)
    before = client_state_of(owner_db, b.tenant)
    with opened(a.tenant) as s, pytest.raises(state.RunFailed) as refused:
        PipelineRunner(
            run_context(s, settings, {**row, "tenant_id": a.tenant}, token=a.token)
        ).run()
    assert (refused.value.error_class, str(refused.value)) == (
        state.VALIDATION,
        "the pinned pipeline version is not available",
    )
    assert seen == []
    assert client_state_of(owner_db, b.tenant) == before


# ------------------------------------------------------------------ connections
def test_a_connection_node_reaches_only_a_named_connection_at_the_runs_scope(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """`_connection_gate`: B's connection, A's connection in another workspace (named by nothing
    the run pins) and an unknown id all get the unknown-connection denial; A's named client-level
    connection passes the gate."""
    a, b = pair
    run = _pipe(owner_db, a, [_api("r", a.conn)])
    with opened(a.tenant) as s:
        runner = PipelineRunner(_ctx(s, settings, a, run))
        runner._load()
        runner._revalidate()
        runner._connection_gate("r", a.conn, write=False)  # control
        for cid in (b.conn, a.far_conn, uuid7()):
            with pytest.raises(state.GateDenied) as denied:
                runner._connection_gate("r", cid, write=False)
            assert (denied.value.error_class, denied.value.message) == (
                state.AUTHZ_DENIED,
                "the connection is not available",
            )


def test_a_run_session_re_reads_its_connection_at_the_runs_scope_only(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """`open_for_use(at=)`: the row the session opens must lie at the client level or the run's
    own workspace and project (the row used is the row checked); B's id is unknown too."""
    a, b = pair
    run = _pipe(owner_db, a, [_api("r", a.conn)])
    scope = (a.workspace, a.project)
    with opened(a.tenant) as s:
        conns = _ctx(s, settings, a, run).artifacts.conns
        for cid in (a.far_conn, b.conn):
            with pytest.raises(NotFound), conns.open_for_use(cid, "t", at=scope):
                pass


# ------------------------------------------------------------------ C1: a write caught mid-write
def test_a_pipeline_write_caught_mid_write_ends_with_an_open_decision_not_cancelled(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """C1 (B-2122, B-2145, B-2194): a write node's step is written ahead with its idempotency key;
    a run cancelled while it is `running` settles it `indeterminate` with an
    `indeterminate_side_effect` decision, and a recovering worker parks it the same way."""
    a, b = pair
    run = _pipe(owner_db, a, [_api("w", a.conn, "POST")])
    with owner_db.session() as o:
        insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=run, seq=1, attempt=1,
               kind="pipeline_node", name="api_request:w", node_id="w", status="running",
               idempotency_key="k-mid-write")  # fmt: skip
        o.commit()
    with opened(a.tenant) as s, pytest.raises(state.Waiting) as waiting:
        PipelineRunner(_ctx(s, settings, a, run)).run()
    assert waiting.value.reason == "indeterminate_side_effect"
    [step] = _rows(owner_db, "SELECT status FROM shifde.run_steps WHERE run_id = :r", r=run)
    [appr] = _rows(owner_db, "SELECT tenant_id, reason, call_ref, epoch, status FROM "
                   "shifde.approvals WHERE run_id = :r", r=run)  # fmt: skip
    assert step["status"] == "indeterminate"
    assert appr == {"tenant_id": a.tenant, "reason": "indeterminate_side_effect",
                    "call_ref": "node:w", "epoch": 0, "status": "pending"}  # fmt: skip
    # the settle path: a second run cancelled with its write in flight
    other = _pipe(owner_db, a, [_api("w", a.conn, "POST")])
    with owner_db.session() as o:
        insert(o, "run_steps", id=uuid7(), tenant_id=a.tenant, run_id=other, seq=1, attempt=1,
               kind="pipeline_node", name="api_request:w", node_id="w", status="running",
               idempotency_key="k-mid-write-2")  # fmt: skip
        assert repo.transition(o, a.tenant, other, state.RUNNING, state.CANCELLED,
                               finished_at=utcnow(), error_class="cancelled")  # fmt: skip
        assert repo.settle_ended_run(o, b.tenant, other) == 0  # B's id for A's run: nothing
        assert repo.settle_ended_run(o, a.tenant, other) == 1
        o.commit()
    [settled] = _rows(owner_db, "SELECT status FROM shifde.run_steps WHERE run_id = :r", r=other)
    assert settled["status"] == "indeterminate"


# ------------------------------------------------------------------ keys: lineage and artifacts
def test_lineage_and_artifacts_can_only_name_a_run_of_the_writers_client(
    pair: tuple[Client, Client], owner_db: Database
) -> None:
    a, b = pair
    b_run = _pipe(owner_db, b, [_tool("t", b.tool)], pins=[(b.tool, "tool")])
    with owner_db.session() as o:
        _, onto = add_published(o, a.tenant, "ontologies", _key("onto"), {"object_types": []})
        o.commit()
    with owner_db.session() as o:
        onto_art = o.execute(
            text("SELECT artifact_id FROM shifde.artifact_versions WHERE id = :v"), {"v": onto}
        ).scalar_one()
        with pytest.raises(IntegrityError, match="fk_ontology_lineage__run"):
            insert(o, "ontology_query_lineage", id=uuid7(), tenant_id=a.tenant,
                   principal_id=a.user, principal_type="user", ontology_id=onto_art,
                   ontology_version_id=onto, run_id=b_run, channel="pipeline",
                   object_types=[], connection_ids=[], compiled_query_hashes=[],
                   duration_ms=0, outcome="success")  # fmt: skip
    with owner_db.session() as o, pytest.raises(IntegrityError):
        repo.insert_artifact(o, a.tenant, b_run, "intermediate", {"x": 1})
        o.flush()


# ------------------------------------------------------------------ ontology connections in a run
def test_an_ontology_read_in_a_run_reaches_only_connections_at_the_runs_scope(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """`OntologyService._conn_row` on the unchecked (run) path: a mapping's connection in another
    workspace of the client is the unknown-connection answer (B-2193); a client-level one is
    read. The scope is the anchored run's."""
    a, b = pair
    run = _pipe(owner_db, a, [_api("r", a.conn)])
    with opened(a.tenant) as s:
        svc = OntologyService(_ctx(s, settings, a, run).artifacts)
        svc._run_scope = (a.workspace, a.project)
        assert svc._conn_row(a.conn)["id"] == a.conn
        for cid in (a.far_conn, b.conn):
            with pytest.raises(NotFound):
                svc._conn_row(cid)


# ------------------------------------------------------------------ moved from SETH
def test_f8_sanitize_json_and_a_delivered_write_is_never_failed_for_its_size() -> None:
    """SETH `test_p6_review1.py`, assertions unchanged."""
    dirty = {"a\x00k": ["x\x00y", float("nan"), float("inf"), "\ud800z", 1, {"n": -float("inf")}]}
    clean = sanitize_json(dirty)
    assert clean == {"ak": ["xy", None, None, "?z", 1, {"n": None}]}
    runner = PipelineRunner.__new__(PipelineRunner)
    big = {"response": {"body": "x" * 3_000_000}}
    fitted = runner._fit(big)
    assert fitted["truncated"] is True and fitted["result"]["delivered"] is True
    assert runner._fit({"ok": 1}) == {"ok": 1}


def test_every_caller_of_a_tool_call_goes_through_the_guarded_runtime() -> None:
    """SETH `test_r1_round7.py`, assertions unchanged: every caller of the runtime's tool call
    goes through `steps.run_tool`, which owns the guard: agents, graph nodes, pipeline `tool`
    nodes and ontology actions."""
    from shifde.runtime import action, agent, graph
    from shifde.runtime import pipeline as pipeline_mod

    for module in (action, agent, graph, pipeline_mod):
        assert "steps.run_tool(" in inspect.getsource(module), module.__name__
