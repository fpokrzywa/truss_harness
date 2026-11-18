"""Tenancy endpoints under /api/v1 (authentication is the app-level router dependency).

Ported from SETH 3ba0e06 `tenancy/api.py`; the `app` port mounts `router` (B-501). Changes
(tenancy part 2b, DECISIONS B-1019, B-1020): organization lists return the summary shape
(B-1011); every `{org}` route resolves the client through `resolve_org` (only a client the caller
may see, B-1009); `GET /platform/status` for platform administrators only (APP-2, B-2401);
`POST /platform/orgs/{org}/suspend` and `/unsuspend` for platform administrators only (F-03,
B-2511); a client that is not active is refused on every `{org}` route but `GET` / `PATCH
/orgs/{org}`, which still admit its owner and the platform administrator (B-2512).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from shifde.core.deps import RequestCtxDep, SessionDep, SettingsDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity.dependencies import AuthDep
from shifde.identity.schemas import MemberOut
from shifde.tenancy import service
from shifde.tenancy.dependencies import (
    CursorQuery,
    IncludeArchivedQuery,
    LimitQuery,
    OrgStatusScopeDep,
    ScopeDep,
)
from shifde.tenancy.schemas import (
    DashboardOut,
    OrgCreate,
    OrgOut,
    OrgPatch,
    OrgSummaryOut,
    PlatformStatusOut,
    ProjectCreate,
    ProjectOut,
    ProjectPatch,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspacePatch,
)

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------- organizations


@router.get("/orgs", tags=["orgs"])
def list_orgs(
    auth: AuthDep,
    session: SessionDep,
    include_archived: IncludeArchivedQuery = False,
    scope: Literal["all"] | None = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[OrgSummaryOut]:
    """The caller's clients; `?scope=all` (platform administrators only) lists every client."""
    if scope == "all":
        return service.list_all_orgs(
            session, auth.principal, include_archived=include_archived, limit=limit, cursor=cursor
        )
    return service.list_orgs(session, auth.principal, include_archived=include_archived)


@router.post("/orgs", status_code=201, tags=["orgs"])
def create_org(body: OrgCreate, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep) -> OrgOut:
    return service.create_org(session, ctx, auth.principal, body)


@router.get("/orgs/{org}", tags=["orgs"])
def get_org(scope: OrgStatusScopeDep) -> OrgOut:
    return service.get_org(scope)


@router.patch("/orgs/{org}", tags=["orgs"])
def update_org(scope: OrgStatusScopeDep, body: OrgPatch) -> OrgOut:
    return service.update_org(scope, body)


@router.get("/orgs/{org}/members", tags=["orgs"])
def list_members(
    scope: ScopeDep, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[MemberOut]:
    return service.list_members(scope, limit=limit, cursor=cursor)


# ---------------------------------------------------------------- workspaces


@router.get("/orgs/{org}/workspaces", tags=["workspaces"])
def list_workspaces(
    scope: ScopeDep,
    include_archived: IncludeArchivedQuery = False,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[WorkspaceOut]:
    return service.list_workspaces(
        scope, include_archived=include_archived, limit=limit, cursor=cursor
    )


@router.post("/orgs/{org}/workspaces", status_code=201, tags=["workspaces"])
def create_workspace(scope: ScopeDep, body: WorkspaceCreate) -> WorkspaceOut:
    return service.create_workspace(scope, body)


@router.get("/orgs/{org}/workspaces/{ws}", tags=["workspaces"])
def get_workspace(scope: ScopeDep, ws: str) -> WorkspaceOut:
    return service.get_workspace(scope, ws)


@router.patch("/orgs/{org}/workspaces/{ws}", tags=["workspaces"])
def update_workspace(scope: ScopeDep, ws: str, body: WorkspacePatch) -> WorkspaceOut:
    return service.update_workspace(scope, ws, body)


# ---------------------------------------------------------------- projects


@router.get("/orgs/{org}/workspaces/{ws}/projects", tags=["projects"])
def list_projects(
    scope: ScopeDep,
    ws: str,
    include_archived: IncludeArchivedQuery = False,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[ProjectOut]:
    return service.list_projects(
        scope, ws, include_archived=include_archived, limit=limit, cursor=cursor
    )


@router.post("/orgs/{org}/workspaces/{ws}/projects", status_code=201, tags=["projects"])
def create_project(scope: ScopeDep, ws: str, body: ProjectCreate) -> ProjectOut:
    return service.create_project(scope, ws, body)


@router.get("/orgs/{org}/workspaces/{ws}/projects/{proj}", tags=["projects"])
def get_project(scope: ScopeDep, ws: str, proj: str) -> ProjectOut:
    return service.get_project(scope, ws, proj)


@router.patch("/orgs/{org}/workspaces/{ws}/projects/{proj}", tags=["projects"])
def update_project(scope: ScopeDep, ws: str, proj: str, body: ProjectPatch) -> ProjectOut:
    return service.update_project(scope, ws, proj, body)


# ---------------------------------------------------------------- dashboard & platform status


@router.get("/orgs/{org}/dashboard", tags=["dashboard"])
def dashboard(
    scope: ScopeDep, workspace: str | None = None, project: str | None = None
) -> DashboardOut:
    return service.dashboard(scope, workspace_slug=workspace, project_slug=project)


@router.get("/platform/status", tags=["platform"])
def platform_status(auth: AuthDep, session: SessionDep, settings: SettingsDep) -> PlatformStatusOut:
    return service.platform_status(session, settings, auth.principal)


@router.post("/platform/orgs/{org}/suspend", tags=["platform"])
def suspend_org(auth: AuthDep, session: SessionDep, ctx: RequestCtxDep, org: str) -> OrgSummaryOut:
    return service.suspend_org(session, ctx, auth.principal, org)


@router.post("/platform/orgs/{org}/unsuspend", tags=["platform"])
def unsuspend_org(
    auth: AuthDep, session: SessionDep, ctx: RequestCtxDep, org: str
) -> OrgSummaryOut:
    return service.unsuspend_org(session, ctx, auth.principal, org)
