"""SP7-4 (design sections 3, 4 and 6): the eleven `seth.*` internal functions, called as
`runtime/steps.py` calls them: on a session in the run's client, under the run principal, with the
run's workspace, project and run.

Each read returns its shape: tables with column names and types only (no row value), solutions
with their versions' entrypoint and screens, releases with status, digest and diagnostics,
evaluation runs with each threshold's result, costs, budgets with what is left, the project's
board and the targets of the project and its workspace without any `credential_ref`. Results are
capped at 64 KiB (`truncated`). Every function succeeds on a session whose `commit` and
`rollback` raise. An id of client B, of project P2 or unknown answers `not_found` (so does an
evaluation run of theirs with P1's release) and a propose naming one stores nothing; a proposal's tables are checked against the latest discovery; a replay
of a propose step answers the same proposal; a run that serves no assist request is
`not_an_assist_run`. A scan finds no approve, reject, accept-draft, apply, cancel, reconcile or
mark-failed call in `assist/tools.py` or the declarations. Each test has a database of its own.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any  # Any: JSON arguments, results and row values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.assist import service as assist
from shifde.assist import tools
from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.core import db
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.runtime.internal_tools import InternalToolCall
from tests._support.assist_fixtures import sent
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, B
from tests._support.p5util import Hdr, user_session
from tests._support.runs import add_run
from tests.appgen.helpers import TABLES, databricks, wire
from tests.assist.helpers import (
    add_project,
    enable,
    installed,
    model,
    project_of,
    rows,
    user_of,
)
from tests.conftest import CLEAN_TABLES
from tests.delivery.releases import make_release
from tests.delivery.routes import address, register, target_body
from tests.evaluation.helpers import case
from tests.evaluation.helpers import release as evaluated_release
from tests.evaluation.helpers import request as request_evaluation
from tests.ledger.util import add_budget

for _table in ("budgets", "budget_reservations", "usage_events"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

P1_SCOPE = {"workspace": "main", "project": "sample"}
P2_SCOPE = {"workspace": "main", "project": "other"}


@dataclass(frozen=True)
class W:
    """Client A with every specialist installed in P1 (`main/sample`), P2 (`main/other`) and
    client B, each with a discovered connection, a sealed release and a target, and A's routed
    `build`, `evaluation` and `deployment` requests (their runs stay queued). P1's release is
    sealed through the routes and has a requested evaluation; P2's and B's are rows."""

    client: TestClient
    admin: Hdr
    model: str
    ws: UUID
    p1: UUID
    p2: UUID
    conn: dict[str, str]
    release: dict[str, str]
    target: dict[str, str]
    runs: dict[str, str]  # A's assist runs by specialist key, B's and P2's plain runs


def _target(h: Hdr, org_slug: str, project: str | None) -> str:
    body = target_body("main", address(), **({"project": project} if project else {}))
    return str(register(h.client, h, org_slug, body)["id"])


@pytest.fixture
def w(client: TestClient, world: World, owner_session: Session, owner_db: Database) -> W:
    admin = user_session(client, world.passwords, ADMIN)
    enable(admin)
    model_id = model(admin)
    installed(admin, model_id)
    add_project(admin, "other")
    wire(admin.client)
    b = user_session(client, world.passwords, ADMIN_B)
    conn = {
        "p1": databricks(admin.client, admin, "lake-p1", scope=P1_SCOPE),
        "p2": databricks(admin.client, admin, "lake-p2", scope=P2_SCOPE),
        "b": databricks(b.client, b, "lake-b", B),
    }
    ws, p1 = (UUID(x) for x in project_of(owner_session, "demo-a"))
    p2 = UUID(project_of(owner_session, "demo-a", "other")[1])
    tenant_a, tenant_b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    ws_b, proj_b = (UUID(x) for x in project_of(owner_session, "demo-b"))
    p1_release = evaluated_release(admin, owner_session, [case("c1", "hello", ("contains", "x"))])
    assert request_evaluation(p1_release).status_code == 201
    release = {
        "p1": p1_release.id,
        "p2": str(make_release(owner_db, tenant_a, ws, p2).id),
        "b": str(make_release(owner_db, tenant_b, ws_b, proj_b).id),
    }
    target = {
        "p1": _target(admin, "demo-a", "sample"),
        "ws": _target(admin, "demo-a", None),
        "p2": _target(admin, "demo-a", "other"),
        "b": _target(b, "demo-b", "sample"),
    }
    runs = {
        key: str(sent(admin.client, admin, {"text": text})["run_id"])
        for key, text in (
            ("build", "build an app for the sales team"),
            ("evaluation", "evaluate the accuracy"),
            ("deployment", "deploy the sales app"),
        )
    }
    owner_session.rollback()
    runs["b"] = str(add_run(owner_session, tenant_b, workspace_id=ws_b, project_id=proj_b))
    runs["p2"] = str(add_run(owner_session, tenant_a, workspace_id=ws, project_id=p2))
    add_budget(owner_session, tenant_a, amount="1000")
    owner_session.commit()
    return W(client, admin, model_id, ws, p1, p2, conn, release, target, runs)


@contextmanager
def _call(
    w: W, owner: Session, key: str, args: dict[str, Any], *, run: str | None = None,
    step_key: str = "step-1", email: str = ADMIN,
) -> Iterator[InternalToolCall]:  # fmt: skip
    """The call `runtime/steps.py` makes of `key`'s function in A's P1 run `run` (a new id when
    None), as `email`, on a session of A's client."""
    app: Any = w.client.app  # Any: the FastAPI app behind the test client
    database: Database = app.state.db
    tenant = tenant_id(owner, "demo-a")
    session = database.session()
    db.init_context(session, "assist-tools-test")
    db.set_tenant(session, tenant)
    try:
        scope = assist._requester_scope(
            session,
            tenant,
            "assist-tools-test",
            {"principal_id": user_of(owner, email), "principal_type": "user", "api_key_id": None},
        )
        yield InternalToolCall(
            session=session, scope=scope, runtime=app.state.integration, tenant_id=tenant,
            workspace_id=w.ws, project_id=w.p1, run_id=UUID(run) if run else uuid7(),
            step_key=step_key, args=args,
        )  # fmt: skip
    finally:
        session.rollback()
        session.close()


def _run(
    w: W, owner: Session, key: str, args: dict[str, Any], *, commit: bool = False, **kw: Any
) -> dict[str, Any]:
    with _call(w, owner, key, args, **kw) as call:
        out: dict[str, Any] = tools.REGISTRY[key](call)
        if commit:
            call.session.commit()
    json.dumps(out)  # a JSON document
    return out


def _proposals(owner: Session) -> list[tuple[Any, ...]]:
    return rows(owner, "SELECT run_id::text, step_key, kind, payload FROM shifde.assist_proposals")


def _plan(w: W, release: str = "p1", target: str = "p1") -> dict[str, Any]:
    return {"target_id": w.target[target], "release_id": w.release[release],
            "application_key": "sales-app", "display_name": "Sales app"}  # fmt: skip


def _app(w: W, connection: str = "p1", **over: Any) -> dict[str, Any]:
    return {"connection_id": w.conn[connection], "tables": TABLES, "brief": "A sales app",
            "model_id": w.model, **over}  # fmt: skip


# ------------------------------------------------------------------ registry and shapes
def test_the_registry_holds_every_declared_function() -> None:
    assert set(tools.REGISTRY) == set(INTERNAL_FUNCTIONS)


def test_each_read_returns_its_shape(w: W, owner_session: Session) -> None:
    catalog_out = _run(w, owner_session, "seth.catalog.read", {})
    assert catalog_out["truncated"] is False
    names = {c["name"] for c in catalog_out["connections"]}
    assert "lake-p1" in names and "lake-p2" not in names
    [lake] = [c for c in catalog_out["connections"] if c["name"] == "lake-p1"]
    assert {t["name"] for t in lake["tables"]} >= set(TABLES)
    for table in lake["tables"]:
        assert set(table) == {"name", "table_type", "columns"}
        assert table["columns"] and all(set(c) == {"name", "type"} for c in table["columns"])
    one = _run(w, owner_session, "seth.catalog.read", {"connection_id": w.conn["p1"]})
    assert [c["id"] for c in one["connections"]] == [w.conn["p1"]]

    solutions = _run(w, owner_session, "seth.solutions.read", {})["solutions"]
    [sol] = [s for s in solutions if s["versions"]]
    assert set(sol["versions"][0]) == {"id", "version_no", "entrypoint", "screens"}

    releases = _run(w, owner_session, "seth.releases.read", {})["releases"]
    assert [r["id"] for r in releases] == [w.release["p1"]]
    [rel] = _run(w, owner_session, "seth.releases.read", {"release_id": w.release["p1"]})[
        "releases"
    ]
    assert rel["status"] == "sealed" and len(rel["digest"]) == 64
    assert isinstance(rel["compatibility"], list)

    evaluations = _run(w, owner_session, "seth.evaluations.read",
                       {"release_id": w.release["p1"]})  # fmt: skip
    [run] = evaluations["runs"]
    assert run["status"] in ("queued", "running")
    thresholds = {t["threshold"]: t for t in run["thresholds"]}
    assert thresholds["min_accuracy"] == {"threshold": "min_accuracy", "limit": "0.9",
                                          "value": None, "passed": None}  # fmt: skip

    costs = _run(w, owner_session, "seth.costs.read", {"days": 7})
    assert set(costs) >= {"by_day", "by_model", "by_run_kind", "truncated"}
    one_run = _run(w, owner_session, "seth.costs.read", {"run_id": w.runs["deployment"]})
    assert one_run["run_id"] == w.runs["deployment"] and "totals" in one_run

    [budget] = _run(w, owner_session, "seth.budgets.read", {})["budgets"]
    assert budget["scope"]["type"] == "tenant" and budget["remaining"] == 1000

    board = _run(w, owner_session, "seth.board.read", {})["board"]
    assert [p["project"]["id"] for p in board["projects"]] == [str(w.p1)]

    targets = _run(w, owner_session, "seth.targets.read", {})
    assert {t["id"] for t in targets["targets"]} == {w.target["p1"], w.target["ws"]}
    assert "credential_ref" not in json.dumps(targets)


def test_results_are_capped_at_64_kib() -> None:
    small = tools.capped({"items": ["x"] * 10})
    assert small == {"items": ["x"] * 10, "truncated": False}
    big = tools.capped({"items": [{"name": "n" * 100, "columns": ["c"] * 5}] * 5000})
    assert big["truncated"] is True and 0 < len(big["items"]) < 5000
    assert len(json.dumps(big).encode()) <= tools.MAX_BYTES


def test_every_function_runs_without_commit_or_rollback(w: W, owner_session: Session) -> None:
    calls: dict[str, tuple[dict[str, Any], str | None]] = {
        "seth.catalog.read": ({}, None),
        "seth.solutions.read": ({}, None),
        "seth.releases.read": ({}, None),
        "seth.evaluations.read": ({"release_id": w.release["p1"]}, None),
        "seth.costs.read": ({}, None),
        "seth.costs.read#run": ({"run_id": w.runs["build"]}, None),
        "seth.budgets.read": ({}, None),
        "seth.board.read": ({}, None),
        "seth.targets.read": ({}, None),
        "seth.propose.app_request": (_app(w), w.runs["build"]),
        "seth.propose.evaluation_run": ({"release_id": w.release["p1"]}, w.runs["evaluation"]),
        "seth.propose.delivery_plan": (_plan(w), w.runs["deployment"]),
    }
    assert {key.split("#")[0] for key in calls} == set(tools.REGISTRY)

    def refused() -> None:
        raise AssertionError("a function committed or rolled back")

    for key, (args, run) in calls.items():
        name = key.split("#")[0]
        with _call(w, owner_session, name, args, run=run) as call:
            session: Any = call.session  # Any: the session with its methods replaced
            session.commit, session.rollback = refused, refused
            try:
                out = tools.REGISTRY[name](call)
            finally:
                del session.commit, session.rollback
        assert "error" not in out, (key, out)
        if key.startswith("seth.propose."):
            assert UUID(out["proposal_id"]) and out["truncated"] is False


def test_a_replayed_propose_step_returns_the_same_proposal(w: W, owner_session: Session) -> None:
    run = w.runs["deployment"]
    first = _run(w, owner_session, "seth.propose.delivery_plan", _plan(w), run=run, commit=True)
    again = _run(w, owner_session, "seth.propose.delivery_plan", _plan(w), run=run, commit=True)
    assert first == again and first["truncated"] is False
    [(run_id, step_key, kind, payload)] = _proposals(owner_session)
    assert (run_id, step_key, kind, payload) == (run, "step-1", "delivery_plan", _plan(w))
    [(request_id,)] = rows(owner_session, "SELECT id::text FROM shifde.assist_requests "
                                          "WHERE run_id = :r", r=run)  # fmt: skip
    [(action, details)] = rows(owner_session, "SELECT action, details FROM shifde.audit_events "
                                              "WHERE target_id = :p", p=first["proposal_id"])  # fmt: skip
    assert action == "assist.proposal.created"
    assert {k: details[k] for k in ("kind", "request_id", "run_id")} == {
        "kind": "delivery_plan", "request_id": request_id, "run_id": run,
    }  # fmt: skip
    assert "payload" not in details and w.target["p1"] not in json.dumps(details)
    other = _run(w, owner_session, "seth.propose.delivery_plan", _plan(w), run=run,
                 step_key="step-2", commit=True)  # fmt: skip
    assert other["proposal_id"] != first["proposal_id"] and len(_proposals(owner_session)) == 2


@pytest.mark.parametrize("where", ["b", "p2", "unknown"])
def test_ids_of_another_client_or_project_are_not_found(
    w: W, owner_session: Session, where: str
) -> None:
    def pick(ids: dict[str, str]) -> str:
        return str(uuid7()) if where == "unknown" else ids[where]

    not_found = {"error": "not_found", "truncated": False}
    reads = [
        ("seth.catalog.read", {"connection_id": pick(w.conn)}),
        ("seth.releases.read", {"release_id": pick(w.release)}),
        ("seth.evaluations.read", {"release_id": pick(w.release)}),
        ("seth.costs.read", {"run_id": pick(w.runs)}),
    ]
    for key, args in reads:
        assert _run(w, owner_session, key, args) == not_found, key
    [sol] = [s for s in _run(w, owner_session, "seth.solutions.read", {})["solutions"]]
    other_solution = rows(
        owner_session, "SELECT v.solution_id::text FROM shifde.releases r JOIN "
        "shifde.solution_versions v ON v.id = r.solution_version_id WHERE r.id = :r",
        r=pick(w.release) if where != "unknown" else w.release["p1"],
    )[0][0]  # fmt: skip
    if where != "unknown":
        assert other_solution != sol["id"]
        for key in ("seth.solutions.read", "seth.releases.read"):
            assert _run(w, owner_session, key, {"solution_id": other_solution}) == not_found
    proposes = [
        ("seth.propose.app_request", _app(w, connection="p1", connection_id=pick(w.conn)), "build"),
        ("seth.propose.evaluation_run", {"release_id": pick(w.release)}, "evaluation"),
        ("seth.propose.delivery_plan", {**_plan(w), "release_id": pick(w.release)}, "deployment"),
        ("seth.propose.delivery_plan", {**_plan(w), "target_id": pick(w.target)}, "deployment"),
    ]
    for key, args, specialist in proposes:
        out = _run(w, owner_session, key, args, run=w.runs[specialist], commit=True)
        assert out == not_found, (key, args)
    assert _proposals(owner_session) == []


@pytest.mark.parametrize("where", ["b", "p2", "unknown"])
def test_an_evaluation_run_of_another_client_or_project_is_not_found(
    w: W, owner_session: Session, where: str
) -> None:
    """P1's release with B's, P2's or an unknown `evaluation_run_id` (issue #351)."""

    def run_of(release: str) -> str:
        [(run_id,)] = rows(
            owner_session, "SELECT id::text FROM shifde.evaluation_runs WHERE release_id = :r",
            r=w.release[release],
        )  # fmt: skip
        return str(run_id)

    foreign = str(uuid7()) if where == "unknown" else run_of(where)
    args = {"release_id": w.release["p1"], "evaluation_run_id": foreign}
    out = _run(w, owner_session, "seth.evaluations.read", args)
    assert out == {"error": "not_found", "truncated": False}
    own = run_of("p1")
    mine = _run(w, owner_session, "seth.evaluations.read", {**args, "evaluation_run_id": own})
    assert mine["release_id"] == w.release["p1"]
    assert [r["id"] for r in mine["runs"]] == [own]


def test_an_id_that_is_not_a_uuid_is_not_found_or_an_invalid_payload(
    w: W, owner_session: Session
) -> None:
    not_found = {"error": "not_found", "truncated": False}
    reads = [
        ("seth.catalog.read", {"connection_id": "conn-1"}),
        ("seth.solutions.read", {"solution_id": "sol-1"}),
        ("seth.releases.read", {"release_id": "rel-1"}),
        ("seth.evaluations.read", {"release_id": "rel-1"}),
        ("seth.evaluations.read", {"release_id": w.release["p1"], "evaluation_run_id": "run-1"}),
        ("seth.costs.read", {"run_id": "run-1"}),
    ]
    for key, args in reads:
        assert _run(w, owner_session, key, args) == not_found, key
    run = w.runs["evaluation"]
    out = _run(w, owner_session, "seth.propose.evaluation_run", {"release_id": "rel-1"}, run=run,
               commit=True)  # fmt: skip
    assert out["error"] == "invalid_payload" and out["problems"], out
    out = _run(w, owner_session, "seth.propose.delivery_plan", {**_plan(w), "target_id": "t-1"},
               run=w.runs["deployment"], commit=True)  # fmt: skip
    assert out["error"] == "invalid_payload", out
    assert _proposals(owner_session) == []


def test_a_proposal_is_checked_against_the_latest_discovery_and_the_model(
    w: W, owner_session: Session
) -> None:
    run = w.runs["build"]
    out = _run(w, owner_session, "seth.propose.app_request",
               _app(w, tables=["main.default.customers", "main.nope.nothing"]), run=run)  # fmt: skip
    assert out == {"error": "unknown_table", "tables": ["main.nope.nothing"], "truncated": False}
    out = _run(w, owner_session, "seth.propose.app_request", _app(w, model_id=str(uuid7())),
               run=run)  # fmt: skip
    assert out == {"error": "not_found", "truncated": False}
    assert _proposals(owner_session) == []


def test_a_run_that_serves_no_assist_request_is_not_an_assist_run(
    w: W, owner_session: Session
) -> None:
    not_assist = {"error": "not_an_assist_run", "truncated": False}
    # a run of another specialist (its key proposes no plan), and a run of no request at all
    for run in (w.runs["evaluation"], str(uuid7())):
        assert _run(w, owner_session, "seth.propose.delivery_plan", _plan(w), run=run) == not_assist
    assert _proposals(owner_session) == []


# ------------------------------------------------------------------ specialists propose only
CALLS = re.compile(r"\b(approve|reject|accept_draft|apply|cancel|reconcile|mark_failed)\w*\(")
FORBIDDEN_NAME = re.compile(
    r"^_*(approve|reject|accept_draft|apply|cancel|reconcile|mark_failed)", re.IGNORECASE
)


def _called_names(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names}
    return names


def no_approve_findings(source: str, decls: dict[str, tuple[str, str]]) -> list[str]:
    """Each approve, reject, accept-draft, apply, cancel, reconcile or mark-failed call that
    `source` makes or names, and each declaration (key, description) naming one."""
    found = sorted(n for n in _called_names(source) if FORBIDDEN_NAME.match(n))
    for key, description in decls.values():
        for part in key.split("."):
            if FORBIDDEN_NAME.match(part):
                found.append(key)
        found += [m.group(0) for m in CALLS.finditer(description)]
    return found


def test_specialists_never_approve_apply_or_accept() -> None:
    decls = {k: (d.key, d.description) for k, d in INTERNAL_FUNCTIONS.items()}
    assert no_approve_findings(inspect.getsource(tools), decls) == []
    # the scan is not vacuous
    planted = "from shifde.delivery.plans import PlanService\nPlanService(s, r).approve(p, b)\n"
    assert no_approve_findings(planted, {}) == ["approve"]
    assert no_approve_findings("svc.accept_draft(r)", {}) == ["accept_draft"]
    assert no_approve_findings("", {"k": ("seth.plans.apply", "Apply a plan.")}) == [
        "seth.plans.apply"
    ]
    assert no_approve_findings("", {"k": ("seth.x.read", "calls mark_failed(run)")}) == [
        "mark_failed("
    ]
