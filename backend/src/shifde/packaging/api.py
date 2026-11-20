"""Solution and solution version routes (sub-project 3 design sections 4 and 5, settled call 7):
under a project, `{ws}` and `{proj}` named as in the tenancy routes."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.adapters.deps import RuntimeDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.packaging import schemas as s
from shifde.packaging.service import SolutionService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
SOLUTIONS = "/orgs/{org}/workspaces/{ws}/projects/{proj}/solutions"


def _solutions(scope: ScopeDep, runtime: RuntimeDep) -> SolutionService:
    return SolutionService(scope, runtime)


Solutions = Annotated[SolutionService, Depends(_solutions)]


@router.get(SOLUTIONS, tags=["packaging"])
def list_solutions(
    svc: Solutions,
    ws: str,
    proj: str,
    status: Annotated[Literal["active", "archived"] | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.SolutionOut]:
    return svc.list(ws, proj, status=status, limit=limit, cursor=cursor)


@router.post(SOLUTIONS, status_code=201, tags=["packaging"])
def create_solution(svc: Solutions, ws: str, proj: str, body: s.SolutionCreate) -> s.SolutionOut:
    return svc.create(ws, proj, body)


@router.get(SOLUTIONS + "/{solution_id}", tags=["packaging"])
def get_solution(svc: Solutions, ws: str, proj: str, solution_id: UUID) -> s.SolutionOut:
    return svc.get(ws, proj, solution_id)


@router.post(SOLUTIONS + "/{solution_id}/archive", tags=["packaging"])
def archive_solution(svc: Solutions, ws: str, proj: str, solution_id: UUID) -> s.SolutionOut:
    return svc.archive(ws, proj, solution_id)


@router.get(SOLUTIONS + "/{solution_id}/versions", tags=["packaging"])
def list_solution_versions(
    svc: Solutions,
    ws: str,
    proj: str,
    solution_id: UUID,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.SolutionVersionOut]:
    return svc.list_versions(ws, proj, solution_id, limit=limit, cursor=cursor)


@router.post(SOLUTIONS + "/{solution_id}/versions", status_code=201, tags=["packaging"])
def create_solution_version(
    svc: Solutions, ws: str, proj: str, solution_id: UUID, body: s.SolutionVersionCreate
) -> s.SolutionVersionOut:
    return svc.create_version(ws, proj, solution_id, body)


@router.get(SOLUTIONS + "/{solution_id}/versions/{version_id}", tags=["packaging"])
def get_solution_version(
    svc: Solutions, ws: str, proj: str, solution_id: UUID, version_id: UUID
) -> s.SolutionVersionOut:
    return svc.get_version(ws, proj, solution_id, version_id)
