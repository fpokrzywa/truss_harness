"""Deployment target, executor and plan routes (design sections 3, 4, 5 and 9 items 2, 12 and 17;
DECISIONS B-2501, B-2664, B-2675..B-2679)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.core.deps import SettingsDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.delivery import schemas as s
from shifde.delivery.plans import PlanService
from shifde.delivery.service import ExecutorService, TargetService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")


def _targets(scope: ScopeDep, settings: SettingsDep) -> TargetService:
    return TargetService(scope, settings.delivery_databricks_host_suffixes)


Targets = Annotated[TargetService, Depends(_targets)]


def _executors(scope: ScopeDep, settings: SettingsDep) -> ExecutorService:
    return ExecutorService(scope, settings.pepper)


Executors = Annotated[ExecutorService, Depends(_executors)]


def _plans(scope: ScopeDep) -> PlanService:
    return PlanService(scope)


Plans = Annotated[PlanService, Depends(_plans)]
PlanStatus = Literal[
    "requested", "planning", "sealed", "approved", "rejected", "expired", "plan_failed"
]


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


@router.post("/orgs/{org}/delivery/executors", status_code=201, tags=["delivery"])
def create_executor(svc: Executors, body: s.ExecutorCreate) -> s.DeliveryExecutorOut:
    return svc.create(body)


@router.get("/orgs/{org}/delivery/executors", tags=["delivery"])
def list_executors(
    svc: Executors,
    status: Annotated[Literal["active", "disabled"] | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.DeliveryExecutorOut]:
    return svc.list_executors(status=status, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/delivery/executors/{executor_id}", tags=["delivery"])
def get_executor(svc: Executors, executor_id: UUID) -> s.DeliveryExecutorOut:
    return svc.get(executor_id)


@router.patch("/orgs/{org}/delivery/executors/{executor_id}", tags=["delivery"])
def update_executor(
    svc: Executors, executor_id: UUID, body: s.ExecutorPatch
) -> s.DeliveryExecutorOut:
    return svc.set_status(executor_id, body)


@router.get("/orgs/{org}/delivery/executors/{executor_id}/credentials", tags=["delivery"])
def list_executor_credentials(svc: Executors, executor_id: UUID) -> list[s.CredentialOut]:
    return svc.list_credentials(executor_id)


@router.post(
    "/orgs/{org}/delivery/executors/{executor_id}/credentials", status_code=201, tags=["delivery"]
)
def issue_executor_credential(svc: Executors, executor_id: UUID) -> s.CredentialIssued:
    return svc.issue_credential(executor_id)


@router.post(
    "/orgs/{org}/delivery/executors/{executor_id}/credentials/{credential_id}/rotate",
    status_code=201,
    tags=["delivery"],
)
def rotate_executor_credential(
    svc: Executors, executor_id: UUID, credential_id: UUID
) -> s.CredentialIssued:
    return svc.rotate_credential(executor_id, credential_id)


@router.post(
    "/orgs/{org}/delivery/executors/{executor_id}/credentials/{credential_id}/revoke",
    tags=["delivery"],
)
def revoke_executor_credential(
    svc: Executors, executor_id: UUID, credential_id: UUID
) -> s.CredentialOut:
    return svc.revoke_credential(executor_id, credential_id)


@router.post("/orgs/{org}/delivery/plans", status_code=201, tags=["delivery"])
def request_plan(svc: Plans, body: s.PlanCreate) -> s.PlanOut:
    return svc.request(body)


@router.get("/orgs/{org}/delivery/plans", tags=["delivery"])
def list_plans(
    svc: Plans,
    status: Annotated[PlanStatus | None, Query()] = None,
    target: Annotated[UUID | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.PlanOut]:
    return svc.list(status=status, target=target, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/delivery/plans/{plan_id}", tags=["delivery"])
def get_plan(svc: Plans, plan_id: UUID) -> s.PlanOut:
    return svc.get(plan_id)


@router.post("/orgs/{org}/delivery/plans/{plan_id}/approve", tags=["delivery"])
def approve_plan(svc: Plans, plan_id: UUID, body: s.PlanApprove) -> s.PlanOut:
    return svc.approve(plan_id, body)


@router.post("/orgs/{org}/delivery/plans/{plan_id}/reject", tags=["delivery"])
def reject_plan(svc: Plans, plan_id: UUID, body: s.PlanReject) -> s.PlanOut:
    return svc.reject(plan_id, body)
