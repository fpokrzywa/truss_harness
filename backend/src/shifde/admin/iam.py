"""Users, groups, roles, role bindings, effective permissions (client and platform administration).

Ported from SETH 3ba0e06 `admin/iam.py`. Authorization model for every client function: the PDP
decides (`require`), the escalation guards below enforce that nobody grants more than they hold,
every change is audited in the same transaction, and the service owns the commit. Body ids are
always resolved inside the caller's client (RLS + explicit lookups), so another client's role,
group or user is reported exactly like a missing one.

Changes (admin part 1, DECISIONS B-1401..B-1406):

* Groups are platform-level (design 5.3, B-928). A client sees the groups bound in it as id and
  name only (`authz.bound_groups`, B-930); creating, renaming, disabling and filling groups and
  mapping an IdP group name are the platform administrator's (`/platform/groups`), audited at
  installation level and in every client where the group is bound, never naming another client
  (identity review 5 item 3). A platform administrator adding themselves to a group bound in a
  client is a self-add shown to that client's owner (B-1012, B-1017).
* A group is bound in a client only through tenancy's owner path (`tenancy.bind_group`, B-1010);
  naming an owner follows B-1016 (`authz.may_name_owner`); people and memberships are created
  only by the platform administrator (B-910, B-913), so `POST /orgs/{org}/users` is theirs.
* Roles refuse every `PLATFORM_ONLY_ACTIONS` key with 422 before the insert; the
  `platform_administrator` template is hidden from clients and never bound (B-406, B-1010).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime
from typing import Any  # Any: audit details documents
from uuid import UUID

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from shifde.admin import schemas as s
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import BindingRow, ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ScopeArchived,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.identity import service as identity
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

ORG_NOT_FOUND = "organization not found"  # the same answer as `tenancy.dependencies.resolve_org`
BINDING_REFUSED = "binding references are not valid in this organization"


# ------------------------------------------------------------------ helpers
def assert_active(scope: TenantScope) -> None:
    if scope.tenant_status != "active":
        raise ScopeArchived("organization is archived")


def commit(session: Session, what: str) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise Conflict(f"{what} conflicts with an existing resource") from exc


def record(
    scope: TenantScope,
    action: str,
    *,
    target_type: str,
    target_id: UUID | str,
    label: str | None = None,
    details: dict[str, object] | None = None,
    workspace_id: UUID | None = None,
    project_id: UUID | None = None,
) -> None:
    audit.record(
        scope.session,
        scope.ctx,
        action=action,
        tenant_id=scope.tenant_id,
        target_type=target_type,
        target_id=target_id,
        target_label=label,
        details=details,
        workspace_id=workspace_id,
        project_id=project_id,
    )


def guard_grant(scope: TenantScope, permissions: Collection[str], ref: ResourceRef) -> None:
    """Escalation guard: the actor must itself hold every permission it hands out at `ref`."""
    missing = scope.pdp.missing_permissions(frozenset(permissions), ref)
    if missing:
        raise Forbidden(f"cannot grant permissions you do not hold: {', '.join(sorted(missing))}")


def hold_ref(scope: TenantScope, ref: ResourceRef) -> ResourceRef:
    """Resource-level scopes have unknown ancestry, so guards evaluate them at tenant level."""
    return scope.tenant_ref if ref.resource_id is not None else ref


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def _validate_permission_keys(session: Session, keys: list[str]) -> list[str]:
    """Known, non-platform-only permission keys (carry_forward row 79): a platform-only key is
    a 422 here, before the `authz_0002` CHECK could turn it into a 500."""
    unique = list(dict.fromkeys(keys))
    platform_only = sorted(set(unique) & authz.PLATFORM_ONLY_ACTIONS)
    if platform_only:
        raise ValidationFailed(
            f"platform-only permissions cannot be put in a role: {', '.join(platform_only)}"
        )
    known = authz.existing_permission_keys(session, unique)
    unknown = sorted(set(unique) - known)
    if unknown:
        raise ValidationFailed(f"unknown permissions: {', '.join(unknown)}")
    return unique


def _is_hidden_role(role: authz.Role) -> bool:
    """System templates never bound in a client (B-1010) are not shown to one either."""
    return role.tenant_id is None and role.key in authz.PLATFORM_ONLY_ROLE_KEYS


# ------------------------------------------------------------------ permissions catalog
def list_permissions(session: Session) -> Page[s.PermissionOut]:
    rows = authz.list_permissions(session)
    return Page[s.PermissionOut](
        items=[
            s.PermissionOut(
                key=r.key,
                resource_type=r.resource_type,
                action=r.action,
                description=r.description,
                is_privileged=r.is_privileged,
            )
            for r in rows
        ],
        next_cursor=None,
    )


# ------------------------------------------------------------------ roles
def _role_out(role: authz.Role, perms: list[str]) -> s.RoleOut:
    return s.RoleOut(
        id=role.id,
        key=role.key,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        status=role.status,
        permissions=perms,
        row_version=role.row_version,
    )


def list_roles(scope: TenantScope, *, limit: int, cursor: str | None) -> Page[s.RoleOut]:
    scope.pdp.require("role:read", scope.tenant_ref)
    rows = authz.list_roles(
        scope.session, tenant_id=scope.tenant_id, after=decode_cursor(cursor), limit=limit + 1
    )
    page, more = paginate(rows, limit)
    shown = [r for r in page if not _is_hidden_role(r)]
    perms = authz.permissions_by_role(scope.session, [r.id for r in shown])
    return Page[s.RoleOut](
        items=[_role_out(r, perms[r.id]) for r in shown],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def _get_role(scope: TenantScope, role_id: UUID) -> authz.Role:
    role = authz.get_role(scope.session, scope.tenant_id, role_id)
    if role is None or _is_hidden_role(role):
        raise NotFound("role not found")
    return role


def get_role(scope: TenantScope, role_id: UUID) -> s.RoleOut:
    scope.pdp.require("role:read", scope.tenant_ref)
    role = _get_role(scope, role_id)
    return _role_out(role, authz.permissions_by_role(scope.session, [role.id])[role.id])


def create_role(scope: TenantScope, body: s.RoleCreate) -> s.RoleOut:
    scope.pdp.require("role:manage", scope.tenant_ref)
    assert_active(scope)
    perms = _validate_permission_keys(scope.session, body.permissions)
    guard_grant(scope, perms, scope.tenant_ref)
    if authz.role_key_exists(scope.session, scope.tenant_id, body.key):
        raise Conflict("a role with this key already exists")
    role_id = uuid7()
    authz.insert_role(
        scope.session,
        {
            "id": role_id,
            "tenant_id": scope.tenant_id,
            "key": body.key,
            "name": body.name,
            "description": body.description,
            "is_system": False,
            "status": "active",
            "created_at": utcnow(),
        },
    )
    authz.insert_role_permissions(scope.session, role_id, scope.tenant_id, perms)
    record(
        scope,
        "authz.role.created",
        target_type="role",
        target_id=role_id,
        label=f"role:{body.key}",
        details={"permissions": sorted(perms)},
    )
    commit(scope.session, "role")
    role = _get_role(scope, role_id)
    return _role_out(role, sorted(perms))


def update_role(scope: TenantScope, role_id: UUID, body: s.RolePatch) -> s.RoleOut:
    scope.pdp.require("role:manage", scope.tenant_ref)
    assert_active(scope)
    role = _get_role(scope, role_id)
    if role.is_system or role.tenant_id is None:
        raise Forbidden("system role templates are read-only")
    sent = body.model_fields_set - {"row_version"}
    if not sent:
        raise ValidationFailed("no updatable fields supplied")
    current = set(authz.permissions_by_role(scope.session, [role.id])[role.id])
    added: set[str] = set()
    removed: set[str] = set()
    if body.permissions is not None:
        wanted = set(_validate_permission_keys(scope.session, body.permissions))
        added, removed = wanted - current, current - wanted
        guard_grant(scope, added, scope.tenant_ref)
    values: dict[str, object] = {}
    if "name" in sent and body.name is not None:
        values["name"] = body.name
    if "description" in sent:
        values["description"] = body.description
    if "status" in sent and body.status is not None:
        values["status"] = body.status
    if body.status == "active" and role.status != "active":
        # re-enabling revives every binding of the role: guard ALL of its permissions
        guard_grant(scope, (current | added) - removed, scope.tenant_ref)
    updated = authz.update_role(scope.session, role.id, body.row_version, values)
    if updated is None:
        raise Conflict("row_version mismatch: the role was modified; reload and retry")
    authz.delete_role_permissions(scope.session, role.id, removed)
    authz.insert_role_permissions(scope.session, role.id, scope.tenant_id, added)
    record(
        scope,
        "authz.role.updated",
        target_type="role",
        target_id=role.id,
        label=f"role:{role.key}",
        details={
            "changed": sorted(sent),
            "permissions_added": sorted(added),
            "permissions_removed": sorted(removed),
        },
    )
    commit(scope.session, "role")
    final = sorted((current | added) - removed)
    return _role_out(updated, final)


# ------------------------------------------------------------------ role bindings
def _scope_out(b: BindingRow) -> s.ScopeOut:
    return s.ScopeOut(type=b.scope_type, id=b.scope_id, resource_type=b.resource_type)


def _binding_out(b: BindingRow, labels: dict[UUID, str] | None = None) -> s.BindingOut:
    return s.BindingOut(
        id=b.id,
        subject_type=b.subject_type,
        subject_id=b.subject_id,
        subject_label=(labels or {}).get(b.subject_id),
        role_id=b.role_id,
        role_name=b.role_name,
        scope=_scope_out(b),
        expires_at=b.expires_at,
        created_by=b.created_by,
        created_at=b.created_at,
    )


def _subject_labels(session: Session, tenant_id: UUID, rows: list[BindingRow]) -> dict[UUID, str]:
    """Members' e-mails, bound groups' names (as this client sees them, B-930) and service
    accounts' names, all read in the current client only."""
    labels: dict[UUID, str] = {}
    users = {b.subject_id for b in rows if b.subject_type == "user"}
    if users:
        for m in identity.list_members_admin(
            session, tenant_id=tenant_id, after=None, limit=10_000
        ):
            if m.user_id in users:
                labels[m.user_id] = m.email
    if any(b.subject_type == "group" for b in rows):
        labels.update({g.id: g.name for g in authz.bound_groups(session)})
    for b in rows:
        if b.subject_type == "service_account" and b.subject_id not in labels:
            sa = identity.get_service_account(session, tenant_id, b.subject_id)
            if sa:
                labels[b.subject_id] = sa.name
    return labels


def list_bindings(
    scope: TenantScope,
    *,
    subject_type: str | None,
    subject_id: UUID | None,
    scope_type: str | None,
    scope_id: UUID | None,
    limit: int,
    cursor: str | None,
) -> Page[s.BindingOut]:
    visible = scope.pdp.visible_scopes("role:read")
    if not visible.tenant_wide and not visible.workspace_ids and not visible.project_ids:
        scope.pdp.require("role:read", scope.tenant_ref)  # records the denial, raises Forbidden
    rows = authz.list_bindings(
        scope.session,
        tenant_id=scope.tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        scope_type=scope_type,
        scope_id=scope_id,
        tenant_wide=visible.tenant_wide,
        workspace_ids=visible.workspace_ids,
        project_ids=visible.project_ids,
        after=decode_cursor(cursor),
        limit=limit + 1,
    )
    page, more = paginate(rows, limit)
    labels = _subject_labels(scope.session, scope.tenant_id, page)
    return Page[s.BindingOut](
        items=[_binding_out(b, labels) for b in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def _bindable_role(session: Session, tenant_id: UUID, role_id: UUID) -> authz.Role:
    """An active role of this client (or a system template) that may be bound in a client."""
    role = authz.get_role(session, tenant_id, role_id)
    if role is None or role.status != "active" or _is_hidden_role(role):
        raise ValidationFailed("role not found")
    perms = authz.permissions_by_role(session, [role.id])[role.id]
    if authz.PLATFORM_ONLY_ACTIONS & set(perms) or authz.is_platform_only_role(session, role.id):
        raise ValidationFailed("platform-only roles cannot be bound inside an organization")
    return role


def _target_ref(scope: TenantScope, scope_in: s.ScopeIn) -> ResourceRef:
    try:
        return tenancy.resolve_scope_ref(scope, scope_in.type, scope_in.id, scope_in.resource_type)
    except NotFound as exc:
        # Missing and foreign scopes answer alike, and only after a tenant-level permission check
        # (so a caller without it learns nothing about which ids exist).
        scope.pdp.require("role:manage", scope.tenant_ref)
        raise ValidationFailed("scope not found in this organization") from exc


def _names_an_owner(role: authz.Role, subject_type: str, scope_type: str) -> bool:
    return (
        role.tenant_id is None
        and role.key == authz.OWNER_ROLE_KEY
        and subject_type == "user"
        and scope_type == "tenant"
    )


def create_binding_checked(
    scope: TenantScope,
    *,
    subject_type: str,
    subject_id: UUID,
    role_id: UUID,
    scope_in: s.ScopeIn,
    expires_at: datetime | None,
) -> UUID:
    """Validate, guard and insert one binding of a person or a service account (no commit)."""
    target_ref = _target_ref(scope, scope_in)
    if scope_in.type == "resource" and not scope_in.resource_type:
        raise ValidationFailed("resource bindings require scope.resource_type")
    guard_ref = hold_ref(scope, target_ref)
    scope.pdp.require("role:manage", guard_ref)
    assert_active(scope)
    role = _bindable_role(scope.session, scope.tenant_id, role_id)
    perms = authz.permissions_by_role(scope.session, [role.id])[role.id]
    guard_grant(scope, perms, guard_ref)
    if expires_at is not None and expires_at <= utcnow():
        raise ValidationFailed("expires_at must be in the future")
    _check_subject(scope, subject_type, subject_id)
    if _names_an_owner(role, subject_type, scope_in.type) and not authz.may_name_owner(
        scope.session, scope.tenant_id
    ):
        # B-1016: only the owner or the platform administrator names an owner (the trigger
        # checks the same rule); `role:manage` alone is not enough.
        raise Forbidden(authz.NAMING_AN_OWNER_REFUSED)
    binding_id = uuid7()
    try:
        with scope.session.begin_nested():
            authz.insert_binding(
                scope.session,
                {
                    "id": binding_id,
                    "tenant_id": scope.tenant_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "role_id": role.id,
                    "scope_type": scope_in.type,
                    "scope_id": scope_in.id,
                    "resource_type": scope_in.resource_type,
                    "expires_at": expires_at,
                    "created_by": scope.principal.principal_id,
                    "created_at": utcnow(),
                },
            )
    except DBAPIError as exc:
        if _sqlstate(exc) == "23505":
            raise Conflict("this role is already bound to the subject at that scope") from exc
        raise ValidationFailed(BINDING_REFUSED) from exc
    record(
        scope,
        "authz.binding.created",
        target_type="role_binding",
        target_id=binding_id,
        label=f"role:{role.key}",
        details={
            "subject_type": subject_type,
            "subject_id": str(subject_id),
            "role_id": str(role.id),
            "scope_type": scope_in.type,
            "scope_id": str(scope_in.id),
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
        workspace_id=target_ref.workspace_id,
        project_id=target_ref.project_id,
    )
    return binding_id


def _check_subject(scope: TenantScope, subject_type: str, subject_id: UUID) -> None:
    found: object | None
    if subject_type == "user":
        found = identity.get_membership(scope.session, scope.tenant_id, subject_id)
    else:
        found = identity.get_service_account(scope.session, scope.tenant_id, subject_id)
    if found is None:
        raise ValidationFailed("subject not found in this organization")


def _bind_group(scope: TenantScope, body: s.BindingCreate) -> UUID:
    """A platform group is bound only through tenancy's owner path (B-1010): the client's owner
    on a session, in their own client; one generic refusal for everyone else. The binding is
    audited there (`authz.group.bound`) and committed by tenancy."""
    if body.scope.type == "resource":
        raise ValidationFailed("a group is bound at organization, workspace or project scope")
    if body.expires_at is not None and body.expires_at <= utcnow():
        raise ValidationFailed("expires_at must be in the future")
    _bindable_role(scope.session, scope.tenant_id, body.role_id)
    try:
        return tenancy.bind_group(
            scope,
            group_id=body.subject_id,
            role_id=body.role_id,
            scope_type=body.scope.type,
            scope_id=body.scope.id,
            expires_at=body.expires_at,
        )
    except NotFound as exc:  # a scope outside this client, after the owner check passed
        raise ValidationFailed("scope not found in this organization") from exc


def create_binding(scope: TenantScope, body: s.BindingCreate) -> s.BindingOut:
    if body.subject_type == "group":
        binding_id = _bind_group(scope, body)
    else:
        binding_id = create_binding_checked(
            scope,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            role_id=body.role_id,
            scope_in=body.scope,
            expires_at=body.expires_at,
        )
        commit(scope.session, "role binding")
    row = authz.get_binding(scope.session, scope.tenant_id, binding_id)
    if row is None:  # committed in this client a moment ago
        raise NotFound("role binding not found")
    return _binding_out(row, _subject_labels(scope.session, scope.tenant_id, [row]))


def delete_binding(scope: TenantScope, binding_id: UUID) -> None:
    row = authz.get_binding(scope.session, scope.tenant_id, binding_id)
    if row is None:
        # Same answer for "missing" and "not yours": tenant-level role:manage is needed to learn
        # that a binding id does not exist.
        scope.pdp.require("role:manage", scope.tenant_ref)
        raise NotFound("role binding not found")
    ref = hold_ref(
        scope, tenancy.resolve_scope_ref(scope, row.scope_type, row.scope_id, row.resource_type)
    )
    scope.pdp.require("role:manage", ref)
    assert_active(scope)
    authz.delete_binding(scope.session, scope.tenant_id, binding_id)
    record(
        scope,
        "authz.binding.deleted",
        target_type="role_binding",
        target_id=binding_id,
        label=f"role:{row.role_name}",
        details={
            "subject_type": row.subject_type,
            "subject_id": str(row.subject_id),
            "role_id": str(row.role_id),
            "scope_type": row.scope_type,
            "scope_id": str(row.scope_id),
        },
    )
    commit(scope.session, "role binding")


# ------------------------------------------------------------------ me / permissions
def my_permissions(
    scope: TenantScope, workspace: str | None, project: str | None
) -> s.MePermissionsOut:
    ref = tenancy.scope_ref_from_slugs(scope, workspace, project)
    return s.MePermissionsOut(permissions=sorted(scope.pdp.effective_permissions(ref)))


# ------------------------------------------------------------------ users
def _user_admin_outs(
    session: Session, tenant_id: UUID, rows: list[identity.AdminMemberRow]
) -> list[s.UserAdminOut]:
    """Members of the current client; a person's groups are the platform groups bound in this
    client that they are in (id and name, B-930), never a group bound only elsewhere."""
    ids = [r.user_id for r in rows]
    bound = {g.id: g.name for g in authz.bound_groups(session)}
    bindings = authz.bindings_of_subject(session, tenant_id, "user", ids)
    by_user: dict[UUID, list[BindingRow]] = {}
    for b in bindings:
        by_user.setdefault(b.subject_id, []).append(b)
    out: list[s.UserAdminOut] = []
    for r in rows:
        groups = authz.platform_group_membership(session, tenant_id=tenant_id, user_id=r.user_id)
        out.append(
            s.UserAdminOut(
                user=s.UserRef(id=r.user_id, email=r.email, display_name=r.display_name),
                status=r.status,
                row_version=r.row_version,
                groups=[s.GroupRef(id=g, name=bound[g]) for g in sorted(groups) if g in bound],
                role_bindings=[_binding_out(b) for b in by_user.get(r.user_id, [])],
            )
        )
    return out


def list_users(scope: TenantScope, *, limit: int, cursor: str | None) -> Page[s.UserAdminOut]:
    scope.pdp.require("member:read", scope.tenant_ref)
    rows = identity.list_members_admin(
        scope.session, tenant_id=scope.tenant_id, after=decode_cursor(cursor), limit=limit + 1
    )
    page, more = paginate(rows, limit)
    return Page[s.UserAdminOut](
        items=_user_admin_outs(scope.session, scope.tenant_id, page),
        next_cursor=encode_cursor(page[-1].membership_id) if more and page else None,
    )


def _one_user(session: Session, tenant_id: UUID, user_id: UUID) -> s.UserAdminOut:
    for r in identity.list_members_admin(session, tenant_id=tenant_id, after=None, limit=10_000):
        if r.user_id == user_id:
            return _user_admin_outs(session, tenant_id, [r])[0]
    raise NotFound("user not found")


def create_user(
    session: Session, ctx: RequestContext, principal: Principal, org: str, body: s.UserCreate
) -> s.UserCreated:
    """The platform administrator creates a person with a membership in client `org`, binds the
    given roles at organization scope and issues an invitation (B-910, B-913: a client owner
    gives staff access by binding platform groups, not by creating people). Audited in that
    client only. The administrator check comes first, so a refusal says nothing about `org`."""
    tenancy.require_platform_admin(session, principal)
    try:
        with db.platform_admin_context(session):
            tenant_id = tenancy.tenant_id_by_slug(session, org)
            if tenant_id is None:
                raise NotFound(ORG_NOT_FOUND)
            db.set_tenant(session, tenant_id)
            header = tenancy.tenant_header(session, tenant_id)
            if header is None:
                raise NotFound(ORG_NOT_FOUND)
            if header[2] != "active":
                raise ScopeArchived("organization is archived")
            user_id = identity.create_member_user(
                session, tenant_id=tenant_id, email=body.email, display_name=body.display_name
            )
            for role_id in dict.fromkeys(body.role_ids):
                _bind_new_member(session, ctx, principal, tenant_id, user_id, role_id)
            token, expires = identity.create_invite_token(session, user_id)
            email = identity.normalize_email(body.email)
            audit.record(
                session,
                ctx,
                action="identity.user.created",
                tenant_id=tenant_id,
                target_type="user",
                target_id=user_id,
                target_label=f"user:{email}",
                details={"email": email, "role_ids": [str(r) for r in body.role_ids]},
            )
            session.flush()
            out = _one_user(session, tenant_id, user_id)
        commit(session, "user")
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    db.set_tenant(session, None)
    return s.UserCreated(user=out, setup_token=token, setup_token_expires_at=expires)


def _bind_new_member(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    tenant_id: UUID,
    user_id: UUID,
    role_id: UUID,
) -> None:
    """A role for a person the platform administrator just added (platform-administrator
    context, current client `tenant_id`); the binding trigger checks the subject and scope."""
    role = _bindable_role(session, tenant_id, role_id)
    binding_id = uuid7()
    try:
        with session.begin_nested():
            authz.insert_binding(
                session,
                {
                    "id": binding_id,
                    "tenant_id": tenant_id,
                    "subject_type": "user",
                    "subject_id": user_id,
                    "role_id": role.id,
                    "scope_type": "tenant",
                    "scope_id": tenant_id,
                    "created_by": principal.user_id,
                    "created_at": utcnow(),
                },
            )
    except DBAPIError as exc:
        raise ValidationFailed(BINDING_REFUSED) from exc
    audit.record(
        session,
        ctx,
        action="authz.binding.created",
        tenant_id=tenant_id,
        target_type="role_binding",
        target_id=binding_id,
        target_label=f"role:{role.key}",
        details={
            "subject_type": "user",
            "subject_id": str(user_id),
            "role_id": str(role.id),
            "scope_type": "tenant",
            "scope_id": str(tenant_id),
            "expires_at": None,
            "by_platform_administrator": True,
        },
    )


def update_user_status(scope: TenantScope, user_id: UUID, body: s.UserPatch) -> s.UserAdminOut:
    scope.pdp.require("member:manage", scope.tenant_ref)
    assert_active(scope)
    membership = identity.get_membership(scope.session, scope.tenant_id, user_id)
    if membership is None:
        raise NotFound("user not found")
    if body.status == "suspended" and user_id == scope.principal.principal_id:
        raise Conflict("you cannot suspend yourself")
    if body.status != membership.status:
        guard_target_privileges(
            scope, user_id, reactivating=body.status == "active", action=f"set status {body.status}"
        )
    _, sessions_revoked, keys_revoked = identity.set_member_status(
        scope.session,
        tenant_id=scope.tenant_id,
        user_id=user_id,
        status=body.status,
        expected_version=body.row_version,
        actor_id=scope.principal.principal_id,
    )
    record(
        scope,
        "identity.user.suspended" if body.status == "suspended" else "identity.user.reactivated",
        target_type="user",
        target_id=user_id,
        details={
            "previous_status": membership.status,
            "sessions_revoked": sessions_revoked,
            "keys_revoked": keys_revoked,
        },
    )
    commit(scope.session, "user")
    return _one_user(scope.session, scope.tenant_id, user_id)


def guard_target_privileges(
    scope: TenantScope, user_id: UUID, *, reactivating: bool, action: str
) -> None:
    """Escalation guard for a member's lifecycle (suspend, reactivate): the actor must hold every
    permission the person holds in this client (directly or through platform groups bound here)
    at each binding scope, so reactivating cannot hand back more power than the actor has.

    A client cannot read platform group membership (B-928), and `shifde_active_group_ids` is
    empty while the membership is not active (B-1018). Suspending reads the person's bound groups
    through it; reactivating counts every group bound in this client, failing closed
    (DECISIONS B-1404)."""
    bindings = list(authz.bindings_of_subject(scope.session, scope.tenant_id, "user", [user_id]))
    if reactivating:
        group_ids = [g.id for g in authz.bound_groups(scope.session)]
    else:
        group_ids = sorted(
            authz.platform_group_membership(
                scope.session, tenant_id=scope.tenant_id, user_id=user_id
            )
        )
    bindings += authz.bindings_of_subject(scope.session, scope.tenant_id, "group", group_ids)
    for b in bindings:
        try:
            ref = hold_ref(
                scope, tenancy.resolve_scope_ref(scope, b.scope_type, b.scope_id, b.resource_type)
            )
        except NotFound:
            continue
        perms = authz.permissions_by_role(scope.session, [b.role_id])[b.role_id]
        missing = scope.pdp.missing_permissions(frozenset(perms), ref)
        if missing:
            raise Forbidden(f"cannot {action}: the subject holds permissions you do not hold")


# ------------------------------------------------------------------ groups, as a client sees them
def _bound_group_out(scope: TenantScope, group: authz.BoundGroup) -> s.BoundGroupOut:
    rows = authz.bindings_of_subject(scope.session, scope.tenant_id, "group", [group.id])
    labels = {group.id: group.name}
    return s.BoundGroupOut(
        id=group.id, name=group.name, role_bindings=[_binding_out(b, labels) for b in rows]
    )


def list_bound_groups(
    scope: TenantScope, *, limit: int, cursor: str | None
) -> Page[s.BoundGroupOut]:
    """The platform groups bound in this client (B-930): id, name and their bindings here; never
    members, and never a group bound only in another client (DECISIONS B-1401)."""
    scope.pdp.require("group:read", scope.tenant_ref)
    after = decode_cursor(cursor)
    rows = sorted(authz.bound_groups(scope.session), key=lambda g: g.id)
    rows = [g for g in rows if after is None or g.id > after][: limit + 1]
    page, more = paginate(rows, limit)
    return Page[s.BoundGroupOut](
        items=[_bound_group_out(scope, g) for g in page],
        next_cursor=encode_cursor(page[-1].id) if more and page else None,
    )


def get_bound_group(scope: TenantScope, group_id: UUID) -> s.BoundGroupOut:
    scope.pdp.require("group:read", scope.tenant_ref)
    for g in authz.bound_groups(scope.session):
        if g.id == group_id:
            return _bound_group_out(scope, g)
    raise NotFound("group not found")


# ------------------------------------------------------------------ platform groups (admin only)
def _group_out(g: authz.Group, count: int) -> s.GroupOut:
    return s.GroupOut(
        id=g.id,
        slug=g.slug,
        name=g.name,
        description=g.description,
        source=g.source,
        idp_config_id=g.idp_config_id,
        external_id=g.external_id,
        status=g.status,
        row_version=g.row_version,
        member_count=count,
        created_at=g.created_at,
        updated_at=g.updated_at,
    )


def _group_detail(session: Session, group: authz.Group) -> s.GroupDetailOut:
    members = authz.group_members(session, group.id)
    base = _group_out(group, len(members))
    return s.GroupDetailOut(
        **base.model_dump(),
        members=[
            s.GroupMemberOut(
                id=m.user_id, email=m.email, display_name=m.display_name, source=m.source
            )
            for m in members
        ],
    )


def _get_platform_group(session: Session, group_id: UUID) -> authz.Group:
    group = authz.get_group(session, group_id)
    if group is None:
        raise NotFound("group not found")
    return group


def list_platform_groups(
    session: Session, principal: Principal, *, limit: int, cursor: str | None
) -> Page[s.GroupOut]:
    tenancy.require_platform_admin(session, principal)
    with db.platform_admin_context(session):
        rows = authz.list_groups(session, after=decode_cursor(cursor), limit=limit + 1)
        page, more = paginate(rows, limit)
        counts = authz.member_counts(session, [g.id for g in page])
        out = Page[s.GroupOut](
            items=[_group_out(g, counts.get(g.id, 0)) for g in page],
            next_cursor=encode_cursor(page[-1].id) if more and page else None,
        )
    session.rollback()
    return out


def get_platform_group(session: Session, principal: Principal, group_id: UUID) -> s.GroupDetailOut:
    tenancy.require_platform_admin(session, principal)
    with db.platform_admin_context(session):
        out = _group_detail(session, _get_platform_group(session, group_id))
    session.rollback()
    return out


def _record_group_change(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    group: authz.Group,
    clients: Iterable[UUID],
    action: str,
    details: dict[str, Any],
    *,
    client_details: dict[str, Any] | None = None,
    joined: Collection[UUID] = (),
) -> None:
    """The change at installation level, then in each client where the group is bound, never
    naming another client (identity review 5 item 3, DECISIONS B-1403); a client's row carries
    `client_details` when given (no person outside it, B-1408). Each platform administrator in
    `joined` reaching a client through the group is a self-add there, shown to its owner
    (B-1012, B-1407)."""
    audit.record(
        session,
        ctx,
        action=action,
        tenant_id=None,
        target_type="group",
        target_id=group.id,
        target_label=f"group:{group.slug}",
        details=details,
    )
    for tenant_id in clients:
        db.set_tenant(session, tenant_id)
        audit.record(
            session,
            ctx,
            action=action,
            tenant_id=tenant_id,
            target_type="group",
            target_id=group.id,
            target_label=f"group:{group.name}",
            details={
                "group_name": group.name,
                **(details if client_details is None else client_details),
            },
        )
        if joined:
            tenancy.record_group_self_adds(
                session,
                ctx,
                principal,
                tenant_id=tenant_id,
                group_id=group.id,
                added=joined,
                details={"group_membership": "added", "by_platform_administrator": True},
            )
    db.set_tenant(session, None)


def _platform_write(session: Session) -> None:
    """Commit a platform-administrator change; leave no client context behind."""
    db.set_tenant(session, None)
    commit(session, "group")


def create_platform_group(
    session: Session, ctx: RequestContext, principal: Principal, body: s.GroupCreate
) -> s.GroupDetailOut:
    tenancy.require_platform_admin(session, principal)
    if (body.idp_config_id is None) != (body.external_id is None):
        raise ValidationFailed("idp_config_id and external_id map an IdP group together")
    group_id = uuid7()
    try:
        with db.platform_admin_context(session):
            try:
                with session.begin_nested():
                    authz.insert_group(
                        session,
                        {
                            "id": group_id,
                            "slug": body.slug,
                            "name": body.name,
                            "description": body.description,
                            "source": "idp" if body.idp_config_id else "local",
                            "idp_config_id": body.idp_config_id,
                            "external_id": body.external_id,
                            "status": "active",
                            "created_at": utcnow(),
                            "created_by": principal.user_id,
                        },
                    )
            except DBAPIError as exc:  # slug or IdP mapping taken, or not an installation IdP
                raise Conflict("the group could not be created") from exc
            group = _get_platform_group(session, group_id)
            _record_group_change(
                session,
                ctx,
                principal,
                group,
                (),
                "identity.group.created",
                {"group_id": str(group_id), "slug": body.slug, "source": group.source},
            )
            session.flush()
            out = _group_detail(session, group)
        _platform_write(session)
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    return out


def update_platform_group(
    session: Session, ctx: RequestContext, principal: Principal, group_id: UUID, body: s.GroupPatch
) -> s.GroupDetailOut:
    """Rename, describe, disable or re-enable a platform group. Disabling ends every grant
    through it in every client at once (`shifde_active_group_ids` counts active groups only)."""
    tenancy.require_platform_admin(session, principal)
    sent = body.model_fields_set - {"row_version"}
    if not sent:
        raise ValidationFailed("no updatable fields supplied")
    values: dict[str, object] = {}
    if "name" in sent and body.name is not None:
        values["name"] = body.name
    if "description" in sent:
        values["description"] = body.description
    if "status" in sent and body.status is not None:
        values["status"] = body.status
    try:
        with db.platform_admin_context(session):
            group = _get_platform_group(session, group_id)
            clients = tenancy.clients_binding_group(session, group.id)  # before a disable
            updated = authz.update_group(session, group.id, body.row_version, values)
            if updated is None:
                raise Conflict("row_version mismatch: the group was modified; reload and retry")
            _record_group_change(
                session,
                ctx,
                principal,
                updated,
                clients,
                "identity.group.updated",
                {"group_id": str(group.id), "changed": sorted(sent), "status": updated.status},
            )
            session.flush()
            out = _group_detail(session, updated)
        _platform_write(session)
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    return out


def replace_platform_group_members(
    session: Session,
    ctx: RequestContext,
    principal: Principal,
    group_id: UUID,
    body: s.GroupMembersPut,
) -> s.GroupDetailOut:
    """Set a platform group's manual members (platform administrator only, B-934). The change is
    audited at installation level and in every client where the group is bound; the
    administrator adding themselves is a self-add in each of those clients (B-1012)."""
    tenancy.require_platform_admin(session, principal)
    try:
        with db.platform_admin_context(session):
            group = _get_platform_group(session, group_id)
            if group.source != "local":
                raise Conflict(
                    "membership of IdP-synced groups is managed by the identity provider"
                )
            wanted = set(body.user_ids)
            for uid in wanted:
                if identity.get_user(session, uid) is None:
                    raise ValidationFailed("one or more users do not exist")
            current = authz.member_user_ids(session, group.id)
            added, removed = wanted - set(current), set(current) - wanted
            clients = tenancy.clients_binding_group(session, group.id)
            updated = authz.update_group(session, group.id, body.row_version, {})
            if updated is None:
                raise Conflict("row_version mismatch: the group was modified; reload and retry")
            authz.remove_group_members(session, group.id, removed)
            authz.add_group_members(session, group.id, added, "manual", utcnow())
            _record_group_change(
                session,
                ctx,
                principal,
                updated,
                clients,
                "identity.group.members_changed",
                {
                    "group_id": str(group.id),
                    "added": sorted(str(u) for u in added),
                    "removed": sorted(str(u) for u in removed),
                },
                # a client is told counts only: the people may belong to other clients (B-1408)
                client_details={
                    "group_id": str(group.id),
                    "added_count": len(added),
                    "removed_count": len(removed),
                },
                joined=added,
            )
            session.flush()
            out = _group_detail(session, updated)
        _platform_write(session)
    except BaseException:
        session.rollback()
        db.set_tenant(session, None)
        raise
    return out
