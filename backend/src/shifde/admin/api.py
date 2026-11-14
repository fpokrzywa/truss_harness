"""Admin endpoints under /api/v1 (authentication is the app-level router dependency).

Ported from SETH 3ba0e06 `admin/api.py`, part 1 (IAM); the `app` port mounts `router` (B-501).
Service accounts and API keys (part 2), IdPs, secrets and audit (part 3) are added to the same
router by those parts. Changes (DECISIONS B-1401..B-1406):

* Every `{org}` route resolves the client through `tenancy.dependencies.resolve_org` (B-1019),
  except `POST /orgs/{org}/users`: people are created by the platform administrator only
  (B-910, B-913), who is checked first and then addressed to the client by slug.
* `/orgs/{org}/groups` lists the platform groups bound in that client (id, name, bindings
  there); SETH's per-client group writes are replaced by `/platform/groups*`, platform
  administrator only (design 5.3).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response

from shifde.admin import iam
from shifde.admin import schemas as s
from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity.dependencies import AuthDep
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- permission catalog
@router.get("/permissions", tags=["permissions"])
def list_permissions(_auth: AuthDep, session: SessionDep) -> Page[s.PermissionOut]:
    return iam.list_permissions(session)


# ---------------------------------------------------------------- users
@router.get("/orgs/{org}/users", tags=["users"])
def list_users(
    scope: ScopeDep, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.UserAdminOut]:
    return iam.list_users(scope, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/users", status_code=201, tags=["users"])
def create_user(
    org: str, body: s.UserCreate, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep
) -> s.UserCreated:
    """Platform administrator only (B-913): a person, a membership in `org` and an invitation."""
    return iam.create_user(session, ctx, auth.principal, org, body)


@router.patch("/orgs/{org}/users/{user_id}", tags=["users"])
def update_user(scope: ScopeDep, user_id: UUID, body: s.UserPatch) -> s.UserAdminOut:
    return iam.update_user_status(scope, user_id, body)


# ---------------------------------------------------------------- groups bound in a client
@router.get("/orgs/{org}/groups", tags=["groups"])
def list_groups(
    scope: ScopeDep, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.BoundGroupOut]:
    return iam.list_bound_groups(scope, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/groups/{group_id}", tags=["groups"])
def get_group(scope: ScopeDep, group_id: UUID) -> s.BoundGroupOut:
    return iam.get_bound_group(scope, group_id)


# ---------------------------------------------------------------- platform groups (admin only)
@router.get("/platform/groups", tags=["groups"])
def list_platform_groups(
    auth: AuthDep,
    session: SessionDep,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.GroupOut]:
    return iam.list_platform_groups(session, auth.principal, limit=limit, cursor=cursor)


@router.post("/platform/groups", status_code=201, tags=["groups"])
def create_platform_group(
    body: s.GroupCreate, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep
) -> s.GroupDetailOut:
    return iam.create_platform_group(session, ctx, auth.principal, body)


@router.get("/platform/groups/{group_id}", tags=["groups"])
def get_platform_group(group_id: UUID, auth: AuthDep, session: SessionDep) -> s.GroupDetailOut:
    return iam.get_platform_group(session, auth.principal, group_id)


@router.patch("/platform/groups/{group_id}", tags=["groups"])
def update_platform_group(
    group_id: UUID, body: s.GroupPatch, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep
) -> s.GroupDetailOut:
    return iam.update_platform_group(session, ctx, auth.principal, group_id, body)


@router.put("/platform/groups/{group_id}/members", tags=["groups"])
def replace_platform_group_members(
    group_id: UUID,
    body: s.GroupMembersPut,
    auth: AuthDep,
    session: SessionDep,
    ctx: RequestCtxDep,
) -> s.GroupDetailOut:
    return iam.replace_platform_group_members(session, ctx, auth.principal, group_id, body)


# ---------------------------------------------------------------- roles & bindings
@router.get("/orgs/{org}/roles", tags=["roles"])
def list_roles(
    scope: ScopeDep, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.RoleOut]:
    return iam.list_roles(scope, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/roles", status_code=201, tags=["roles"])
def create_role(scope: ScopeDep, body: s.RoleCreate) -> s.RoleOut:
    return iam.create_role(scope, body)


@router.get("/orgs/{org}/roles/{role_id}", tags=["roles"])
def get_role(scope: ScopeDep, role_id: UUID) -> s.RoleOut:
    return iam.get_role(scope, role_id)


@router.patch("/orgs/{org}/roles/{role_id}", tags=["roles"])
def update_role(scope: ScopeDep, role_id: UUID, body: s.RolePatch) -> s.RoleOut:
    return iam.update_role(scope, role_id, body)


@router.get("/orgs/{org}/role-bindings", tags=["roles"])
def list_role_bindings(
    scope: ScopeDep,
    subject_type: Literal["user", "group", "service_account"] | None = None,
    subject_id: UUID | None = None,
    scope_type: Literal["tenant", "workspace", "project", "resource"] | None = None,
    scope_id: UUID | None = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.BindingOut]:
    return iam.list_bindings(
        scope,
        subject_type=subject_type,
        subject_id=subject_id,
        scope_type=scope_type,
        scope_id=scope_id,
        limit=limit,
        cursor=cursor,
    )


@router.post("/orgs/{org}/role-bindings", status_code=201, tags=["roles"])
def create_role_binding(scope: ScopeDep, body: s.BindingCreate) -> s.BindingOut:
    return iam.create_binding(scope, body)


@router.delete("/orgs/{org}/role-bindings/{binding_id}", status_code=204, tags=["roles"])
def delete_role_binding(scope: ScopeDep, binding_id: UUID) -> Response:
    iam.delete_binding(scope, binding_id)
    return Response(status_code=204)


@router.get("/orgs/{org}/me/permissions", tags=["permissions"])
def my_permissions(
    scope: ScopeDep, workspace: str | None = None, project: str | None = None
) -> s.MePermissionsOut:
    return iam.my_permissions(scope, workspace, project)


# ---------------------------------------------------------------- parts 2-3
# Service accounts and API keys (`credentials`), IdPs, secrets and audit (`providers`) are added
# here by admin parts 2 and 3.
