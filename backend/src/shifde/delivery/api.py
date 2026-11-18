"""Deployment target routes (design sections 3 and 9 item 2, DECISIONS B-2501)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.core.deps import SettingsDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.delivery import schemas as s
from shifde.delivery.service import TargetService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")


def _targets(scope: ScopeDep, settings: SettingsDep) -> TargetService:
    return TargetService(scope, settings.delivery_databricks_host_suffixes)


Targets = Annotated[TargetService, Depends(_targets)]


@router.post("/orgs/{org}/delivery/targets", status_code=201, tags=["delivery"])
def register_target(svc: Targets, body: s.TargetCreate) -> s.TargetOut:
    return svc.register_target(body)


@router.get("/orgs/{org}/delivery/targets", tags=["delivery"])
def list_targets(
    svc: Targets,
    status: Annotated[Literal["active", "archived"] | None, Query()] = None,
    workspace: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.TargetOut]:
    return svc.list(status=status, workspace=workspace, project=project, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/delivery/targets/{target_id}", tags=["delivery"])
def get_target(svc: Targets, target_id: UUID) -> s.TargetOut:
    return svc.get(target_id)


@router.post("/orgs/{org}/delivery/targets/{target_id}/archive", tags=["delivery"])
def archive_target(svc: Targets, target_id: UUID) -> s.TargetOut:
    return svc.archive_target(target_id)
