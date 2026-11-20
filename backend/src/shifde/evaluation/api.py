"""Evaluation dataset and evaluation run routes (sub-project 3 design section 6, settled call 7):
datasets under a project, `{ws}` and `{proj}` named as in the tenancy routes; evaluation runs
under a release (`/orgs/{org}/releases/{release_id}/evaluations`) and under the organization
(`/orgs/{org}/evaluation-runs/{run_id}`)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from shifde.adapters.deps import RuntimeDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.evaluation import schemas as s
from shifde.evaluation.service import EvaluationService
from shifde.runtime.governance import GovernanceHooks
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
DATASETS = "/orgs/{org}/workspaces/{ws}/projects/{proj}/evaluation-datasets"
EVALUATIONS = "/orgs/{org}/releases/{release_id}/evaluations"


def _evaluations(scope: ScopeDep, runtime: RuntimeDep, request: Request) -> EvaluationService:
    hooks: GovernanceHooks = request.app.state.governance
    return EvaluationService(scope, runtime, hooks)


Evaluations = Annotated[EvaluationService, Depends(_evaluations)]


@router.get(DATASETS, tags=["evaluation"])
def list_datasets(
    svc: Evaluations,
    ws: str,
    proj: str,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.DatasetOut]:
    return svc.list_datasets(ws, proj, limit=limit, cursor=cursor)


@router.post(DATASETS, status_code=201, tags=["evaluation"])
def create_dataset(svc: Evaluations, ws: str, proj: str, body: s.DatasetCreate) -> s.DatasetOut:
    return svc.create_dataset(ws, proj, body)


@router.get(DATASETS + "/{dataset_id}", tags=["evaluation"])
def get_dataset(svc: Evaluations, ws: str, proj: str, dataset_id: UUID) -> s.DatasetOut:
    return svc.get_dataset(ws, proj, dataset_id)


@router.post(EVALUATIONS, status_code=201, tags=["evaluation"])
def request_evaluation(
    svc: Evaluations, release_id: UUID, body: s.EvaluationRequest
) -> s.EvaluationRunOut:
    return svc.request(release_id, body)


@router.get(EVALUATIONS, tags=["evaluation"])
def list_evaluations(
    svc: Evaluations,
    release_id: UUID,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.EvaluationRunOut]:
    return svc.list_runs(release_id, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/evaluation-runs/{run_id}", tags=["evaluation"])
def get_evaluation_run(svc: Evaluations, run_id: UUID) -> s.EvaluationRunOut:
    return svc.get_run(run_id)
