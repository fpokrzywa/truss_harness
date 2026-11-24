"""The `seth.*` internal functions of the specialists (SP7 design sections 3, 4 and 6; Calls S7-C,
S7-D, S7-E, S7-I).

`REGISTRY` maps every key of `build.internal_functions.INTERNAL_FUNCTIONS` to its function; the
composition root (`worker_main.internal_tools`) hands it to the worker, and `runtime/steps.py`
calls a function inside a run, on the step's session (the run's client) and in the step's
transaction. A function therefore never commits, never rolls back and never opens a session, and
calls only owning-service methods that do neither (`evaluation.service.stored_runs`,
`delivery.plans.PlanService.readable` and `governance.service.project_costs` are their read-only
reads; deployment and plan facts come from `board.service`).

Scope comes only from the call (the run row): its workspace, project and run, under the run
principal (`call.scope`). Nothing about scope is taken from `args`, and no function reads the
run's prompt variables (`assist_request_id`, `context`: any caller of a direct run may set them).
Each function first requires its read permission on the run's project through the PDP; a denial
propagates and ends the tool call with the 403 failure. Every id in `args` is then resolved
through its owning service and must belong to the run's project (a target: the project or its
workspace with no project); an unknown id, another project's and another client's all answer
`{"error": "not_found"}`, also when the service answers 404 or 403 or the id is not a UUID
(the step's argument check does not enforce `format`). Results hold names, kinds,
column types and SETH records, never client row values, and no target's `credential_ref`; they
are capped at 64 KiB of JSON by trimming list items (`truncated: true`).

The three `seth.propose.*` functions write one `assist_proposals` row and its audit event and
nothing else, and approve, apply or accept nothing (design section 6). The proposal's request is
found from server-side state only: the request (same client) that the run's stored idempotency
key `assist:{id}` names or whose `run_id` is the run, in the run's project, sent by the run's
initiating principal, routed to the specialist whose installed agent the run runs, handing over or
routed to this run. Without one (a direct run of an `assist-*` agent) the answer is
`{"error": "not_an_assist_run"}`. The payload is checked against the owning schema and the run's
project under the run principal; a problem is the tool result (`{"error": <code>, ...}`) so the
agent can correct itself. A replay of the step returns the stored proposal's id.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any  # Any: JSON arguments and results
from uuid import UUID

from pydantic import ValidationError

from shifde.appgen.schemas import AppRequestCreate
from shifde.assist import catalog, repository
from shifde.assist.service import record_proposal
from shifde.authz.service import ResourceRef
from shifde.board.service import BoardService
from shifde.connections.service import ConnectionService
from shifde.core.errors import Forbidden, NotFound
from shifde.delivery.schemas import PlanCreate
from shifde.delivery.service import TargetService
from shifde.evaluation import schemas as evaluation_schemas
from shifde.evaluation.service import EvaluationService
from shifde.governance.service import GovernanceService
from shifde.models_admin.service import ModelService
from shifde.packaging.service import SolutionService, in_scope, release_record, scoped_release
from shifde.runtime.governance import TokenBudgetHooks
from shifde.runtime.internal_tools import InternalToolCall, InternalToolFn
from shifde.runtime.service import run_binding
from shifde.tenancy import service as tenancy

MAX_BYTES = 64 * 1024  # the cap of a result's JSON
PAGE = 200  # items per owning-service page
MAX_ITEMS = 200  # connections, solutions or targets read per call
MAX_VERSIONS = 50  # solution versions read per solution
MAX_RUNS = 50  # evaluation runs read per release
NOT_FOUND = "not_found"
NOT_AN_ASSIST_RUN = "not_an_assist_run"
RUN_KEY_PREFIX = "assist:"  # the idempotency key of a handover's run (`assist:{request id}`)
UNKNOWN = (NotFound, Forbidden)  # an owning service's answers for an id the caller cannot use


class MalformedId(ValueError):
    """An id argument that is not a UUID: `not_found` from a read, `invalid_payload` from a
    propose."""


def _error(code: str, **details: Any) -> dict[str, Any]:  # Any: JSON details
    return {"error": code, **details, "truncated": False}


def _largest_list(doc: Any) -> list[Any] | None:  # Any: a JSON document
    best: list[Any] | None = None
    stack = [doc]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            if value and (best is None or len(value) > len(best)):
                best = value
            stack.extend(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
    return best


def capped(doc: Mapping[str, Any]) -> dict[str, Any]:  # Any: a JSON document
    """`doc` as JSON with `truncated`, trimmed to at most `MAX_BYTES`: the last items of the
    largest list are dropped until it fits (`truncated: true`)."""
    out: dict[str, Any] = json.loads(json.dumps(dict(doc), default=str))
    truncated = bool(out.pop("truncated", False))
    out["truncated"] = truncated
    while len(json.dumps(out).encode()) > MAX_BYTES:
        items = _largest_list(out)
        if items is None:
            break
        del items[-max(1, len(items) // 8) :]
        out["truncated"] = True
    return out


def _project(call: InternalToolCall) -> ResourceRef:
    return ResourceRef(call.tenant_id, call.workspace_id, call.project_id)


def _require(call: InternalToolCall, action: str) -> None:
    """The run principal's `action` on the run's project; a denial ends the call (403)."""
    call.scope.pdp.require(action, _project(call))


def _slugs(call: InternalToolCall) -> tuple[str, str]:
    workspaces, projects = tenancy.scope_slug_map(
        call.scope, [call.workspace_id], [call.project_id]
    )
    return workspaces[call.workspace_id], projects[call.project_id]


def _parsed(value: Any) -> UUID:  # Any: a JSON argument
    try:
        return UUID(str(value))
    except ValueError:
        raise MalformedId from None


def _uuid(call: InternalToolCall, name: str) -> UUID | None:
    value = call.args.get(name)
    return _parsed(value) if value is not None else None


def _read(fn: Callable[[InternalToolCall], dict[str, Any]]) -> InternalToolFn:
    """`fn`, answering `not_found` for an id argument that is not a UUID."""

    def read(call: InternalToolCall) -> dict[str, Any]:
        try:
            return fn(call)
        except MalformedId:
            return _error(NOT_FOUND)

    return read


def _without_credential_ref(value: Any) -> Any:  # Any: a JSON document
    if isinstance(value, dict):
        return {k: _without_credential_ref(v) for k, v in value.items() if k != "credential_ref"}
    if isinstance(value, list):
        return [_without_credential_ref(v) for v in value]
    return value


def _release_in_project(call: InternalToolCall, release_id: UUID) -> bool:
    """Whether the run principal can read the release and it is in the run's project."""
    try:
        return bool(scoped_release(call.scope, release_id)["project_id"] == call.project_id)
    except UNKNOWN:
        return False


def _target_in_project(call: InternalToolCall, target_id: UUID) -> bool:
    """Whether the run principal can read the target and it is in the run's project or in its
    workspace with no project."""
    ws, proj = _slugs(call)
    try:
        target = TargetService(call.scope).get(target_id)
    except UNKNOWN:
        return False
    return target.workspace == ws and target.project in (None, proj)


# ====================================================================== reads
def _table_doc(table: Mapping[str, Any]) -> dict[str, Any]:  # Any: a discovered table
    """A discovered table's name, kind and column names and types; never a value."""
    return {
        "name": f"{table.get('catalog')}.{table.get('schema')}.{table.get('name')}".lower(),
        "table_type": table.get("table_type"),
        "columns": [
            {"name": c.get("name"), "type": c.get("type")}
            for c in table.get("columns") or []
            if isinstance(c, dict)
        ],
    }


def catalog_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.catalog.read`: the connections visible in the run's project (or one of them) with
    the tables of their latest `schema` discovery."""
    _require(call, "connection:read")
    conns = ConnectionService(call.scope, call.runtime)
    connection_id = _uuid(call, "connection_id")
    if connection_id is not None:
        placement = conns.placement(connection_id)
        here = {"workspace_id": call.workspace_id, "project_id": call.project_id}
        if placement is None or not in_scope(here, placement.workspace_id, placement.project_id):
            return _error(NOT_FOUND)
        try:
            found = [conns.get(connection_id)]
        except UNKNOWN:
            return _error(NOT_FOUND)
    else:
        ws, proj = _slugs(call)
        found, cursor = [], None
        while len(found) < MAX_ITEMS:
            page = conns.list(connection_type=None, status=None, workspace=ws, project=proj,
                              include_shared=True, limit=PAGE, cursor=cursor)  # fmt: skip
            found += page.items
            cursor = page.next_cursor
            if cursor is None:
                break
    out = []
    for conn in found[:MAX_ITEMS]:
        discovery = conns.discovery_of(conn.id)
        schema = discovery is not None and discovery.kind == "schema"
        out.append(
            {
                "id": str(conn.id),
                "name": conn.name,
                "connection_type": conn.connection_type,
                "adapter_key": conn.adapter_key,
                "access_mode": conn.access_mode,
                "status": conn.status,
                "discovered_at": discovery.discovered_at if schema and discovery else None,
                "tables": [_table_doc(t) for t in (discovery.payload.get("tables") or [])]
                if schema and discovery
                else [],
            }
        )
    return capped({"connections": out, "truncated": len(found) > MAX_ITEMS})


def _versions(
    solutions: SolutionService, ws: str, proj: str, solution_id: UUID
) -> list[Any]:  # Any: SolutionVersionOut
    found: list[Any] = []
    cursor = None
    while len(found) < MAX_VERSIONS:
        page = solutions.list_versions(ws, proj, solution_id, limit=MAX_VERSIONS, cursor=cursor)
        found += page.items
        cursor = page.next_cursor
        if cursor is None:
            break
    return found[:MAX_VERSIONS]


def _solutions(call: InternalToolCall, solution_id: UUID | None) -> list[Any] | None:
    """The run's project's solutions (one when given; None when it is not one of them)."""
    ws, proj = _slugs(call)
    solutions = SolutionService(call.scope, call.runtime)
    if solution_id is not None:
        try:
            return [solutions.get(ws, proj, solution_id)]
        except UNKNOWN:
            return None
    found: list[Any] = []
    cursor = None
    while len(found) < MAX_ITEMS:
        page = solutions.list(ws, proj, status=None, limit=PAGE, cursor=cursor)
        found += page.items
        cursor = page.next_cursor
        if cursor is None:
            break
    return found[:MAX_ITEMS]


def solutions_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.solutions.read`: the project's solutions (or one) with their versions' entrypoint
    and screens."""
    _require(call, "packaging:solution:read")
    found = _solutions(call, _uuid(call, "solution_id"))
    if found is None:
        return _error(NOT_FOUND)
    ws, proj = _slugs(call)
    solutions = SolutionService(call.scope, call.runtime)
    return capped(
        {
            "solutions": [
                {
                    "id": str(sol.id),
                    "name": sol.name,
                    "status": sol.status,
                    "target_kind": sol.target_kind,
                    "versions": [
                        {
                            "id": str(v.id),
                            "version_no": v.version_no,
                            "entrypoint": {
                                "kind": v.entrypoint.kind,
                                "version_id": v.entrypoint.version_id,
                            },
                            "screens": v.spec.get("screens") or [],
                        }
                        for v in _versions(solutions, ws, proj, sol.id)
                    ],
                }
                for sol in found
            ]
        }
    )


def _release_doc(release: Any) -> dict[str, Any]:  # Any: ReleaseOut
    return {
        "id": str(release.id),
        "solution_id": str(release.solution_id),
        "solution_version_id": str(release.solution_version_id),
        "version_no": release.version_no,
        "status": release.status,
        "digest": release.digest,
        "compatibility": [d.model_dump(mode="json") for d in release.compatibility],
        "sealed_at": release.sealed_at,
    }


def releases_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.releases.read`: the releases of the project's solutions (of one solution, or one
    release): status, digest and compatibility diagnostics."""
    _require(call, "packaging:solution:read")
    solutions = SolutionService(call.scope, call.runtime)
    solution_id, release_id = _uuid(call, "solution_id"), _uuid(call, "release_id")
    if release_id is not None:
        if not _release_in_project(call, release_id):
            return _error(NOT_FOUND)
        release = solutions.get_release(release_id)
        if solution_id is not None and release.solution_id != solution_id:
            return _error(NOT_FOUND)
        return capped({"releases": [_release_doc(release)]})
    found = _solutions(call, solution_id)
    if found is None:
        return _error(NOT_FOUND)
    ws, proj = _slugs(call)
    releases: list[dict[str, Any]] = []
    for sol in found:
        for version in _versions(solutions, ws, proj, sol.id):
            page = solutions.list_releases(ws, proj, sol.id, version.id, limit=PAGE, cursor=None)
            releases += [_release_doc(r) for r in page.items]
    return capped({"releases": releases})


def _measured(run: evaluation_schemas.EvaluationRunOut) -> dict[str, Any]:
    return {
        "min_accuracy": run.accuracy,
        "max_p95_latency_ms": run.p95_latency_ms,
        "max_total_cost_usd": run.total_cost_usd,
    }


def _threshold_results(run: evaluation_schemas.EvaluationRunOut) -> list[dict[str, Any]]:
    """Each threshold with the run's measured value and whether it held (null until scored)."""
    out = []
    measured = _measured(run)
    for name, limit in sorted(run.thresholds.items()):
        value = measured.get(name)
        passed: bool | None = None
        if value is not None and name in measured:
            if name.startswith("min_"):
                passed = Decimal(str(value)) >= Decimal(str(limit))
            else:
                passed = Decimal(str(value)) <= Decimal(str(limit))
        out.append({"threshold": name, "limit": limit, "value": value, "passed": passed})
    return out


def evaluations_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.evaluations.read`: the evaluation runs of a release of the project (or one run):
    status and each threshold's result."""
    _require(call, "evaluation:run:read")
    release_id = _uuid(call, "release_id")
    run_id = _uuid(call, "evaluation_run_id")
    assert release_id is not None  # required by the input schema
    release = release_record(call.session, release_id, tenant_id=call.tenant_id)
    if release is None or release["project_id"] != call.project_id:
        return _error(NOT_FOUND)
    evaluations = EvaluationService(call.scope, call.runtime, TokenBudgetHooks())
    try:
        if run_id is not None:
            runs = [evaluations.stored_run(run_id)]
            if runs[0].release_id != release_id:
                return _error(NOT_FOUND)
        else:
            runs = evaluations.stored_runs(release_id, limit=MAX_RUNS)
    except UNKNOWN:
        return _error(NOT_FOUND)
    return capped(
        {
            "release_id": str(release_id),
            "runs": [
                {
                    "id": str(r.id),
                    "status": r.status,
                    "status_reason": r.status_reason,
                    "thresholds": _threshold_results(r),
                    "created_at": r.created_at,
                    "finished_at": r.finished_at,
                }
                for r in runs
            ],
        }
    )


def costs_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.costs.read`: one run's cost (a run of the project), or the project's totals of the
    last `days` days (30 by default) by day, model and run kind."""
    _require(call, "usage:read")
    governance = GovernanceService(call.scope)
    run_id = _uuid(call, "run_id")
    if run_id is not None:
        run = run_binding(call.session, call.tenant_id, run_id)
        if run is None or run.project_id != call.project_id:
            return _error(NOT_FOUND)
        try:
            usage = governance.run_usage(run_id)
        except UNKNOWN:
            return _error(NOT_FOUND)
        return capped(
            {
                "run_id": str(run_id),
                "totals": usage.totals.model_dump(mode="json"),
                "final_reconciled_cost": usage.final_reconciled_cost,
            }
        )
    days = int(call.args.get("days") or 30)
    return capped({"days": days, **governance.project_costs(call.project_id, days)})


def budgets_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.budgets.read`: the budgets of the organization, the project's workspace and the
    project, with what is left of each."""
    _require(call, "budget:read")
    governance = GovernanceService(call.scope)
    applies = {("workspace", call.workspace_id), ("project", call.project_id)}
    out = []
    for budget in governance.list_budgets().items:
        if budget.scope.type != "tenant" and (budget.scope.type, budget.scope.id) not in applies:
            continue
        status = governance.budget_status(budget.id)
        out.append(
            {
                "id": str(budget.id),
                "name": budget.name,
                "scope": budget.scope.model_dump(mode="json"),
                "period": budget.period,
                "metric": budget.metric,
                "currency": budget.currency,
                "amount": budget.amount,
                "status": budget.status,
                "enforcement": budget.enforcement,
                "reserved": status.reserved,
                "settled": status.settled,
                "remaining": status.remaining,
                "utilization": status.utilization,
            }
        )
    return capped({"budgets": out})


def board_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.board.read`: the run's project's board (`board.service`, which never commits)."""
    _require(call, "project:read")
    ws, proj = _slugs(call)
    return capped(
        {"board": BoardService(call.scope).project_board(ws, proj).model_dump(mode="json")}
    )


def targets_read(call: InternalToolCall) -> dict[str, Any]:
    """`seth.targets.read`: the targets of the run's project and of its workspace with no
    project, without any `credential_ref`."""
    _require(call, "delivery:target:read")
    ws, proj = _slugs(call)
    targets = TargetService(call.scope)
    found: list[Any] = []
    cursor = None
    while len(found) < MAX_ITEMS:
        page = targets.list(status=None, workspace=ws, project=None, limit=PAGE, cursor=cursor)
        found += [t for t in page.items if t.project in (None, proj)]
        cursor = page.next_cursor
        if cursor is None:
            break
    docs = [_without_credential_ref(t.model_dump(mode="json")) for t in found[:MAX_ITEMS]]
    return capped({"targets": docs, "truncated": len(found) > MAX_ITEMS})


# ====================================================================== proposals (design 6)
def bound_request(call: InternalToolCall, kind: str) -> Mapping[str, Any] | None:
    """The assist request this run serves, from server-side state only (the run row, the
    request rows and the installed specialist); None when there is not exactly one."""
    run = run_binding(call.session, call.tenant_id, call.run_id)
    if run is None or run.project_id != call.project_id or run.agent_id is None:
        return None
    named: UUID | None = None
    key = run.idempotency_key or ""
    if key.startswith(RUN_KEY_PREFIX):
        try:
            named = UUID(key[len(RUN_KEY_PREFIX) :])
        except ValueError:
            named = None
    found = []
    for row in repository.bound_requests(
        call.session,
        tenant_id=call.tenant_id,
        run_id=call.run_id,
        named=named,
        project_id=call.project_id,
        requested_by=run.initiated_by_principal_id,
    ):
        specialist_key = row["specialist_key"]
        if specialist_key is None or kind not in catalog.CATALOG[specialist_key].proposal_kinds:
            continue
        spec = repository.get_specialist(
            call.session, tenant_id=call.tenant_id, project_id=call.project_id, key=specialist_key
        )
        if spec is not None and spec["agent_id"] == run.agent_id:
            found.append(row)
    return found[0] if len(found) == 1 else None


def _propose(
    call: InternalToolCall,
    kind: str,
    action: str,
    checked: Callable[[InternalToolCall], dict[str, Any]],
) -> dict[str, Any]:
    """Store the proposal `checked` makes of the call's arguments (its `error` is the answer
    instead), for the run's assist request; a replay of the step answers the stored id."""
    _require(call, action)
    request = bound_request(call, kind)
    if request is None:
        return _error(NOT_AN_ASSIST_RUN)
    replay = repository.step_proposal(
        call.session, tenant_id=call.tenant_id, run_id=call.run_id, step_key=call.step_key
    )
    if replay is not None:
        return {"proposal_id": str(replay["id"]), "truncated": False}
    try:
        payload = checked(call)
    except ValidationError as exc:
        return _error("invalid_payload", problems=[e["msg"] for e in exc.errors()][:20])
    except MalformedId:
        return _error("invalid_payload", problems=["release_id is not a UUID"])
    if "error" in payload:
        return payload
    proposal_id = record_proposal(
        call.scope, request, run_id=call.run_id, step_key=call.step_key, kind=kind,
        payload=payload,
    )  # fmt: skip
    return {"proposal_id": str(proposal_id), "truncated": False}


def _app_request(call: InternalToolCall) -> dict[str, Any]:
    """`appgen.schemas.AppRequestCreate`: a read connection of the run's project, tables of its
    latest `schema` discovery and a model usable in the project."""
    body = AppRequestCreate(**call.args)
    conns = ConnectionService(call.scope, call.runtime)
    placement = conns.placement(body.connection_id)
    here = {"workspace_id": call.workspace_id, "project_id": call.project_id}
    if placement is None or not in_scope(here, placement.workspace_id, placement.project_id):
        return _error(NOT_FOUND)
    discovery = conns.discovery_of(body.connection_id)
    if discovery is None or discovery.kind != "schema":
        return _error("connection_not_discovered")
    known = {_table_doc(t)["name"] for t in discovery.payload.get("tables") or []}
    unknown = [t for t in body.tables if t.lower() not in known]
    if unknown:
        return _error("unknown_table", tables=unknown)
    problem = (
        ModelService(call.scope, call.runtime)
        .problems_at(_project(call), [body.model_id])
        .get(body.model_id)
    )
    if problem is not None:
        return _error(NOT_FOUND if problem == "model_not_found" else problem)
    return body.model_dump(mode="json")


def _evaluation_run(call: InternalToolCall) -> dict[str, Any]:
    """`{release_id}`: a release of the run's project."""
    release_id = _parsed(call.args["release_id"])
    if not _release_in_project(call, release_id):
        return _error(NOT_FOUND)
    return {"release_id": str(release_id)}


def _delivery_plan(call: InternalToolCall) -> dict[str, Any]:
    """`delivery.schemas.PlanCreate`: a release of the run's project and a target of the project
    or its workspace."""
    body = PlanCreate(**call.args)
    if not _release_in_project(call, body.release_id):
        return _error(NOT_FOUND)
    if not _target_in_project(call, body.target_id):
        return _error(NOT_FOUND)
    return body.model_dump(mode="json")


def propose_app_request(call: InternalToolCall) -> dict[str, Any]:
    """`seth.propose.app_request`."""
    return _propose(call, "appgen_request", "appgen:request:write", _app_request)


def propose_evaluation_run(call: InternalToolCall) -> dict[str, Any]:
    """`seth.propose.evaluation_run`."""
    return _propose(call, "evaluation_run", "evaluation:run:request", _evaluation_run)


def propose_delivery_plan(call: InternalToolCall) -> dict[str, Any]:
    """`seth.propose.delivery_plan`."""
    return _propose(call, "delivery_plan", "delivery:plan:request", _delivery_plan)


REGISTRY: Mapping[str, InternalToolFn] = MappingProxyType(
    {
        "seth.catalog.read": _read(catalog_read),
        "seth.solutions.read": _read(solutions_read),
        "seth.releases.read": _read(releases_read),
        "seth.evaluations.read": _read(evaluations_read),
        "seth.costs.read": _read(costs_read),
        "seth.budgets.read": budgets_read,
        "seth.board.read": board_read,
        "seth.targets.read": targets_read,
        "seth.propose.app_request": propose_app_request,
        "seth.propose.evaluation_run": propose_evaluation_run,
        "seth.propose.delivery_plan": propose_delivery_plan,
    }
)
