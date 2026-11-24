"""Tenancy use cases: organizations, workspaces, projects, dashboard, who may grant.

Layering: services call the PDP (authorization), audit (recording) and other modules' *service*
functions; SQL lives in `repository`. Services own the transaction boundary (explicit commit).

Ported from SETH 3ba0e06 `tenancy/service.py` (part 2a, DECISIONS B-1008..B-1014). Changes:
organization lists come from `shifde_my_memberships` alone (visibility by binding, no client
context switching) in the summary shape; the installation-wide list checks the principal, not
only the database flag; a platform administrator adding themselves to a client and every group
binding are audited in that client; group bindings for the platform administrator or the
client's owner only; `platform_status` for the platform administrator only (B-2401);
`suspend_org` / `unsuspend_org` for the platform administrator only, audited in that client, and
no owner edit of a suspended client (F-03, B-2511); `set_capture_content` writes the client's own
`capture_content` setting, audited (J-M2, B-2550).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any  # Any: settings documents are arbitrary JSON
from uuid import UUID

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from shifde import __version__
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.config_resolution import resolve_config
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import Conflict, Forbidden, NotFound, ScopeArchived, ValidationFailed
from shifde.core.health import migration_state
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.settings import Settings
from shifde.identity import service as identity
from shifde.identity.schemas import MemberOut, MemberUserOut
from shifde.tenancy import repository
from shifde.tenancy.models import Project, Tenant, Workspace
from shifde.tenancy.schemas import (
    ActivityOut,
    AuthStatusOut,
    CaptureContentOut,
    CaptureContentPut,
    DashboardCountsOut,
    DashboardOut,
    DashboardScopeOut,
    DatabaseStatusOut,
    OrgCreate,
    OrgOut,
    OrgPatch,
    OrgSummaryOut,
    PlatformStatusOut,
    ProjectCreate,
    ProjectOut,
    ProjectPatch,
    WorkerStatusOut,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspacePatch,
)
from shifde.tenancy.scope import TenantScope


# ------------------------------------------------------------------ helpers
def _commit(session: Session, what: str) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise Conflict(f"{what} conflicts with an existing resource") from exc


def _patch_values(
    body: OrgPatch | WorkspacePatch | ProjectPatch, current_status: str
) -> dict[str, Any]:
    """Translate a PATCH body into column values; enforces the archived-is-read-only rule."""
    sent = body.model_fields_set - {"row_version"}
    values: dict[str, Any] = {}
    if "name" in sent and body.name is not None:
        values["name"] = body.name
    if "description" in sent:
        values["description"] = body.description
    if "status" in sent and body.status is not None:
        values["status"] = body.status
    if not values:
        raise ValidationFailed("no updatable fields supplied")
    reactivation_only = values == {"status": "active"}
    if current_status == "archived" and not reactivation_only:
        raise ScopeArchived("archived scopes are read-only; reactivate it first")
    return values


def _lifecycle(kind: str, old: str, values: Mapping[str, Any]) -> str:
    new = values.get("status", old)
    if old == "active" and new == "archived":
        return f"tenancy.{kind}.archived"
    if old == "archived" and new == "active":
        return f"tenancy.{kind}.restored"
    return f"tenancy.{kind}.updated"


def _assert_tenant_active(scope: TenantScope) -> None:
    if scope.tenant_status != "active":
        raise ScopeArchived("organization is archived")


def _assert_workspace_active(ws: Workspace) -> None:
    if ws.status != "active":
        raise ScopeArchived("workspace is archived")


def _org_out(t: Tenant) -> OrgOut:
    return OrgOut.model_validate(t)


def _ws_out(w: Workspace) -> WorkspaceOut:
    return WorkspaceOut.model_validate(w)


def _project_out(p: Project, workspace_slug: str) -> ProjectOut:
    base = {
        f: getattr(p, f)
        for f in (
            "id",
            "slug",
            "name",
            "description",
            "status",
            "row_version",
            "created_at",
            "updated_at",
        )
    }
    return ProjectOut(**base, workspace_slug=workspace_slug)


# ------------------------------------------------------------------ organizations
def _is_platform_admin(session: Session, principal: Principal) -> bool:
    """The platform administrator, signed in as a person on a session not pinned to a client
    (never an API key): the principal and the PDP agree (tenancy review 1 item 4)."""
    return (
        principal.is_platform_admin
        and principal.principal_type == "user"
        and principal.credential == "session"
        and principal.tenant_id is None
        and authz.new_pdp(session, principal, None).authorize("platform:admin").allowed
    )


def is_platform_admin(session: Session, principal: Principal) -> bool:
    return _is_platform_admin(session, principal)


def _require_platform_admin(session: Session, principal: Principal) -> None:
    if not _is_platform_admin(session, principal):
        raise Forbidden("this is a platform administrator action")


def _summary(tenant_id: UUID, slug: str, name: str, status: str) -> OrgSummaryOut:
    return OrgSummaryOut.model_validate(
        {"id": tenant_id, "slug": slug, "name": name, "status": status}
    )


def list_orgs(
    session: Session, principal: Principal, *, include_archived: bool
) -> Page[OrgSummaryOut]:
    """The clients the caller can see: a live role binding there, direct (with an active
    membership) or through an active platform group (`shifde_my_memberships`, B-1009). Needs the
    caller's principal context; reads no client row, so no client context is switched (B-116
    item 6). The platform-administrator flag adds nothing here (see `list_all_orgs`)."""
    items: list[OrgSummaryOut] = []
    for m in identity.my_memberships(session):
        if principal.tenant_id not in (None, m.tenant_id):  # session pinned to one tenant
            continue
        if m.status == "archived" and not include_archived:
            continue
        items.append(_summary(m.tenant_id, m.slug, m.name, m.status))
    return Page[OrgSummaryOut](items=items, next_cursor=None)


def list_all_orgs(
    session: Session,
    principal: Principal,
    *,
    include_archived: bool,
    limit: int,
    cursor: str | None,
) -> Page[OrgSummaryOut]:
    """Installation metadata: id, slug, name and status of every org row (never a client's
    description or settings). Platform administrators only, checked on the principal and the
    PDP; the platform-admin RLS context is enabled strictly after that check."""
    _require_platform_admin(session, principal)
    with db.platform_admin_context(session):
        rows = repository.list_tenants(
            session, include_archived=include_archived, after=decode_cursor(cursor), limit=limit + 1
        )
        page, more = paginate(rows, limit)
        items = [_summary(t.id, t.slug, t.name, t.status) for t in page]
        # Read the cursor inside the context: after the rollback the rows are expired and
        # reloading one outside the platform-admin context finds nothing under RLS.
        next_cursor = encode_cursor(page[-1].id) if more and page else None
    session.rollback()
    return Page[OrgSummaryOut](items=items, next_cursor=next_cursor)


def _require_some_grant(scope: TenantScope) -> None:
    """The PDP grants the caller something in this client: a permission at organization scope,
    or any grant on a workspace or project of it (the path the selectors show). Hidden (404)
    otherwise, as an unknown client (review 2 item 4, B-1019)."""
    held = scope.pdp.effective_permissions(scope.tenant_ref) - authz.PLATFORM_ONLY_ACTIONS
    if not held and not scope.pdp.ancestor_workspace_ids():
        raise NotFound("organization not found")


def get_org(scope: TenantScope) -> OrgOut:
    _require_some_grant(scope)
    tenant = repository.get_tenant(scope.session, scope.tenant_id)
    if tenant is None:  # pragma: no cover - membership resolved it moments ago
        raise NotFound("organization not found")
    return _org_out(tenant)


def create_org(
    session: Session, ctx: RequestContext, principal: Principal, body: OrgCreate
) -> OrgOut:
    """Create a client (platform administrator only). `admin_email` names an existing person
    who becomes the client's owner: a member with `tenant_administrator` at organization scope
    (B-1010). A failure rolls back and leaves no client context set (B-116 item 6)."""
    _require_platform_admin(session, principal)
    tenant_id = uuid7()
    try:
        out = _create_org(session, ctx, principal, body, tenant_id)
        _commit(session, "organization")
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    db.set_tenant(session, None)
    return out


def _create_org(
    session: Session, ctx: RequestContext, principal: Principal, body: OrgCreate, tenant_id: UUID
) -> OrgOut:
    with db.platform_admin_context(session):
        if repository.tenant_slug_exists(session, body.slug):
            raise Conflict("organization slug is already taken")
        admin_user_id: UUID | None = None
        if body.admin_email:
            admin_user_id = identity.find_user_id_by_email(session, body.admin_email)
            if admin_user_id is None:
                raise ValidationFailed("admin_email does not match an existing user")
        try:
            repository.insert_tenant(
                session,
                {
                    "id": tenant_id,
                    "slug": body.slug,
                    "name": body.name,
                    "description": body.description,
                    "status": "active",
                    "settings": {},
                    "created_at": utcnow(),
                    "created_by": principal.user_id,
                },
            )
        except IntegrityError as exc:  # lost a slug race after the pre-check
            raise Conflict("organization slug is already taken") from exc
        db.set_tenant(session, tenant_id)
        if admin_user_id is not None:
            identity.add_member(session, tenant_id=tenant_id, user_id=admin_user_id)
            authz.grant_role(
                session,
                tenant_id=tenant_id,
                user_id=admin_user_id,
                role_key="tenant_administrator",
                scope_type="tenant",
                scope_id=tenant_id,
                created_by=principal.user_id,
            )
        audit.record(
            session,
            ctx,
            action="tenancy.org.created",
            tenant_id=tenant_id,
            target_type="org",
            target_id=tenant_id,
            target_label=f"org:{body.slug}",
            details={"slug": body.slug, "admin_email": body.admin_email},
        )
        session.flush()
        created = repository.get_tenant(session, tenant_id)
    if created is None:  # pragma: no cover
        raise NotFound("organization not found")
    return _org_out(created)


def update_org(scope: TenantScope, body: OrgPatch) -> OrgOut:
    scope.pdp.require("tenant:admin", scope.tenant_ref)
    tenant = repository.get_tenant(scope.session, scope.tenant_id)
    if tenant is None:  # pragma: no cover
        raise NotFound("organization not found")
    if tenant.status == "suspended":  # only the platform administrator lifts it (B-2511)
        raise ScopeArchived("organization is suspended")
    values = _patch_values(body, tenant.status)
    old_status = tenant.status  # the ORM row is refreshed by the UPDATE below
    updated = repository.update_tenant(scope.session, scope.tenant_id, body.row_version, values)
    if updated is None:
        raise Conflict("row_version mismatch: the organization was modified; reload and retry")
    audit.record(
        scope.session,
        scope.ctx,
        action=_lifecycle("org", old_status, values),
        tenant_id=scope.tenant_id,
        target_type="org",
        target_id=scope.tenant_id,
        target_label=f"org:{tenant.slug}",
        details={"changed": sorted(values)},
    )
    out = _org_out(updated)
    _commit(scope.session, "organization")
    return out


def set_capture_content(scope: TenantScope, body: CaptureContentPut) -> CaptureContentOut:
    """Set (or with `null` unset) the client's own `capture_content` setting, keeping every other
    settings key; audited in that client with the old and the new value (J-M2, issue #2)."""
    scope.pdp.require("tenant:admin", scope.tenant_ref)
    tenant = repository.get_tenant(scope.session, scope.tenant_id)
    if tenant is None:  # pragma: no cover
        raise NotFound("organization not found")
    if tenant.status == "suspended":  # only the platform administrator lifts it (B-2511)
        raise ScopeArchived("organization is suspended")
    if tenant.status == "archived":
        raise ScopeArchived("archived scopes are read-only; reactivate it first")
    settings = dict(tenant.settings or {})
    old = settings.get("capture_content")
    if body.capture_content is None:
        settings.pop("capture_content", None)
    else:
        settings["capture_content"] = body.capture_content
    updated = repository.update_tenant(
        scope.session, scope.tenant_id, body.row_version, {"settings": settings}
    )
    if updated is None:
        raise Conflict("row_version mismatch: the organization was modified; reload and retry")
    audit.record(
        scope.session,
        scope.ctx,
        action="tenancy.org.capture_content.updated",
        tenant_id=scope.tenant_id,
        target_type="org",
        target_id=scope.tenant_id,
        target_label=f"org:{tenant.slug}",
        details={"setting": "capture_content", "old": old, "new": body.capture_content},
    )
    out = CaptureContentOut(
        capture_content=updated.settings.get("capture_content"), row_version=updated.row_version
    )
    _commit(scope.session, "organization")
    return out


def suspend_org(
    session: Session, ctx: RequestContext, principal: Principal, org: str
) -> OrgSummaryOut:
    """The platform administrator suspends an active client (F-03, B-2511)."""
    return _set_org_suspension(session, ctx, principal, org, suspend=True)


def unsuspend_org(
    session: Session, ctx: RequestContext, principal: Principal, org: str
) -> OrgSummaryOut:
    """The platform administrator makes a suspended client active again (F-03, B-2511)."""
    return _set_org_suspension(session, ctx, principal, org, suspend=False)


def _set_org_suspension(
    session: Session, ctx: RequestContext, principal: Principal, org: str, *, suspend: bool
) -> OrgSummaryOut:
    """The administrator check comes first (one 403 for everyone else whatever the slug); the
    client is then looked up by slug under the flag, its status re-checked in the writing
    transaction and the change audited in that client only. No client context is left set."""
    _require_platform_admin(session, principal)
    current, new = ("active", "suspended") if suspend else ("suspended", "active")
    try:
        with db.platform_admin_context(session):
            tenant_id = tenant_id_by_slug(session, org)
            if tenant_id is None:
                raise NotFound("organization not found")
            db.set_tenant(session, tenant_id)
            tenant = repository.get_tenant(session, tenant_id)
            if tenant is None:  # pragma: no cover - found by slug moments ago
                raise NotFound("organization not found")
            if tenant.status != current:
                raise Conflict(f"organization is {tenant.status}, not {current}")
            updated = repository.update_tenant(
                session, tenant_id, tenant.row_version, {"status": new}
            )
            if updated is None:  # pragma: no cover - changed concurrently since the read
                raise Conflict("the organization was modified; reload and retry")
            audit.record(
                session,
                ctx,
                action="tenancy.org.suspended" if suspend else "tenancy.org.unsuspended",
                tenant_id=tenant_id,
                target_type="org",
                target_id=tenant_id,
                target_label=f"org:{updated.slug}",
                details={"by_platform_administrator": True},
            )
            out = _summary(updated.id, updated.slug, updated.name, updated.status)
            session.flush()
        _commit(session, "organization")
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    db.set_tenant(session, None)
    return out


def list_members(scope: TenantScope, *, limit: int, cursor: str | None) -> Page[MemberOut]:
    scope.pdp.require("member:read", scope.tenant_ref)
    rows = identity.list_members(
        scope.session, tenant_id=scope.tenant_id, after=decode_cursor(cursor), limit=limit + 1
    )
    page, more = paginate(rows, limit)
    return Page[MemberOut](
        items=[
            MemberOut(
                user=MemberUserOut(id=r.user_id, email=r.email, display_name=r.display_name),
                status=r.status,
                roles=r.role_names,
            )
            for r in page
        ],
        next_cursor=encode_cursor(page[-1].membership_id) if more and page else None,
    )


# ------------------------------------------------------------------ workspaces
def _get_workspace_row(scope: TenantScope, slug: str) -> Workspace:
    ws = repository.get_workspace_by_slug(scope.session, scope.tenant_id, slug)
    if ws is None:
        raise NotFound("workspace not found")
    return ws


def _ws_ref(scope: TenantScope, ws: Workspace) -> ResourceRef:
    return ResourceRef(tenant_id=scope.tenant_id, workspace_id=ws.id)


def _require_hiding(
    scope: TenantScope, action: str, ref: ResourceRef, hide_unless: str, ws: Workspace
) -> None:
    """`require` that answers 404 for invisible workspaces. A workspace that is the ancestor of
    one of the caller grants is visible (403, not 404) even without `workspace:read`."""
    if ws.id in scope.pdp.ancestor_workspace_ids():
        scope.pdp.require(action, ref)
    else:
        scope.pdp.require(action, ref, hide_unless=hide_unless)


def _readable_workspace(scope: TenantScope, slug: str) -> Workspace:
    ws = _get_workspace_row(scope, slug)
    if (
        not scope.pdp.authorize("workspace:read", _ws_ref(scope, ws)).allowed
        and ws.id not in scope.pdp.ancestor_workspace_ids()  # P1 gap: ancestor of a grant
    ):
        raise NotFound("workspace not found")  # invisible == nonexistent (no oracle)
    return ws


def list_workspaces(
    scope: TenantScope, *, include_archived: bool, limit: int, cursor: str | None
) -> Page[WorkspaceOut]:
    f = scope.pdp.visible_scopes("workspace:read")
    visible_ids = f.workspace_ids | scope.pdp.ancestor_workspace_ids()
    if not f.tenant_wide and not visible_ids:
        return Page[WorkspaceOut](items=[], next_cursor=None)
    rows = repository.list_workspaces(
        scope.session,
        tenant_id=scope.tenant_id,
        only_ids=None if f.tenant_wide else visible_ids,
        include_archived=include_archived,
        after=decode_cursor(cursor),
        limit=limit + 1,
    )
    page, more = paginate(rows, limit)
    return Page[WorkspaceOut](
        items=[_ws_out(w) for w in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def get_workspace(scope: TenantScope, slug: str) -> WorkspaceOut:
    return _ws_out(_readable_workspace(scope, slug))


def create_workspace(scope: TenantScope, body: WorkspaceCreate) -> WorkspaceOut:
    scope.pdp.require("workspace:create", scope.tenant_ref)
    _assert_tenant_active(scope)
    if repository.get_workspace_by_slug(scope.session, scope.tenant_id, body.slug):
        raise Conflict("workspace slug is already taken")
    ws_id = uuid7()
    repository.insert_workspace(
        scope.session,
        {
            "id": ws_id,
            "tenant_id": scope.tenant_id,
            "slug": body.slug,
            "name": body.name,
            "description": body.description,
            "status": "active",
            "settings": {},
            "created_at": utcnow(),
            "created_by": scope.principal.user_id,
        },
    )
    audit.record(
        scope.session,
        scope.ctx,
        action="tenancy.workspace.created",
        tenant_id=scope.tenant_id,
        target_type="workspace",
        target_id=ws_id,
        target_label=f"workspace:{body.slug}",
        workspace_id=ws_id,
        details={"slug": body.slug},
    )
    scope.session.flush()
    created = repository.get_workspace(scope.session, scope.tenant_id, ws_id)
    if created is None:  # pragma: no cover
        raise NotFound("workspace not found")
    out = _ws_out(created)
    _commit(scope.session, "workspace")
    return out


def update_workspace(scope: TenantScope, slug: str, body: WorkspacePatch) -> WorkspaceOut:
    ws = _get_workspace_row(scope, slug)
    _require_hiding(scope, "workspace:manage", _ws_ref(scope, ws), "workspace:read", ws)
    _assert_tenant_active(scope)
    values = _patch_values(body, ws.status)
    old_status = ws.status  # the ORM row is refreshed by the UPDATE below
    updated = repository.update_workspace(
        scope.session, scope.tenant_id, ws.id, body.row_version, values
    )
    if updated is None:
        raise Conflict("row_version mismatch: the workspace was modified; reload and retry")
    audit.record(
        scope.session,
        scope.ctx,
        action=_lifecycle("workspace", old_status, values),
        tenant_id=scope.tenant_id,
        target_type="workspace",
        target_id=ws.id,
        target_label=f"workspace:{ws.slug}",
        workspace_id=ws.id,
        details={"changed": sorted(values)},
    )
    out = _ws_out(updated)
    _commit(scope.session, "workspace")
    return out


# ------------------------------------------------------------------ projects
def _get_project_row(scope: TenantScope, ws: Workspace, slug: str) -> Project:
    project = repository.get_project_by_slug(scope.session, scope.tenant_id, ws.id, slug)
    if project is None:
        raise NotFound("project not found")
    return project


def _project_ref(scope: TenantScope, ws: Workspace, project: Project) -> ResourceRef:
    return ResourceRef(scope.tenant_id, ws.id, project.id)


def _readable_project(scope: TenantScope, ws_slug: str, slug: str) -> tuple[Workspace, Project]:
    ws = _get_workspace_row(scope, ws_slug)
    project = _get_project_row(scope, ws, slug)
    if not scope.pdp.authorize("project:read", _project_ref(scope, ws, project)).allowed:
        raise NotFound("project not found")
    return ws, project


def list_projects(
    scope: TenantScope, ws_slug: str, *, include_archived: bool, limit: int, cursor: str | None
) -> Page[ProjectOut]:
    ws = _get_workspace_row(scope, ws_slug)
    f = scope.pdp.visible_scopes("project:read")
    covers_ws = f.covers_workspace(ws.id)
    if not covers_ws and not f.project_ids:
        raise NotFound("workspace not found")
    rows = repository.list_projects(
        scope.session,
        tenant_id=scope.tenant_id,
        workspace_id=ws.id,
        visible_workspace_ids=None if f.tenant_wide else f.workspace_ids,
        visible_project_ids=f.project_ids,
        include_archived=include_archived,
        after=decode_cursor(cursor),
        limit=limit + 1,
    )
    if (
        not rows
        and not covers_ws
        and not scope.pdp.authorize("workspace:read", _ws_ref(scope, ws)).allowed
        and ws.id not in scope.pdp.ancestor_workspace_ids()
    ):
        raise NotFound("workspace not found")
    page, more = paginate(rows, limit)
    return Page[ProjectOut](
        items=[_project_out(p, ws.slug) for p in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def list_readable_projects(
    scope: TenantScope, *, limit: int, cursor: str | None
) -> Page[ProjectOut]:
    """The active projects of every workspace the caller holds `project:read` on (the key's
    resource scope applies); no `workspace:read` is needed (the MCP `list_projects` tool)."""
    f = scope.pdp.visible_scopes("project:read")
    if not f.tenant_wide and not f.workspace_ids and not f.project_ids:
        return Page[ProjectOut](items=[], next_cursor=None)
    rows = repository.list_projects(
        scope.session,
        tenant_id=scope.tenant_id,
        workspace_id=None,
        visible_workspace_ids=None if f.tenant_wide else f.workspace_ids,
        visible_project_ids=f.project_ids,
        include_archived=False,
        after=decode_cursor(cursor),
        limit=limit + 1,
    )
    page, more = paginate(rows, limit)
    slugs, _ = scope_slug_map(scope, [p.workspace_id for p in page], [])
    return Page[ProjectOut](
        items=[_project_out(p, slugs.get(p.workspace_id, "")) for p in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def get_project(scope: TenantScope, ws_slug: str, slug: str) -> ProjectOut:
    ws, project = _readable_project(scope, ws_slug, slug)
    return _project_out(project, ws.slug)


def create_project(scope: TenantScope, ws_slug: str, body: ProjectCreate) -> ProjectOut:
    ws = _get_workspace_row(scope, ws_slug)
    _require_hiding(scope, "project:create", _ws_ref(scope, ws), "workspace:read", ws)
    _assert_tenant_active(scope)
    _assert_workspace_active(ws)
    if repository.get_project_by_slug(scope.session, scope.tenant_id, ws.id, body.slug):
        raise Conflict("project slug is already taken in this workspace")
    project_id = uuid7()
    repository.insert_project(
        scope.session,
        {
            "id": project_id,
            "tenant_id": scope.tenant_id,
            "workspace_id": ws.id,
            "slug": body.slug,
            "name": body.name,
            "description": body.description,
            "status": "active",
            "settings": {},
            "created_at": utcnow(),
            "created_by": scope.principal.user_id,
        },
    )
    audit.record(
        scope.session,
        scope.ctx,
        action="tenancy.project.created",
        tenant_id=scope.tenant_id,
        target_type="project",
        target_id=project_id,
        target_label=f"project:{ws.slug}/{body.slug}",
        workspace_id=ws.id,
        project_id=project_id,
        details={"slug": body.slug, "workspace": ws.slug},
    )
    scope.session.flush()
    project = repository.get_project_by_slug(scope.session, scope.tenant_id, ws.id, body.slug)
    if project is None:  # pragma: no cover
        raise NotFound("project not found")
    out = _project_out(project, ws.slug)
    _commit(scope.session, "project")
    return out


def update_project(scope: TenantScope, ws_slug: str, slug: str, body: ProjectPatch) -> ProjectOut:
    ws = _get_workspace_row(scope, ws_slug)
    project = _get_project_row(scope, ws, slug)
    scope.pdp.require(
        "project:manage", _project_ref(scope, ws, project), hide_unless="project:read"
    )
    _assert_tenant_active(scope)
    _assert_workspace_active(ws)
    values = _patch_values(body, project.status)
    old_status = project.status  # the ORM row is refreshed by the UPDATE below
    updated = repository.update_project(
        scope.session, scope.tenant_id, project.id, body.row_version, values
    )
    if updated is None:
        raise Conflict("row_version mismatch: the project was modified; reload and retry")
    audit.record(
        scope.session,
        scope.ctx,
        action=_lifecycle("project", old_status, values),
        tenant_id=scope.tenant_id,
        target_type="project",
        target_id=project.id,
        target_label=f"project:{ws.slug}/{project.slug}",
        workspace_id=ws.id,
        project_id=project.id,
        details={"changed": sorted(values)},
    )
    out = _project_out(updated, ws.slug)
    _commit(scope.session, "project")
    return out


# ------------------------------------------------------------------ dashboard
def dashboard(
    scope: TenantScope, *, workspace_slug: str | None, project_slug: str | None
) -> DashboardOut:
    ws: Workspace | None = None
    project: Project | None = None
    if project_slug and not workspace_slug:
        raise ValidationFailed("`project` requires `workspace`")
    if workspace_slug:
        ws = _readable_workspace(scope, workspace_slug)
    if project_slug and ws is not None:
        _, project = _readable_project(scope, ws.slug, project_slug)

    ws_filter = scope.pdp.visible_scopes("workspace:read")
    proj_filter = scope.pdp.visible_scopes("project:read")
    ws_only = (
        None
        if ws_filter.tenant_wide
        else ws_filter.workspace_ids | scope.pdp.ancestor_workspace_ids()
    )
    proj_ws_only = None if proj_filter.tenant_wide else proj_filter.workspace_ids

    if project is not None:
        workspaces, projects = 1, 1
    elif ws is not None:
        workspaces = 1
        projects = repository.count_projects(
            scope.session,
            tenant_id=scope.tenant_id,
            workspace_id=ws.id,
            visible_workspace_ids=proj_ws_only,
            visible_project_ids=proj_filter.project_ids,
        )
    else:
        workspaces = repository.count_workspaces(scope.session, scope.tenant_id, ws_only)
        projects = repository.count_projects(
            scope.session,
            tenant_id=scope.tenant_id,
            workspace_id=None,
            visible_workspace_ids=proj_ws_only,
            visible_project_ids=proj_filter.project_ids,
        )
    can_read_members = scope.pdp.authorize("member:read", scope.tenant_ref).allowed
    members = identity.count_members(scope.session, scope.tenant_id) if can_read_members else None

    is_admin = scope.pdp.authorize("tenant:admin", scope.tenant_ref).allowed
    visible_ws_ids = (
        [] if is_admin else repository.workspace_ids(scope.session, scope.tenant_id, ws_only)
    )
    activity = audit.recent_activity(
        scope.session,
        tenant_id=scope.tenant_id,
        tenant_wide=is_admin,
        visible_workspace_ids=visible_ws_ids,
        workspace_id=ws.id if ws else None,
        project_id=project.id if project else None,
    )
    return DashboardOut(
        scope=DashboardScopeOut(
            org=scope.tenant_slug,
            workspace=ws.slug if ws else None,
            project=project.slug if project else None,
        ),
        counts=DashboardCountsOut(workspaces=workspaces, projects=projects, members=members),
        recent_activity=[
            ActivityOut(at=a.at, actor=a.actor, action=a.action, target=a.target) for a in activity
        ],
    )


# ------------------------------------------------------------------ settings
def effective_settings(
    scope: TenantScope, *, workspace_slug: str | None = None, project_slug: str | None = None
) -> dict[str, Any]:
    """Layered config for a scope (FR-TEN-008): installation, tenant, workspace, project."""
    tenant = repository.get_tenant(scope.session, scope.tenant_id)
    ws = _readable_workspace(scope, workspace_slug) if workspace_slug else None
    project = None
    if project_slug and ws is not None:
        _, project = _readable_project(scope, ws.slug, project_slug)
    return resolve_config(
        repository.get_installation_settings(scope.session),
        tenant.settings if tenant else None,
        ws.settings if ws else None,
        project.settings if project else None,
    )


def tenant_settings(session: Session, tenant_id: UUID) -> dict[str, Any]:
    """The installation and tenant layers of a client's config, read in the session's own client
    context: `effective_settings` of a scope with no workspace or project, for callers that hold
    no `TenantScope` (the runtime's terminal path)."""
    tenant = repository.get_tenant(session, tenant_id)
    return resolve_config(
        repository.get_installation_settings(session), tenant.settings if tenant else None
    )


# ------------------------------------------------------------------ platform status
def _worker_status(session: Session, settings: Settings) -> WorkerStatusOut:
    """Worker health from the `workers` heartbeat rows: healthy = a worker beat within three lease
    periods (at least 15 seconds); stale = workers exist but none beat recently; not_running =
    none ever registered (SETH 3ba0e06, J m2)."""
    count, last = repository.worker_heartbeats(session, max(settings.worker_lease_seconds * 3, 15))
    status = "healthy" if count > 0 else ("stale" if last is not None else "not_running")
    return WorkerStatusOut(status=status, count=count, last_heartbeat_at=last)


def platform_status(
    session: Session, settings: Settings, principal: Principal
) -> PlatformStatusOut:
    """Version, environment, migrations and worker health of the installation. Platform
    administrators only (SEC Minor-2: SETH answered any signed-in person, B-2401); reads no client
    row and switches no context."""
    _require_platform_admin(session, principal)
    state = migration_state(session)
    out = PlatformStatusOut(
        version=__version__,
        environment=settings.env,
        database=DatabaseStatusOut(status=state.status, revision=state.revision, head=state.head),
        auth=AuthStatusOut(
            local_enabled=settings.auth_local_enabled,
            oidc_providers=repository.installation_idp_count(session),
        ),
        worker=_worker_status(session, settings),
    )
    session.rollback()
    return out


# ------------------------------------------------------------------ scope helpers (admin layer)
def resolve_scope_ref(
    scope: TenantScope, scope_type: str, scope_id: UUID, resource_type: str | None = None
) -> ResourceRef:
    """ResourceRef for a (type, id) scope of this tenant; NotFound if it is not in this tenant
    (cross-tenant ids give the same answer as nonexistent ones)."""
    if scope_type == "tenant":
        if scope_id != scope.tenant_id:
            raise NotFound("scope not found")
        return scope.tenant_ref
    if scope_type == "workspace":
        ws = repository.get_workspace(scope.session, scope.tenant_id, scope_id)
        if ws is None:
            raise NotFound("scope not found")
        return ResourceRef(scope.tenant_id, workspace_id=ws.id)
    if scope_type == "project":
        project = repository.get_project_by_id(scope.session, scope.tenant_id, scope_id)
        if project is None:
            raise NotFound("scope not found")
        return ResourceRef(scope.tenant_id, project.workspace_id, project.id)
    if scope_type == "resource":
        # Resource ancestry is unknown until the owning module exists; guards use tenant level.
        return ResourceRef(scope.tenant_id, resource_type=resource_type, resource_id=scope_id)
    raise NotFound("scope not found")


def scope_ref_from_slugs(
    scope: TenantScope, workspace_slug: str | None, project_slug: str | None
) -> ResourceRef:
    """Resolve `?workspace=&project=` slugs to a ResourceRef the caller may see."""
    if project_slug and not workspace_slug:
        raise ValidationFailed("`project` requires `workspace`")
    if not workspace_slug:
        return scope.tenant_ref
    ws = _readable_workspace(scope, workspace_slug)
    if not project_slug:
        return _ws_ref(scope, ws)
    ws, project = _readable_project(scope, ws.slug, project_slug)
    return _project_ref(scope, ws, project)


def scope_slug_map(
    scope: TenantScope, workspace_ids: Iterable[UUID], project_ids: Iterable[UUID]
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """({workspace id: slug}, {project id: slug}) for ids of this tenant (output mapping only;
    visibility was decided by the caller)."""
    workspaces: dict[UUID, str] = {}
    for wid in set(workspace_ids):
        ws = repository.get_workspace(scope.session, scope.tenant_id, wid)
        if ws is not None:
            workspaces[wid] = ws.slug
    projects: dict[UUID, str] = {}
    for pid in set(project_ids):
        project = repository.get_project_by_id(scope.session, scope.tenant_id, pid)
        if project is not None:
            projects[pid] = project.slug
    return workspaces, projects


def tenant_header(session: Session, tenant_id: UUID) -> tuple[str, str, str] | None:
    """(slug, name, status) of a tenant under the current RLS context."""
    tenant = repository.get_tenant(session, tenant_id)
    return (tenant.slug, tenant.name, tenant.status) if tenant else None


def tenant_id_by_slug(session: Session, slug: str) -> UUID | None:
    """Owner/maintenance lookup (relies on BYPASSRLS): tenant id for a slug."""
    tenant = repository.get_tenant_by_slug(session, slug)
    return tenant.id if tenant else None


# ------------------------------------------------------------------ who may grant (design 5.3)
GROUP_BINDING_REFUSED = "the group binding could not be created"


def add_platform_admin_to_client(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    tenant_id: UUID,
    *,
    role_key: str = "tenant_administrator",
) -> None:
    """A platform administrator adds themselves to a client: an active membership (if they have
    none) and a direct binding of `role_key` at organization scope. Always audited in that
    client's own audit log as `tenancy.platform_admin.self_added`, which the client's owner reads
    (B-1012). Any refusal is one generic `NotFound` (an unknown client is not told apart)."""
    _require_platform_admin(session, principal)
    if role_key in authz.PLATFORM_ONLY_ROLE_KEYS:
        raise ValidationFailed(f"unknown role: {role_key}")
    try:
        with db.platform_admin_context(session):
            db.set_tenant(session, tenant_id)
            if repository.get_tenant(session, tenant_id) is None:
                raise NotFound("organization not found")
            if not identity.is_member(session, tenant_id, principal.user_id):
                identity.add_member(session, tenant_id=tenant_id, user_id=principal.user_id)
            authz.grant_role(
                session,
                tenant_id=tenant_id,
                user_id=principal.user_id,
                role_key=role_key,
                scope_type="tenant",
                scope_id=tenant_id,
                created_by=principal.user_id,
            )
            _record_self_add(
                session,
                ctx,
                principal,
                tenant_id=tenant_id,
                details={"role": role_key, "by_platform_administrator": True},
            )
            session.flush()
        _commit(session, "membership")
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    db.set_tenant(session, None)


def _record_self_add(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    *,
    tenant_id: UUID,
    details: dict[str, Any],  # Any: audit details document
    administrator_id: UUID | None = None,
) -> None:
    """`tenancy.platform_admin.self_added` in that client's own audit log, which its owner reads
    (B-1012); never in another client. The target is `administrator_id` (default: the acting
    principal); another administrator is named by id only (B-1407)."""
    target = principal.user_id if administrator_id is None else administrator_id
    audit.record(
        session,
        ctx,
        action="tenancy.platform_admin.self_added",
        tenant_id=tenant_id,
        actor_id=principal.user_id,
        target_type="user",
        target_id=target,
        target_label=f"user:{principal.email}" if target == principal.user_id else None,
        details=details,
    )


def _record_group_self_adds(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    *,
    tenant_id: UUID,
    group_id: UUID,
    details: dict[str, Any],  # Any: audit details document
    only: Iterable[UUID] | None = None,
    skip: Iterable[UUID] = (),
) -> list[UUID]:
    """A self-add in `tenant_id` for each platform administrator in `group_id` (admin isolation
    review 1 finding 1, B-1407): a group binding taking effect, or an administrator joining a
    group bound there, gives them the client. The administrators are read through
    `shifde_bound_group_admin_ids` (only for a group bound in the current client); the database's
    client must be `tenant_id` (B-703), else nothing is written and `Forbidden` is raised.
    `only` limits the records to those ids (the people just added), `skip` leaves out ids
    already recorded by the caller. Returns the ids recorded."""
    if repository.current_tenant_id(session) != tenant_id:
        raise Forbidden(GROUP_BINDING_REFUSED)
    wanted = None if only is None else set(only)
    left_out = set(skip)
    recorded: list[UUID] = []
    for admin_id in repository.bound_group_admin_ids(session, group_id):
        if admin_id in left_out or (wanted is not None and admin_id not in wanted):
            continue
        _record_self_add(
            session,
            ctx,
            principal,
            tenant_id=tenant_id,
            details={**details, "through_group_id": str(group_id)},
            administrator_id=admin_id,
        )
        recorded.append(admin_id)
    return recorded


def _insert_group_binding(
    session: Session,
    *,
    tenant_id: UUID,
    group_id: UUID,
    role_id: UUID,
    scope_type: str,
    scope_id: UUID,
    expires_at: datetime | None,
    created_by: UUID,
) -> UUID:
    if authz.is_platform_only_role(session, role_id):
        raise Forbidden(GROUP_BINDING_REFUSED)
    binding_id = uuid7()
    try:
        with session.begin_nested():
            authz.insert_binding(
                session,
                {
                    "id": binding_id,
                    "tenant_id": tenant_id,
                    "subject_type": "group",
                    "subject_id": group_id,
                    "role_id": role_id,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "expires_at": expires_at,
                    "created_by": created_by,
                    "created_at": utcnow(),
                },
            )
    except (IntegrityError, DBAPIError) as exc:
        raise Forbidden(GROUP_BINDING_REFUSED) from exc
    return binding_id


def _audit_group_binding(
    session: Session,
    ctx: RequestContext,
    *,
    tenant_id: UUID,
    binding_id: UUID,
    group_id: UUID,
    role_id: UUID,
    scope_type: str,
    scope_id: UUID,
    by_platform_administrator: bool,
) -> None:
    """`authz.group.bound` in the client the group was bound in, and only there: the group's id
    and name as that client sees them (B-930), never another client where it is bound."""
    names = {g.id: g.name for g in authz.bound_groups(session)}
    audit.record(
        session,
        ctx,
        action="authz.group.bound",
        tenant_id=tenant_id,
        target_type="role_binding",
        target_id=binding_id,
        target_label=f"group:{names.get(group_id, group_id)}",
        details={
            "group_id": str(group_id),
            "group_name": names.get(group_id),
            "role_id": str(role_id),
            "scope_type": scope_type,
            "scope_id": str(scope_id),
            "by_platform_administrator": by_platform_administrator,
        },
    )


def bind_group(
    scope: TenantScope,
    *,
    group_id: UUID,
    role_id: UUID,
    scope_type: str,
    scope_id: UUID,
    expires_at: datetime | None = None,
) -> UUID:
    """The client owner binds a platform group to a role in their own client (design 5.3,
    B-1010). The owner holds a live direct `tenant_administrator` binding at organization scope
    with an active membership; `role:manage` alone is not enough, and nobody binds into another
    client. Every refusal is one generic `Forbidden`; the database checks the same rule
    (`check_role_binding` -> `shifde_may_bind_group`)."""
    session, principal = scope.session, scope.principal
    if (
        principal.principal_type != "user"
        or principal.credential != "session"
        or principal.tenant_id not in (None, scope.tenant_id)
        or db.get_context(session).tenant_id != scope.tenant_id
        or not repository.may_bind_group(session, scope.tenant_id, group_id)
    ):
        raise Forbidden(GROUP_BINDING_REFUSED)
    resolve_scope_ref(scope, scope_type, scope_id)
    binding_id = _insert_group_binding(
        session,
        tenant_id=scope.tenant_id,
        group_id=group_id,
        role_id=role_id,
        scope_type=scope_type,
        scope_id=scope_id,
        expires_at=expires_at,
        created_by=principal.user_id,
    )
    # An owner who is a platform administrator (an owner by a self-add) acts as one: the
    # binding is shown as theirs and is a self-add in this client (review 2 item 3, B-1017).
    by_admin = repository.actor_is_platform_admin(session, principal.user_id)
    _audit_group_binding(
        session,
        scope.ctx,
        tenant_id=scope.tenant_id,
        binding_id=binding_id,
        group_id=group_id,
        role_id=role_id,
        scope_type=scope_type,
        scope_id=scope_id,
        by_platform_administrator=by_admin,
    )
    if by_admin:
        _record_self_add(
            session,
            scope.ctx,
            principal,
            tenant_id=scope.tenant_id,
            details=_group_self_add_details(
                binding_id, group_id, role_id, scope_type, scope_id, by_platform_administrator=False
            ),
        )
    # every platform administrator in the group reaches this client through the binding (B-1407)
    _record_group_self_adds(
        session,
        scope.ctx,
        principal,
        tenant_id=scope.tenant_id,
        group_id=group_id,
        details={
            **_group_self_add_details(
                binding_id, group_id, role_id, scope_type, scope_id, by_platform_administrator=False
            ),
            "group_membership": "member",
        },
        skip=(principal.user_id,) if by_admin else (),
    )
    _commit(session, "role binding")
    return binding_id


def _group_self_add_details(
    binding_id: UUID,
    group_id: UUID,
    role_id: UUID,
    scope_type: str,
    scope_id: UUID,
    *,
    by_platform_administrator: bool,
) -> dict[str, Any]:  # Any: audit details document
    return {
        "through_group_id": str(group_id),
        "binding_id": str(binding_id),
        "role_id": str(role_id),
        "scope_type": scope_type,
        "scope_id": str(scope_id),
        "by_platform_administrator": by_platform_administrator,
    }


def bind_group_as_platform_admin(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    *,
    tenant_id: UUID,
    group_id: UUID,
    role_id: UUID,
    scope_type: str,
    scope_id: UUID,
    expires_at: datetime | None = None,
) -> UUID:
    """The platform administrator binds a platform group to a role in a client; audited in
    that client like an owner's binding (B-1012). When the administrator is a member of the
    group (whatever its status), that is also a self-add in the client (review 2 item 3,
    B-1017)."""
    _require_platform_admin(session, principal)
    try:
        with db.platform_admin_context(session):
            db.set_tenant(session, tenant_id)
            # re-checked in the writing transaction (admin review 3 item 3, B-1648): an archived
            # client, a disabled group or an inactive role (or one not of this client) is the
            # one generic refusal, whatever the caller's earlier read said
            tenant = repository.get_tenant(session, tenant_id)
            if tenant is None or tenant.status != "active":
                raise Forbidden(GROUP_BINDING_REFUSED)
            group = authz.get_group(session, group_id)
            if group is None or group.status != "active":
                raise Forbidden(GROUP_BINDING_REFUSED)
            role = authz.get_role(session, tenant_id, role_id)
            if role is None or role.status != "active":
                raise Forbidden(GROUP_BINDING_REFUSED)
            binding_id = _insert_group_binding(
                session,
                tenant_id=tenant_id,
                group_id=group_id,
                role_id=role_id,
                scope_type=scope_type,
                scope_id=scope_id,
                expires_at=expires_at,
                created_by=principal.user_id,
            )
            _audit_group_binding(
                session,
                ctx,
                tenant_id=tenant_id,
                binding_id=binding_id,
                group_id=group_id,
                role_id=role_id,
                scope_type=scope_type,
                scope_id=scope_id,
                by_platform_administrator=True,
            )
            # each administrator in the group, the caller included, reaches the client through
            # the binding (review 2 item 3, B-1017; admin isolation review 1 finding 1, B-1407)
            _record_group_self_adds(
                session,
                ctx,
                principal,
                tenant_id=tenant_id,
                group_id=group_id,
                details={
                    **_group_self_add_details(
                        binding_id,
                        group_id,
                        role_id,
                        scope_type,
                        scope_id,
                        by_platform_administrator=True,
                    ),
                    "group_membership": "member",
                },
            )
            session.flush()
        _commit(session, "role binding")
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    db.set_tenant(session, None)
    return binding_id


# ------------------------------------------------------------------ admin layer (platform groups)
def require_platform_admin(session: Session, principal: Principal) -> None:
    """The platform-administrator check of this module, for admin's platform routes: a person on
    a session not pinned to a client whose PDP grants `platform:admin` (B-1011)."""
    _require_platform_admin(session, principal)


def record_group_self_adds(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    *,
    tenant_id: UUID,
    group_id: UUID,
    added: Iterable[UUID],
    details: dict[str, Any],  # Any: audit details document
) -> list[UUID]:
    """`tenancy.platform_admin.self_added` in `tenant_id` only (B-1012) for each platform
    administrator among `added` who is now in `group_id`, bound there (B-1407); the caller has
    set that client's context. Admin records platform administrators joining a group."""
    return _record_group_self_adds(
        session, ctx, principal, tenant_id=tenant_id, group_id=group_id, details=details, only=added
    )


def clients_binding_group(session: Session, group_id: UUID) -> list[UUID]:
    """Ids of the clients where `group_id` holds a live role binding, for the platform
    administrator's group audit (admin part 1, DECISIONS B-1403). Platform-administrator context
    only. Each client is asked in its own context through `shifde_bound_groups` (a client's
    bindings are invisible to the bare flag); the client context is cleared afterwards, also on
    failure."""
    if not db.get_context(session).platform_admin:
        raise Forbidden("this is a platform administrator action")
    found: list[UUID] = []
    after: UUID | None = None
    try:
        while True:
            db.set_tenant(session, None)
            batch = repository.list_tenants(session, include_archived=True, after=after, limit=200)
            for tenant in batch:
                db.set_tenant(session, tenant.id)
                if any(g.id == group_id for g in authz.bound_groups(session)):
                    found.append(tenant.id)
            if len(batch) < 200:
                break
            after = batch[-1].id
    except BaseException:
        # never raise over the original error: on an aborted transaction only the Session's
        # context is cleared, as `platform_admin_context` does (admin review 1 item 4, B-1410)
        db.forget_tenant(session)
        raise
    db.set_tenant(session, None)
    return found
