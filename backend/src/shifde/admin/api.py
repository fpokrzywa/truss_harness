"""Admin endpoints under /api/v1 (authentication is the app-level router dependency).

Ported from SETH 3ba0e06 `admin/api.py`: part 1 (IAM), part 2 (service accounts and API
keys, B-1601), part 3a (identity providers, B-1621) and part 3b (secrets and audit, B-1641);
the `app` port mounts `router` (B-501). Changes (DECISIONS B-1401..B-1406, B-1601..,
B-1621.., B-1641..):

* Every `{org}` route resolves the client through `tenancy.dependencies.resolve_org` (B-1019),
  except `POST /orgs/{org}/users`: people are created by the platform administrator only
  (B-910, B-913), who is checked first and then addressed to the client by slug.
* `/orgs/{org}/groups` lists the platform groups bound in that client (id, name, bindings
  there); SETH's per-client group writes are replaced by `/platform/groups*`, platform
  administrator only (design 5.3).
* `POST /platform/orgs/{org}/group-bindings`: the platform administrator binds a platform group
  in a client without being its owner (B-1602).
* `/platform/idps*` are the platform administrator's (one 403 for everyone else whatever the id)
  and reach installation IdPs only; `/orgs/{org}/idps*` reach that client's IdPs only (B-1621).
* `/orgs/{org}/secrets*` and `/orgs/{org}/audit-events*` reach that client's secrets, events
  and chain only (B-1641, B-1642).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from shifde.admin import credentials, iam, providers
from shifde.admin import schemas as s
from shifde.core.deps import RequestCtxDep, SessionDep, SettingsDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity.dependencies import AuthDep
from shifde.identity.oidc import OidcClient
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")


def _oidc_client(request: Request) -> OidcClient:
    client: OidcClient = request.app.state.oidc
    return client


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


# ---------------------------------------------------------------- platform group bindings
@router.post("/platform/orgs/{org}/group-bindings", status_code=201, tags=["roles"])
def bind_group_as_platform_admin(
    org: str, body: s.GroupBindingCreate, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep
) -> s.BindingOut:
    """Platform administrator only (B-1602): binds a platform group in client `org` without
    being its owner, through `tenancy.service.bind_group_as_platform_admin`."""
    return iam.bind_group_as_platform_admin(session, ctx, auth.principal, org, body)


# ---------------------------------------------------------------- service accounts & API keys
@router.get("/orgs/{org}/service-accounts", tags=["service-accounts"])
def list_service_accounts(
    scope: ScopeDep, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.ServiceAccountOut]:
    return credentials.list_service_accounts(scope, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/service-accounts", status_code=201, tags=["service-accounts"])
def create_service_account(scope: ScopeDep, body: s.ServiceAccountCreate) -> s.ServiceAccountOut:
    return credentials.create_service_account(scope, body)


@router.get("/orgs/{org}/service-accounts/{sa_id}", tags=["service-accounts"])
def get_service_account(scope: ScopeDep, sa_id: UUID) -> s.ServiceAccountOut:
    return credentials.get_service_account(scope, sa_id)


@router.patch("/orgs/{org}/service-accounts/{sa_id}", tags=["service-accounts"])
def update_service_account(
    scope: ScopeDep, sa_id: UUID, body: s.ServiceAccountPatch
) -> s.ServiceAccountOut:
    return credentials.update_service_account(scope, sa_id, body)


@router.get("/orgs/{org}/api-keys", tags=["api-keys"])
def list_api_keys(
    scope: ScopeDep,
    principal_type: Literal["user", "service_account"] | None = None,
    principal_id: UUID | None = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.ApiKeyOut]:
    return credentials.list_api_keys(
        scope, principal_type=principal_type, principal_id=principal_id, limit=limit, cursor=cursor
    )


@router.post("/orgs/{org}/api-keys", status_code=201, tags=["api-keys"])
def create_api_key(scope: ScopeDep, settings: SettingsDep, body: s.ApiKeyCreate) -> s.ApiKeyCreated:
    created, _ = credentials.create_api_key(scope, settings, body)
    return created


@router.post("/orgs/{org}/api-keys/{key_id}/rotate", status_code=201, tags=["api-keys"])
def rotate_api_key(
    scope: ScopeDep, settings: SettingsDep, key_id: UUID, body: s.ApiKeyRotate
) -> s.ApiKeyCreated:
    return credentials.rotate_api_key(scope, settings, key_id, body)


@router.post("/orgs/{org}/api-keys/{key_id}/revoke", tags=["api-keys"])
def revoke_api_key(scope: ScopeDep, key_id: UUID) -> s.ApiKeyOut:
    return credentials.revoke_api_key(scope, key_id)


# ---------------------------------------------------------------- identity providers (part 3a)
def _tenant_idp_scope(scope: ScopeDep) -> providers.IdpScope:
    return providers.IdpScope.for_tenant(scope)


def _platform_idp_scope(
    auth: AuthDep, session: SessionDep, ctx: RequestCtxDep
) -> providers.IdpScope:
    return providers.IdpScope.for_platform(session, ctx, auth.principal)


TenantIdp = Annotated[providers.IdpScope, Depends(_tenant_idp_scope)]
PlatformIdp = Annotated[providers.IdpScope, Depends(_platform_idp_scope)]


@router.get("/platform/idps", tags=["idps"])
def list_platform_idps(
    sc: PlatformIdp, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.IdpOut]:
    return providers.list_idps(sc, limit=limit, cursor=cursor)


@router.post("/platform/idps", status_code=201, tags=["idps"])
def create_platform_idp(sc: PlatformIdp, settings: SettingsDep, body: s.IdpCreate) -> s.IdpOut:
    return providers.create_idp(sc, settings, body)


@router.get("/platform/idps/{idp_id}", tags=["idps"])
def get_platform_idp(sc: PlatformIdp, idp_id: UUID) -> s.IdpOut:
    return providers.get_idp(sc, idp_id)


@router.patch("/platform/idps/{idp_id}", tags=["idps"])
def update_platform_idp(
    sc: PlatformIdp, settings: SettingsDep, idp_id: UUID, body: s.IdpPatch
) -> s.IdpOut:
    return providers.update_idp(sc, settings, idp_id, body)


@router.post("/platform/idps/{idp_id}/test", tags=["idps"])
def test_platform_idp(sc: PlatformIdp, request: Request, idp_id: UUID) -> s.IdpTestOut:
    return providers.test_idp(sc, _oidc_client(request), idp_id)


@router.get("/orgs/{org}/idps", tags=["idps"])
def list_tenant_idps(
    sc: TenantIdp, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.IdpOut]:
    return providers.list_idps(sc, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/idps", status_code=201, tags=["idps"])
def create_tenant_idp(sc: TenantIdp, settings: SettingsDep, body: s.IdpCreate) -> s.IdpOut:
    return providers.create_idp(sc, settings, body)


@router.get("/orgs/{org}/idps/{idp_id}", tags=["idps"])
def get_tenant_idp(sc: TenantIdp, idp_id: UUID) -> s.IdpOut:
    return providers.get_idp(sc, idp_id)


@router.patch("/orgs/{org}/idps/{idp_id}", tags=["idps"])
def update_tenant_idp(
    sc: TenantIdp, settings: SettingsDep, idp_id: UUID, body: s.IdpPatch
) -> s.IdpOut:
    return providers.update_idp(sc, settings, idp_id, body)


@router.post("/orgs/{org}/idps/{idp_id}/test", tags=["idps"])
def test_tenant_idp(sc: TenantIdp, request: Request, idp_id: UUID) -> s.IdpTestOut:
    return providers.test_idp(sc, _oidc_client(request), idp_id)


# ---------------------------------------------------------------- secrets (part 3b, B-1641)
@router.get("/orgs/{org}/secrets", tags=["secrets"])
def list_secrets(
    scope: ScopeDep, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.SecretOut]:
    return providers.list_secrets(scope, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/secrets", status_code=201, tags=["secrets"])
def create_secret(scope: ScopeDep, settings: SettingsDep, body: s.SecretCreate) -> s.SecretOut:
    return providers.create_secret(scope, settings, body)


@router.post("/orgs/{org}/secrets/{secret_id}/rotate", tags=["secrets"])
def rotate_secret(
    scope: ScopeDep, settings: SettingsDep, secret_id: UUID, body: s.SecretRotate
) -> s.SecretOut:
    return providers.rotate_secret(scope, settings, secret_id, body)


@router.patch("/orgs/{org}/secrets/{secret_id}", tags=["secrets"])
def patch_secret(scope: ScopeDep, secret_id: UUID, body: s.SecretPatch) -> s.SecretOut:
    return providers.disable_secret(scope, secret_id, body)


# ---------------------------------------------------------------- audit (part 3b, B-1642)
@router.get("/orgs/{org}/audit-events", tags=["audit"])
def list_audit_events(
    scope: ScopeDep,
    category: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    occurred_from: Annotated[datetime | None, Query(alias="from")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.AuditEventOut]:
    return providers.list_audit_events(
        scope,
        category=category,
        actor=actor,
        action=action,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        limit=limit,
        cursor=cursor,
    )


@router.get("/orgs/{org}/audit-events/verify", tags=["audit"])
def verify_audit(scope: ScopeDep) -> s.AuditVerifyOut:
    return providers.verify_audit(scope)
