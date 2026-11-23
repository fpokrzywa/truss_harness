"""`BoardOut` (sub-project 7 design 7.2): projects, their solutions with the newest releases, and
one cell per (solution, visible target). A field the caller may not read is null."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from shifde.delivery.schemas import DeploymentStatus

EvaluationStatus = Literal["queued", "running", "passed", "failed", "error"]
PlanCellStatus = Literal["requested", "planning", "sealed", "approved", "expired"]


class BoardRef(BaseModel):
    id: UUID
    slug: str
    name: str


class BoardEvaluation(BaseModel):
    run_id: UUID
    status: EvaluationStatus


class BoardRelease(BaseModel):
    id: UUID
    version_no: int
    digest12: str
    status: Literal["sealed", "withdrawn"]
    sealed_at: datetime
    evaluation: BoardEvaluation | None


class BoardCurrent(BaseModel):
    release_id: UUID
    version_no: int
    deployment_id: UUID
    verified_at: datetime


class BoardAttempt(BaseModel):
    deployment_id: UUID
    release_id: UUID
    status: DeploymentStatus
    status_reason: str | None
    updated_at: datetime | None


class BoardOpenPlan(BaseModel):
    plan_id: UUID
    release_id: UUID
    status: PlanCellStatus
    expires_at: datetime | None


class BoardTarget(BaseModel):
    target_id: UUID
    name: str
    environment: str
    platform_kind: str
    current: BoardCurrent | None
    last_attempt: BoardAttempt | None
    open_plan: BoardOpenPlan | None


class BoardSolution(BaseModel):
    id: UUID
    name: str
    status: Literal["active", "archived"]
    latest_version_no: int | None
    releases: list[BoardRelease]
    targets: list[BoardTarget]


class BoardProject(BaseModel):
    workspace: BoardRef
    project: BoardRef
    truncated: bool
    solutions: list[BoardSolution]


class BoardOut(BaseModel):
    generated_at: datetime
    projects: list[BoardProject]
    next_cursor: str | None
