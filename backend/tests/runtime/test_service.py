"""Runtime part 11 (RT-11): `service.py`, the run and approval services behind the API.

Behaviour: a run starts only in the caller's own client, in an active workspace and project
(findings Item 12, REQ F-02), with an idempotent replay; a draft pipeline test run records the
draft's resolved pin set and its env answers for those versions only (B-2195); the API cancel of a
pending run whose approved write may have been delivered ends `cancelled` with an open
unknown-outcome decision (C1, the direct counterpart of SETH `test_r1_critic.py::
test_api_cancel_of_a_pending_run_after_lease_loss`); an approval decision resumes the run.

Isolation, as `shifde_app` with A's context and as the schema owner (where only the client id the
service passes confines it): A's caller cannot start a run on B's target or scope, cannot test-run
B's draft, cannot cancel, read, list or read the steps, events, result, nodes or artifacts of B's
runs, and cannot read, list or decide B's approvals. B's ids answer exactly like unknown ids, list
results never include B's rows, and none of B's rows changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.build import repository as build_repo
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import AppError, NotFound, ScopeArchived
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime import schemas as rs
from shifde.runtime.action import start_action_run
from shifde.runtime.governance import PolicyBudgetHooks
from shifde.runtime.service import ApprovalService, RunService
from shifde.tenancy import service as tenancy
from shifde.tenancy.schemas import CaptureContentPut
from tests._support.runs import add_run
from tests._support.tenants import add_project, add_tenants, add_workspace
from tests.build.env_support import add_published
from tests.build.routes import agent_spec, prompt_spec
from tests.build.service_support import REGISTRY, add_person, service_for
from tests.runtime.conftest import app_session, insert
from tests.runtime.execution import _write_in_flight, client_state
from tests.runtime.test_execution import MODES, opened

__all__ = ["MODES", "opened"]

UNKNOWN = "indeterminate_side_effect"
NONE_TOOL = {"executor": "builtin.function", "binding": {"function_key": "x"}, "timeout_s": 5,
             "side_effect_class": "none", "input_schema": {"type": "object"}}  # fmt: skip
PARAMS = {"type": "object", "properties": {"doc": {"type": "object"}},
          "additionalProperties": False}  # fmt: skip


@dataclass(frozen=True)
class Client:
    tenant: UUID
    admin: UUID  # tenant_administrator: starts, reads, cancels and decides
    ws: str  # slugs of an active workspace and project
    proj: str
    archived_ws: tuple[str, str]  # (workspace slug, project slug) of an archived workspace
    archived_proj: str  # an archived project in `ws`
    agent: UUID
    agent_version: UUID
    tool: UUID  # named by the pipeline draft
    unpinned_tool: UUID
    pipeline: UUID  # a pipeline with a draft (no published version)
    done: UUID  # a completed run with steps, events and an output artifact
    output: UUID  # the output artifact of `done`
    pending: UUID  # a pending run whose approved write may have been delivered (C1)
    waiting: UUID  # a run waiting for `approval`
    approval: UUID
    pipe_run: UUID  # a completed pipeline run of `pipeline`


def _key(kind: str) -> str:
    return f"{kind}-{uuid7().hex}"


def _pipeline_spec(tool: UUID) -> dict[str, Any]:  # Any: spec JSON
    return {
        "schema_version": "1",
        "params": PARAMS,
        "policies": {"max_parallel": 2, "deadline_seconds": 600, "on_failure": "fail_fast"},
        "nodes": [
            {
                "id": "t",
                "type": "tool",
                "config": {"tool_version_id": str(tool)},
                "inputs": {"args": "$params.doc"},
            }
        ],  # fmt: skip
        "edges": [],
    }


def _client(o: Session, t: UUID) -> Client:
    admin = add_person(o, t, "admin", "tenant_administrator")
    # content capture on, so the client's own output is readable (Item 13 is the default's gap)
    o.execute(text("UPDATE shifde.tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
              {"s": '{"capture_content": "full"}', "t": t})  # fmt: skip
    ws = add_workspace(o, t)
    proj = add_project(o, t, ws)
    far_ws = add_workspace(o, t)
    far_proj = add_project(o, t, far_ws)
    old_proj = add_project(o, t, ws)
    o.execute(text("UPDATE shifde.workspaces SET status = 'archived' WHERE id = :i"), {"i": far_ws})
    o.execute(text("UPDATE shifde.projects SET status = 'archived' WHERE id = :i"), {"i": old_proj})
    _, prompt = add_published(o, t, "prompts", _key("prompt"), prompt_spec())
    agent, agent_version = add_published(
        o, t, "agents", _key("agent"), agent_spec(str(prompt), str(uuid7()))
    )
    build_repo.insert_pins(o, t, agent_version, [(prompt, "prompt")])
    _, tool = add_published(o, t, "tools", _key("tool"), NONE_TOOL)
    _, unpinned = add_published(o, t, "tools", _key("unpinned"), NONE_TOOL)
    pipeline, draft = uuid7(), uuid7()
    build_repo.insert_artifact(o, {"id": pipeline, "tenant_id": t, "kind": "pipelines",
                                   "workspace_id": None, "project_id": None, "key": _key("pipe"),
                                   "name": "pipe", "description": None,
                                   "owner_principal_id": admin, "visibility": "scope",
                                   "created_by": None})  # fmt: skip
    build_repo.insert_version(o, {"id": draft, "tenant_id": t, "artifact_id": pipeline,
                                  "kind": "pipelines", "version_no": 1,
                                  "spec": _pipeline_spec(tool), "created_by": None,
                                  "based_on_version_id": None})  # fmt: skip
    scope = {"workspace_id": ws, "project_id": proj}
    target = {"agent_id": agent, "agent_version_id": agent_version}
    done = add_run(o, t, status="running", started_at=utcnow(), initiated_by_principal_id=admin,
                   **scope, **target)  # fmt: skip
    output = repo.insert_artifact(o, t, done, "output", {"output": f"answer of {t}"})
    insert(o, "run_events", id=uuid7(), tenant_id=t, run_id=done, seq=1, type="created")
    insert(o, "run_steps", id=uuid7(), tenant_id=t, run_id=done, seq=1, kind="model_call",
           name="m", status="succeeded")  # fmt: skip
    o.execute(text("UPDATE shifde.runs SET output_ref = :a, event_seq = 1, status = 'completed', state_version = state_version + 1, "
                   "finished_at = now() WHERE id = :r"), {"a": output, "r": done})  # fmt: skip
    pending = add_run(o, t, **scope, **target)
    _write_in_flight(o, t, pending, tool)
    waiting = add_run(o, t, status="waiting_for_approval", waiting_reason="approval", **scope,
                      **target)  # fmt: skip
    approval = uuid7()
    insert(o, "approvals", id=approval, tenant_id=t, run_id=waiting, workspace_id=ws,
           project_id=proj, call_ref="c9", reason="tool_side_effect", requested_by=uuid7(),
           expires_at=utcnow() + timedelta(hours=1))  # fmt: skip
    pipe_run = add_run(o, t, kind="pipeline", pipeline_id=pipeline, pipeline_version_id=draft,
                       is_test=True, status="completed", finished_at=utcnow(),
                       **scope)  # fmt: skip
    return Client(t, admin, f"w-{ws.hex}", f"p-{proj.hex}", (f"w-{far_ws.hex}",
                  f"p-{far_proj.hex}"), f"p-{old_proj.hex}", agent, agent_version, tool,
                  unpinned, pipeline, done, output, pending, waiting, approval,
                  pipe_run)  # fmt: skip


@pytest.fixture
def pair(owner_db: Database) -> tuple[Client, Client]:
    ta, tb = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, ta, tb)
        a, b = _client(o, ta), _client(o, tb)
        o.commit()
    return a, b


def _services(s: Session, c: Client, settings: Settings) -> tuple[RunService, ApprovalService]:
    """`c.admin`'s services in `c`'s client on `s` (the app role keeps the context the session
    was opened with; the schema owner gets none, so only the service's client id confines)."""
    app = s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    sc = service_for(s, c.tenant, c.admin, settings, set_context=app).sc
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    return RunService(sc, runtime, PolicyBudgetHooks()), ApprovalService(sc, runtime)


def _state(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        return client_state(o, tenant)


def _agent_body(c: Client, **over: Any) -> rs.RunCreate:
    values: dict[str, Any] = {"kind": "agent", "agent_id": c.agent, "version_id": c.agent_version,
                              "workspace": c.ws, "project": c.proj, "input": "hello"}  # fmt: skip
    return rs.RunCreate(**{**values, **over})


def _draft_body(c: Client, **over: Any) -> rs.RunCreate:
    values: dict[str, Any] = {"kind": "pipeline", "pipeline_id": c.pipeline, "version_id": "draft",
                              "workspace": c.ws, "project": c.proj, "params": {"doc": {}}}  # fmt: skip
    return rs.RunCreate(**{**values, **over})


def _runs_of(owner_db: Database, tenant: UUID) -> set[UUID]:
    with owner_db.session() as o:
        rows = o.execute(text("SELECT id FROM shifde.runs WHERE tenant_id = :t"), {"t": tenant})
        return {r[0] for r in rows}


# ------------------------------------------------------------------ start
def test_a_run_starts_in_the_callers_client_and_scope_and_replays_idempotently(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    before = _state(owner_db, b.tenant)
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        out, created = runs.create(_agent_body(a), "key-1")
        again, created_again = runs.create(_agent_body(a), "key-1")
    assert created and not created_again and again.id == out.id
    assert (out.tenant_id, out.workspace.slug, out.project.slug) == (a.tenant, a.ws, a.proj)
    with owner_db.session() as o:
        row = repo.get_run(o, a.tenant, out.id)
        jobs = o.execute(text("SELECT tenant_id FROM shifde.jobs WHERE run_id = :r"),
                         {"r": out.id}).all()  # fmt: skip
    assert row is not None and row["root_run_id"] == out.id and row["agent_id"] == a.agent
    assert [j[0] for j in jobs] == [a.tenant]
    assert _state(owner_db, b.tenant) == before


@pytest.mark.parametrize("where", ["archived workspace", "archived project"])
def test_no_run_starts_in_an_archived_workspace_or_project(
    where: str, pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Findings Item 12 (REQ F-02), the runs half (the connections half is B-1509)."""
    a, _ = pair
    ws, proj = a.archived_ws if where == "archived workspace" else (a.ws, a.archived_proj)
    before = _runs_of(owner_db, a.tenant)
    for body in (_agent_body(a, workspace=ws, project=proj),
                 _draft_body(a, workspace=ws, project=proj)):  # fmt: skip
        with opened(a.tenant) as s:
            runs, _ = _services(s, a, settings)
            with pytest.raises(ScopeArchived, match=where.split()[1]):
                runs.create(body, None)
    assert _runs_of(owner_db, a.tenant) == before


def test_a_draft_test_run_records_the_drafts_pins_and_its_env_answers_only_those(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """B-2195: the draft's resolved pin set is recorded at test-run start; the run's env answers
    for the draft, its recorded pins and nothing else (an unpinned tool of the client, B's tool,
    an unknown id: None) and names connections from the snapshot."""
    a, b = pair
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        out, created = runs.create(_draft_body(a), None)
        assert created and out.is_test
        stored = repo.get_run(s, a.tenant, out.id)
        assert stored is not None
        payload = repo.get_artifact(s, a.tenant, stored["input_ref"])
        assert payload["pins"] == [str(a.tool)]
        assert payload["spec_snapshot"]["nodes"][0]["config"]["tool_version_id"] == str(a.tool)
        art = runs.artifacts.artifact("pipelines", a.pipeline)
        env = runs.artifacts.env(art, run_of=stored)
        assert env.version(a.tool) is not None
        assert env.version(stored["pipeline_version_id"]) is not None
        for other in (a.unpinned_tool, b.tool, uuid7()):
            assert env.version(other) is None


def test_bs_tool_version_planted_in_a_runs_recorded_pins_is_not_in_its_env(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Runtime isolation review 10 finding 3: the recorded `pins` are trusted, so B's tool version
    id written there (as the schema owner) must still be dropped by the client-keyed closure."""
    a, b = pair
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        out, _ = runs.create(_draft_body(a), None)
        stored = repo.get_run(s, a.tenant, out.id)
        assert stored is not None
        s.commit()
    with owner_db.session() as o:  # the input is append-only: a new input document
        doc = dict(repo.get_artifact(o, a.tenant, stored["input_ref"]))
        doc["pins"] = [str(a.tool), str(b.tool), str(b.agent_version)]
        planted = repo.insert_artifact(o, a.tenant, out.id, "input", doc)
        o.execute(text("UPDATE shifde.runs SET input_ref = :i WHERE id = :r AND tenant_id = :t"),
                  {"i": planted, "r": out.id, "t": a.tenant})  # fmt: skip
        o.commit()
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        stored = repo.get_run(s, a.tenant, out.id)
        assert stored is not None and stored["input_ref"] == planted
        env = runs.artifacts.env(runs.artifacts.artifact("pipelines", a.pipeline), run_of=stored)
        assert env.version(a.tool) is not None  # not vacuous: the client's own pin answers
        assert env.version(b.tool) is None and env.version(b.agent_version) is None


# ------------------------------------------------------------------ cancel and decide (own)
def test_cancel_of_a_pending_run_with_a_maybe_delivered_write_keeps_the_decision_open(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """C1 (SETH `service.py:690-701`): the API finalizes the pending run in-process through
    `settle_terminal`, so it ends `cancelled` with `indeterminate_side_effect`, an open
    unknown-outcome decision and the audit event; B is untouched."""
    a, b = pair
    before = _state(owner_db, b.tenant)
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        done = runs.cancel(a.pending)
    assert (done.status, done.error_class, done.outcome_unknown) == ("cancelled", UNKNOWN, True)
    with owner_db.session() as o:
        approvals = o.execute(
            text("SELECT tenant_id FROM shifde.approvals WHERE run_id = :r AND status = 'pending' "
                 "AND reason = 'indeterminate_side_effect'"), {"r": a.pending}).all()  # fmt: skip
        audits = o.execute(
            text("SELECT tenant_id FROM shifde.audit_events WHERE target_id = :r "
                 "AND action = 'run.indeterminate_side_effect'"), {"r": str(a.pending)}).all()  # fmt: skip
    assert [r[0] for r in approvals] == [a.tenant] and [r[0] for r in audits] == [a.tenant]
    assert _state(owner_db, b.tenant) == before


def test_approving_resumes_the_run_in_its_own_client(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    before = _state(owner_db, b.tenant)
    with opened(a.tenant) as s:
        _, approvals = _services(s, a, settings)
        out = approvals.decide(a.approval, rs.DecideIn(decision="approve"))
    assert out.status == "approved" and out.run_id == a.waiting
    with owner_db.session() as o:
        assert o.execute(text("SELECT status FROM shifde.runs WHERE id = :r"),
                         {"r": a.waiting}).scalar_one() == "pending"  # fmt: skip
        jobs = o.execute(text("SELECT tenant_id FROM shifde.jobs WHERE run_id = :r"),
                         {"r": a.waiting}).all()  # fmt: skip
    assert [j[0] for j in jobs] == [a.tenant]
    assert _state(owner_db, b.tenant) == before


@pytest.mark.parametrize("status", ["suspended", "archived"])
def test_a_client_that_is_not_active_cannot_resume_a_run_by_deciding(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings, status: str
) -> None:
    """Lead isolation review of F-03 (B-2512): deciding an approval of a suspended or archived
    client is refused before anything is read or queued; the run stays waiting."""
    a, _ = pair
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.tenants SET status = :s WHERE id = :t"),
                  {"s": status, "t": a.tenant})  # fmt: skip
        o.commit()
    before = _state(owner_db, a.tenant)
    with opened(a.tenant) as s:
        app = s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        sc = service_for(s, a.tenant, a.admin, settings, status=status, set_context=app).sc
        approvals = ApprovalService(sc, IntegrationRuntime(settings=settings, registry=REGISTRY))
        with pytest.raises(ScopeArchived):
            approvals.decide(a.approval, rs.DecideIn(decision="approve"))
    assert _state(owner_db, a.tenant) == before


# ------------------------------------------------------------------ cross-client
Op = Callable[[RunService, ApprovalService, Client, Client], Any]
OPS: dict[str, Op] = {
    "get": lambda r, ap, a, b: r.get(b.done),
    "steps": lambda r, ap, a, b: r.steps(b.done),
    "events": lambda r, ap, a, b: r.events(b.done),
    "result": lambda r, ap, a, b: r.result(b.done),
    "nodes": lambda r, ap, a, b: r.nodes(b.pipe_run),
    "artifact of B's run": lambda r, ap, a, b: r.artifact(b.done, b.output),
    "B's artifact under A's run": lambda r, ap, a, b: r.artifact(a.done, b.output),
    "cancel": lambda r, ap, a, b: r.cancel(b.pending),
    "pipeline runs": lambda r, ap, a, b: r.pipeline_runs(b.pipeline, 10, None),
    "start B's agent": lambda r, ap, a, b: r.create(_agent_body(a, agent_id=b.agent), None),
    "start B's agent version": lambda r, ap, a, b: r.create(
        _agent_body(a, version_id=b.agent_version), None
    ),
    "test-run B's draft": lambda r, ap, a, b: r.create(
        _draft_body(a, pipeline_id=b.pipeline), None
    ),
    "start in B's scope": lambda r, ap, a, b: r.create(
        _agent_body(a, workspace=b.ws, project=b.proj), None
    ),
    "approval": lambda r, ap, a, b: ap.get(b.approval),
    "approve": lambda r, ap, a, b: ap.decide(b.approval, rs.DecideIn(decision="approve")),
    "deny": lambda r, ap, a, b: ap.decide(b.approval, rs.DecideIn(decision="reject")),
}
UNKNOWN_IDS: dict[str, Callable[[Client], Client]] = {
    "start in B's scope": lambda b: Client(
        **{**b.__dict__, "ws": f"w-{uuid7().hex}", "proj": f"p-{uuid7().hex}"}
    ),  # fmt: skip
}


def _unknown(b: Client, op: str) -> Client:
    """`b` with every id replaced by an id nobody has (the same shape, for the same call)."""
    if op in UNKNOWN_IDS:
        return UNKNOWN_IDS[op](b)
    fresh = {k: (uuid7() if isinstance(v, UUID) else v) for k, v in b.__dict__.items()}
    return Client(**fresh)


def _answer(call: Callable[[], Any]) -> tuple[type[AppError], str]:
    with pytest.raises(AppError) as err:
        call()
    return type(err.value), str(err.value)


@pytest.mark.parametrize("op", list(OPS))
def test_bs_ids_answer_like_unknown_ones_and_change_nothing(
    op: str, pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    before = _state(owner_db, b.tenant)
    with opened(a.tenant) as s:
        runs, approvals = _services(s, a, settings)
        foreign = _answer(lambda: OPS[op](runs, approvals, a, b))
    with opened(a.tenant) as s:
        runs, approvals = _services(s, a, settings)
        unknown = _answer(lambda: OPS[op](runs, approvals, a, _unknown(b, op)))
    assert foreign == unknown and foreign[0] is NotFound, (foreign, unknown)
    assert _state(owner_db, b.tenant) == before


@pytest.mark.parametrize("op", ["get", "steps", "events", "result", "nodes", "artifact of B's run",
                                "pipeline runs", "approval"])  # fmt: skip
def test_the_same_reads_answer_for_the_callers_own_ids(
    op: str, pair: tuple[Client, Client], opened: Any, settings: Settings
) -> None:
    """Not vacuous: the reads refused above for B's ids answer for A's own (A as `b`)."""
    a, _ = pair
    with opened(a.tenant) as s:
        runs, approvals = _services(s, a, settings)
        out = OPS[op](runs, approvals, a, a)
    if op == "result":
        assert out.output == f"answer of {a.tenant}"
    elif op == "artifact of B's run":
        assert out.content == {"output": f"answer of {a.tenant}"}
    assert out is not None


def test_lists_never_include_another_clients_rows(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    lists: dict[str, set[UUID]] = {}
    for c in (a, b):
        with opened(c.tenant) as s:
            runs, approvals = _services(s, c, settings)
            got = {r.id for r in runs.list_runs(status=None, kind=None, agent_id=None,
                                                 workspace=None, project=None, initiated_by=None,
                                                 since=None, until=None, limit=100,
                                                 cursor=None).items}  # fmt: skip
            got |= {r.id for r in runs.pipeline_runs(c.pipeline, 100, None).items}
            for group in ("pending", "decided", "expired"):
                got |= {x.id for x in approvals.list_approvals(group, None, 100, None).items}
            lists[str(c.tenant)] = got
    a_rows, b_rows = lists[str(a.tenant)], lists[str(b.tenant)]
    assert {a.done, a.pending, a.waiting, a.pipe_run, a.approval} <= a_rows
    assert {b.done, b.pending, b.waiting, b.pipe_run, b.approval} <= b_rows
    assert not a_rows & b_rows
    with opened(a.tenant) as s:  # B's run or tree as a filter: nothing of B's
        _, approvals = _services(s, a, settings)
        assert approvals.list_approvals("pending", b.waiting, 100, None).items == []
        assert approvals.list_approvals("pending", None, 100, None, b.waiting).items == []


# ------------------------------------------------------------------ RT-12: review 10 items 1, 2
def test_a_non_initiator_who_cannot_read_a_run_gets_the_unknown_run_answer_on_cancel(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Runtime isolation review 10 finding 1: no existence oracle inside the client."""
    a, _ = pair
    with owner_db.session() as o:
        stranger = add_person(o, a.tenant, "stranger", None)  # a member with no binding
        o.commit()
    before = _state(owner_db, a.tenant)
    answers = []
    for run in (a.pending, uuid7()):
        with opened(a.tenant) as s:
            app = s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
            sc = service_for(s, a.tenant, stranger, settings, set_context=app).sc
            runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
            svc = RunService(sc, runtime, PolicyBudgetHooks())
            answers.append(_answer(lambda svc=svc, run=run: svc.cancel(run)))
    assert answers[0] == answers[1] == (NotFound, "run not found"), answers
    assert _state(owner_db, a.tenant) == before


def _ontology(owner_db: Database, c: Client) -> UUID:
    """A published ontology of `c` whose action `act` runs `c.tool`."""
    with owner_db.session() as o:
        action = {"key": "act", "tool_version_id": str(c.tool), "param_mapping": {}}
        onto, version = add_published(
            o, c.tenant, "ontologies", _key("onto"), {"actions": [action]}
        )
        build_repo.insert_pins(o, c.tenant, version, [(c.tool, "action_tool")])
        o.execute(text("UPDATE shifde.artifacts SET latest_published_version_id = :v "
                       "WHERE id = :a"), {"v": version, "a": onto})  # fmt: skip
        o.commit()
    return onto


@pytest.mark.parametrize("where", ["archived workspace", "archived project"])
def test_no_action_run_starts_in_an_archived_workspace_or_project(
    where: str, pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    """Runtime isolation review 10 finding 2: `_active_scope` in `start_action_run`."""
    a, _ = pair
    onto = _ontology(owner_db, a)
    ws, proj = a.archived_ws if where == "archived workspace" else (a.ws, a.archived_proj)
    before = _runs_of(owner_db, a.tenant)
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        body = rs.ActionInvokeIn(object_pk="1", workspace=ws, project=proj)
        with pytest.raises(ScopeArchived, match=where.split()[1]):
            start_action_run(runs, onto, "act", body, None)
    # not vacuous: the active scope gets past the scope check (to the object lookup)
    with opened(a.tenant) as s:
        runs, _ = _services(s, a, settings)
        body = rs.ActionInvokeIn(object_pk="1", workspace=a.ws, project=a.proj)
        with pytest.raises(AppError) as err:
            start_action_run(runs, onto, "act", body, None)
        assert not isinstance(err.value, ScopeArchived), err.value
    assert _runs_of(owner_db, a.tenant) == before


def test_bs_ontology_or_version_answers_like_an_unknown_one_in_start_action_run(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, b = pair
    mine, theirs = _ontology(owner_db, a), _ontology(owner_db, b)
    before = _state(owner_db, b.tenant)
    body = rs.ActionInvokeIn(object_pk="1", workspace=a.ws, project=a.proj)
    cases = [((theirs, body), (uuid7(), body)),
             ((mine, body.model_copy(update={"version_id": b.agent_version})),
              (mine, body.model_copy(update={"version_id": uuid7()})))]  # fmt: skip
    for crossed, unknown in cases:
        got = []
        for onto, sent in (crossed, unknown):
            with opened(a.tenant) as s:
                runs, _ = _services(s, a, settings)
                got.append(
                    _answer(lambda r=runs, o=onto, x=sent: start_action_run(r, o, "act", x, None))
                )
        assert got[0] == got[1] and got[0][0] is NotFound, got
    assert _state(owner_db, b.tenant) == before


# ------------------------------------------------------------------ capture_content (J-M2)
def test_the_supported_setter_turns_a_clients_result_content_on_and_off(
    pair: tuple[Client, Client], app_db: Database, owner_db: Database, settings: Settings
) -> None:
    """J-M2 (B-2550): outside development a client's run result is redacted until its owner sets
    `capture_content` to `full` through `tenancy.service.set_capture_content`; `metadata` and
    `null` (the production default) redact it again."""
    a, _ = pair
    production = settings.model_copy(update={"env": "production"})
    for value, redacted in (("metadata", True), ("full", False), (None, True)):
        with owner_db.session() as o:
            version = o.execute(
                text("SELECT row_version FROM shifde.tenants WHERE id = :t"), {"t": a.tenant}
            ).scalar_one()
        with app_session(app_db, a.tenant) as s:
            sc = service_for(s, a.tenant, a.admin, production).sc
            body = CaptureContentPut(capture_content=value, row_version=version)
            assert tenancy.set_capture_content(sc, body).capture_content == value
        with app_session(app_db, a.tenant) as s:
            runs, _ = _services(s, a, production)
            out = runs.result(a.done)
        if redacted:
            assert out.redacted is True and out.output is None, value
        else:
            assert out.redacted is False and out.output == f"answer of {a.tenant}", value
