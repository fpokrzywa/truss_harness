"""Resolve the `{org}` path slug to a client the caller may see, then set the client context.

Ported from SETH 3ba0e06 `tenancy/dependencies.py`. Changes (tenancy part 2b, DECISIONS B-1019):

* The client is chosen only from what the caller is authorized for, never from the path: a
  person's clients come from `shifde_my_memberships` (B-1009: a live binding there, direct with
  an active membership or through an active platform group), an API key's client from the key.
  The path slug is only compared with that; `set_tenant` never takes a value from the path or a
  header (identity review 3 item 5).
* A session or key pinned to a client resolves only that client.
* An unknown slug, another client's slug and a client the caller cannot see give the same 404.
* The PDP is built only for the resolved client, so the denial audit (`register_denial_audit`)
  files a denial only in a client the caller was authorized to address.
* A client that is not active (suspended or archived) is refused on every route with 409
  `scope_archived` (F-03, B-2512). Only `GET` / `PATCH /orgs/{org}` (`OrgStatusScopeDep`) still
  admit its owner and the platform administrator: the status view, and an archived client's
  restore (`update_org` refuses every owner edit of a suspended client).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from shifde.authz import service as authz
from shifde.core import db
from shifde.core.context import Principal, bind_log_context
from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.errors import NotFound, ScopeArchived
from shifde.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from shifde.identity import service as identity
from shifde.identity.dependencies import AuthDep
from shifde.tenancy import service
from shifde.tenancy.scope import TenantScope

ORG_NOT_FOUND = "organization not found"


def _visible_client(
    session: Session, principal: Principal, org: str
) -> tuple[UUID, str, str, str] | None:
    """(id, slug, name, status) of the client `org` names, when the caller may address it."""
    if principal.principal_type == "user":
        for m in identity.my_memberships(session):
            if m.slug == org and principal.tenant_id in (None, m.tenant_id):
                return m.tenant_id, m.slug, m.name, m.status
        return None
    if principal.tenant_id is None:  # a service account always belongs to one client
        return None
    # A service-account key: the client is the key's, set from the authenticated credential.
    db.set_tenant(session, principal.tenant_id)
    header = service.tenant_header(session, principal.tenant_id)
    if header is None or header[0] != org:
        db.set_tenant(session, None)
        return None
    return principal.tenant_id, *header


def _resolve(org: str, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep) -> TenantScope:
    """404 unless the caller may address `org` (no oracle for another client)."""
    principal = auth.principal
    found = _visible_client(session, principal, org)
    if found is None:
        raise NotFound(ORG_NOT_FOUND)
    tenant_id, slug, name, status = found
    db.set_tenant(session, tenant_id)
    ctx.tenant_id = tenant_id
    bind_log_context(tenant_id=str(tenant_id))
    return TenantScope(
        session=session,
        ctx=ctx,
        principal=principal,
        tenant_id=tenant_id,
        tenant_slug=slug,
        tenant_name=name,
        tenant_status=status,
        pdp=authz.new_pdp(session, principal, tenant_id),
    )


def _refuse_inactive(scope: TenantScope) -> None:
    raise ScopeArchived(f"organization is {scope.tenant_status}")


def resolve_org(org: str, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep) -> TenantScope:
    """`_resolve`, then 409 `scope_archived` for a client that is not active."""
    scope = _resolve(org, auth, session, ctx)
    if scope.tenant_status != "active":
        _refuse_inactive(scope)
    return scope


def resolve_org_status(
    org: str, auth: AuthDep, session: SessionDep, ctx: RequestCtxDep
) -> TenantScope:
    """`resolve_org` for `GET` / `PATCH /orgs/{org}`: a client that is not active is admitted
    for its owner (`tenant:admin`) and the platform administrator only."""
    scope = _resolve(org, auth, session, ctx)
    if scope.tenant_status != "active" and not (
        scope.pdp.authorize("tenant:admin", scope.tenant_ref).allowed
        or service.is_platform_admin(session, scope.principal)
    ):
        _refuse_inactive(scope)
    return scope


ScopeDep = Annotated[TenantScope, Depends(resolve_org)]
OrgStatusScopeDep = Annotated[TenantScope, Depends(resolve_org_status)]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
CursorQuery = Annotated[str | None, Query()]
IncludeArchivedQuery = Annotated[bool, Query()]

__all__ = [
    "DEFAULT_LIMIT",
    "ORG_NOT_FOUND",
    "CursorQuery",
    "IncludeArchivedQuery",
    "LimitQuery",
    "OrgStatusScopeDep",
    "ScopeDep",
    "resolve_org",
    "resolve_org_status",
]
