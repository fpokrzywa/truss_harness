"""Routes of the P5 operate area (docs/api/PHASE_5_CONTRACT.md sections 2-3).

Client isolation (RT-12, B-2290): the client comes only from the path's organization through
`tenancy.dependencies.resolve_org` (`ScopeDep`); every service is built per request on that scope,
and every id in the path or body is read in that client by the service (another client's id
answers exactly like an unknown one). The router is mounted under `require_auth` by the app
(APP-2); there is no platform route here (SETH's runtime api has none).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any  # Any: event documents
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse

from shifde.adapters.deps import RuntimeDep
from shifde.core.errors import IdempotencyKeyReserved, ValidationFailed
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.runtime import schemas as s
from shifde.runtime.action import start_action_run
from shifde.runtime.governance import GovernanceHooks
from shifde.runtime.schedules import ScheduleService
from shifde.runtime.service import RUN_KEY_PREFIX, ApprovalService, RunService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1/orgs/{org}", tags=["operate"])
IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,200}$")


def _check_idempotency_key(key: str | None) -> None:
    """The `Idempotency-Key` of a route that creates a run: well formed, and not a key of the
    platform's own runs (refused before any lookup or replay, #373)."""
    if key is None:
        return
    if not IDEMPOTENCY_KEY.match(key):
        raise ValidationFailed("Idempotency-Key must be 1-200 printable ASCII characters")
    if key.startswith(RUN_KEY_PREFIX):
        raise IdempotencyKeyReserved(f"Idempotency-Key must not start with {RUN_KEY_PREFIX}")


def _hooks(request: Request) -> GovernanceHooks:
    hooks: GovernanceHooks = request.app.state.governance
    return hooks


def _runs(scope: ScopeDep, runtime: RuntimeDep, request: Request) -> RunService:
    return RunService(scope, runtime, _hooks(request))


def _approvals(scope: ScopeDep, runtime: RuntimeDep) -> ApprovalService:
    return ApprovalService(scope, runtime)


Runs = Annotated[RunService, Depends(_runs)]
Approvals = Annotated[ApprovalService, Depends(_approvals)]


@router.post("/runs", status_code=202)
def create_run(
    svc: Runs,
    body: s.RunCreate,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    _check_idempotency_key(idempotency_key)
    run, created = svc.create(body, idempotency_key)
    return JSONResponse(run.model_dump(mode="json"), status_code=202 if created else 200)


@router.post("/ontologies/{ontology_id}/actions/{action_key}/invoke", status_code=202)
def invoke_ontology_action(
    svc: Runs,
    ontology_id: UUID,
    action_key: Annotated[str, Path(min_length=1, max_length=100)],
    body: s.ActionInvokeIn,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """Registered ontology action: creates a run of kind `ontology_action` (contract section 4)."""
    _check_idempotency_key(idempotency_key)
    run, created = start_action_run(svc, ontology_id, action_key, body, idempotency_key)
    return JSONResponse(run.model_dump(mode="json"), status_code=202 if created else 200)


@router.get("/runs")
def list_runs(
    svc: Runs,
    status: Annotated[str | None, Query(max_length=40)] = None,
    kind: Annotated[str | None, Query(max_length=40)] = None,
    agent_id: UUID | None = None,
    workspace: Annotated[str | None, Query(max_length=100)] = None,
    project: Annotated[str | None, Query(max_length=100)] = None,
    initiated_by: UUID | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    outcome_unknown: bool = False,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.RunOut]:
    return svc.list_runs(
        status=status,
        kind=kind,
        agent_id=agent_id,
        workspace=workspace,
        project=project,
        initiated_by=initiated_by,
        since=from_,
        until=to,
        outcome_unknown=outcome_unknown,
        limit=limit,
        cursor=cursor,
    )


@router.get("/runs/{run_id}")
def get_run(svc: Runs, run_id: UUID) -> s.RunOut:
    return svc.get(run_id)


@router.get("/runs/{run_id}/steps")
def run_steps(svc: Runs, run_id: UUID) -> list[s.StepOut]:
    return svc.steps(run_id)


@router.get("/runs/{run_id}/events")
def run_events(svc: Runs, run_id: UUID) -> list[dict[str, Any]]:
    return svc.events(run_id)


@router.get("/runs/{run_id}/result")
def run_result(svc: Runs, run_id: UUID) -> s.ResultOut:
    return svc.result(run_id)


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(svc: Runs, run_id: UUID) -> s.RunOut:
    return svc.cancel(run_id)


@router.get("/approvals")
def list_approvals(
    svc: Approvals,
    status: Annotated[str, Query(max_length=20)] = "pending",
    run_id: UUID | None = None,
    tree_run_id: UUID | None = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.ApprovalOut]:
    return svc.list_approvals(status, run_id, limit, cursor, tree_run_id)


@router.get("/approvals/{approval_id}")
def get_approval(svc: Approvals, approval_id: UUID) -> s.ApprovalOut:
    return svc.get(approval_id)


@router.post("/approvals/{approval_id}/decide")
def decide_approval(svc: Approvals, approval_id: UUID, body: s.DecideIn) -> s.ApprovalOut:
    return svc.decide(approval_id, body)


# ------------------------------------------------------------------ pipelines (P6)
def _schedules(scope: ScopeDep, runtime: RuntimeDep) -> ScheduleService:
    return ScheduleService(scope, runtime)


Schedules = Annotated[ScheduleService, Depends(_schedules)]


@router.get("/pipelines/{pipeline_id}/runs")
def pipeline_runs(
    svc: Runs, pipeline_id: UUID, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.RunOut]:
    return svc.pipeline_runs(pipeline_id, limit, cursor)


@router.get("/runs/{run_id}/nodes")
def run_nodes(svc: Runs, run_id: UUID) -> s.NodeListOut:
    return svc.nodes(run_id)


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def run_artifact(svc: Runs, run_id: UUID, artifact_id: UUID) -> s.ArtifactOut:
    return svc.artifact(run_id, artifact_id)


@router.get("/pipelines/{pipeline_id}/schedules")
def list_schedules(svc: Schedules, pipeline_id: UUID) -> list[s.ScheduleOut]:
    return svc.list(pipeline_id)


@router.post("/pipelines/{pipeline_id}/schedules", status_code=201)
def create_schedule(svc: Schedules, pipeline_id: UUID, body: s.ScheduleCreate) -> s.ScheduleOut:
    return svc.create(pipeline_id, body)
