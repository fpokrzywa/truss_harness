"""Public DTOs for tenancy (SETH 3ba0e06 `tenancy/schemas.py`; docs/api/PHASE_1_CONTRACT.md).

Ported as far as `service` needs (part 2a). Change from SETH: organization lists use
`OrgSummaryOut`, without the client-written `description` and `settings` (tenancy review 1 item
4, DECISIONS B-1011). The platform-status shapes are SETH's (`platform_status`, B-2401)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
Status = Literal["active", "archived"]
OrgStatus = Literal["active", "suspended", "archived"]  # suspended: F-03, B-2511


class _Resource(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str | None = None
    status: Status
    row_version: int
    created_at: datetime
    updated_at: datetime | None = None


class OrgOut(_Resource):
    status: OrgStatus  # type: ignore[assignment]  # widens _Resource.status: clients only


class OrgSummaryOut(BaseModel):
    """One entry of an organization list: what the client chooser needs, nothing the client
    wrote about itself."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    status: OrgStatus


class WorkspaceOut(_Resource):
    pass


class ProjectOut(_Resource):
    workspace_slug: str


class _CreateBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class OrgCreate(_CreateBase):
    admin_email: str | None = Field(default=None, max_length=320)


class WorkspaceCreate(_CreateBase):
    pass


class ProjectCreate(_CreateBase):
    pass


class _PatchBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Status | None = None
    row_version: int = Field(ge=1)


class OrgPatch(_PatchBase):
    pass


class WorkspacePatch(_PatchBase):
    pass


class ProjectPatch(_PatchBase):
    pass


CaptureContent = Literal["full", "metadata"]


class CaptureContentPut(BaseModel):
    """The client's own `capture_content` setting; `null` unsets it (the environment default)."""

    model_config = ConfigDict(extra="forbid")

    capture_content: CaptureContent | None
    row_version: int = Field(ge=1)


class CaptureContentOut(BaseModel):
    capture_content: CaptureContent | None
    row_version: int


# ---------------------------------------------------------------- dashboard / platform status
class DashboardScopeOut(BaseModel):
    org: str
    workspace: str | None = None
    project: str | None = None


class DashboardCountsOut(BaseModel):
    workspaces: int
    projects: int
    members: int | None = None  # null when the caller lacks member:read


class ActivityOut(BaseModel):
    at: datetime
    actor: str | None
    action: str
    target: str | None


class DashboardOut(BaseModel):
    scope: DashboardScopeOut
    counts: DashboardCountsOut
    recent_activity: list[ActivityOut]


class DatabaseStatusOut(BaseModel):
    status: str
    revision: str | None
    head: str


class AuthStatusOut(BaseModel):
    local_enabled: bool
    oidc_providers: int


class WorkerStatusOut(BaseModel):
    status: Literal["healthy", "stale", "not_running"]
    count: int = 0
    last_heartbeat_at: datetime | None = None


class PlatformStatusOut(BaseModel):
    version: str
    environment: str
    database: DatabaseStatusOut
    auth: AuthStatusOut
    worker: WorkerStatusOut
