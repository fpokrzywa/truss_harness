"""SQL for roles, permissions, groups, role bindings and policy decisions (all under RLS)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any  # Any: column value dicts for INSERT/UPDATE
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    column,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    table,
    text,
    update,
)
from sqlalchemy.orm import Session

from shifde.authz.models import (
    Group,
    GroupMember,
    PolicyDecision,
    Role,
    RoleBinding,
    RolePermission,
)
from shifde.core import db
from shifde.core.errors import Forbidden


@dataclass(frozen=True)
class GrantRow:
    binding_id: UUID
    scope_type: str
    scope_id: UUID
    permission_key: str
    resource_type: str | None = None


@dataclass(frozen=True)
class PermissionRow:
    key: str
    resource_type: str
    action: str
    description: str
    is_privileged: bool


# ------------------------------------------------------------------ grants (PDP)
# `shifde.memberships` belongs to identity (a higher layer): named here as a bare table so the
# PDP can require an active membership without importing identity's models. Read under RLS.
_MEMBERSHIPS = table(
    "memberships", column("tenant_id"), column("user_id"), column("status"), schema="shifde"
)


def _subject(
    principal_type: str, principal_id: UUID, group_ids: Collection[UUID]
) -> ColumnElement[bool]:
    """Bindings held by the principal: directly, or (users only) through one of `group_ids`.
    A person's direct bindings count only while their membership in the binding's client is
    active (tenancy review 3 item 1, B-1018, DECISIONS B-1405): the visibility rule of
    `shifde_my_memberships`, so a PDP built without `resolve_org` (admin, runtime) grants a
    suspended member nothing. Group grants are already filtered the same way by
    `shifde_active_group_ids`."""
    subject: ColumnElement[bool] = (RoleBinding.subject_type == principal_type) & (
        RoleBinding.subject_id == principal_id
    )
    if principal_type == "user":
        subject = subject & exists(
            select(literal(1))
            .select_from(_MEMBERSHIPS)
            .where(
                _MEMBERSHIPS.c.tenant_id == RoleBinding.tenant_id,
                _MEMBERSHIPS.c.user_id == principal_id,
                _MEMBERSHIPS.c.status == "active",
            )
        )
    if principal_type == "user" and group_ids:
        subject = or_(
            subject,
            (RoleBinding.subject_type == "group") & RoleBinding.subject_id.in_(list(group_ids)),
        )
    return subject


def load_grants(
    session: Session,
    *,
    tenant_id: UUID,
    principal_type: str,
    principal_id: UUID,
    group_ids: Collection[UUID],
    now: datetime,
) -> list[GrantRow]:
    """Active (non-expired) permissions granted to a principal in a tenant, directly or (for
    users) through the groups in `group_ids`; one row per (binding, permission). Only bindings
    stored in `tenant_id` count, whatever the group: a group with no binding in this tenant
    grants nothing here. Runs under RLS."""
    stmt = (
        select(
            RoleBinding.id,
            RoleBinding.scope_type,
            RoleBinding.scope_id,
            RolePermission.permission_key,
            RoleBinding.resource_type,
        )
        .join(RolePermission, RolePermission.role_id == RoleBinding.role_id)
        .join(Role, Role.id == RoleBinding.role_id)
        .where(
            RoleBinding.tenant_id == tenant_id,
            _subject(principal_type, principal_id, group_ids),
            Role.status == "active",
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > now),
        )
    )
    return [GrantRow(r[0], r[1], r[2], r[3], r[4]) for r in session.execute(stmt)]


def bound_roles(
    session: Session,
    *,
    tenant_id: UUID,
    principal_type: str,
    principal_id: UUID,
    group_ids: Collection[UUID],
    now: datetime,
) -> list[tuple[UUID, str, UUID, str | None]]:
    """(role id, scope type, scope id, resource type) of the principal's active, non-expired
    bindings in `tenant_id`, directly or (users) through the groups in `group_ids`. Runs under
    RLS."""
    stmt = (
        select(
            RoleBinding.role_id,
            RoleBinding.scope_type,
            RoleBinding.scope_id,
            RoleBinding.resource_type,
        )
        .join(Role, Role.id == RoleBinding.role_id)
        .where(
            RoleBinding.tenant_id == tenant_id,
            _subject(principal_type, principal_id, group_ids),
            Role.status == "active",
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > now),
        )
    )
    return [(r[0], r[1], r[2], r[3]) for r in session.execute(stmt)]


def platform_group_ids(session: Session, tenant_id: UUID, user_id: UUID) -> set[UUID]:
    """The person's active platform groups that hold a role binding in `tenant_id`, which must be
    the database's current client (`shifde_active_group_ids`, identity_0005, B-929): empty
    without a client context, for another client, or for a group bound only elsewhere."""
    rows = session.execute(
        text(
            "SELECT g FROM shifde.shifde_active_group_ids(:u) AS g "
            "WHERE shifde.current_tenant_id() = :t"
        ),
        {"u": user_id, "t": tenant_id},
    )
    return {r[0] for r in rows}


def workspace_ids_of_projects(
    session: Session, tenant_id: UUID, project_ids: Collection[UUID]
) -> set[UUID]:
    if not project_ids:
        return set()
    rows = session.execute(
        text(
            "SELECT DISTINCT workspace_id FROM shifde.projects "
            "WHERE tenant_id = :t AND id = ANY(:ids)"
        ),
        {"t": tenant_id, "ids": list(project_ids)},
    )
    return {r[0] for r in rows}


def privileged_keys(session: Session) -> frozenset[str]:
    rows = session.execute(text("SELECT key FROM shifde.permissions WHERE is_privileged"))
    return frozenset(r[0] for r in rows)


# ------------------------------------------------------------------ catalog
def list_permissions(session: Session) -> list[PermissionRow]:
    rows = session.execute(
        text(
            "SELECT key, resource_type, action, description, is_privileged "
            "FROM shifde.permissions ORDER BY key"
        )
    )
    return [PermissionRow(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def existing_permission_keys(session: Session, keys: Collection[str]) -> set[str]:
    if not keys:
        return set()
    rows = session.execute(
        text("SELECT key FROM shifde.permissions WHERE key = ANY(:k)"), {"k": list(keys)}
    )
    return {r[0] for r in rows}


# ------------------------------------------------------------------ roles
def system_role_id(session: Session, key: str) -> UUID | None:
    return session.scalar(select(Role.id).where(Role.tenant_id.is_(None), Role.key == key))


def list_roles(session: Session, *, tenant_id: UUID, after: UUID | None, limit: int) -> list[Role]:
    stmt = select(Role).where(or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id))
    if after is not None:
        stmt = stmt.where(Role.id > after)
    return list(session.scalars(stmt.order_by(Role.id).limit(limit)))


def get_role(session: Session, tenant_id: UUID, role_id: UUID) -> Role | None:
    return session.scalar(
        select(Role).where(
            Role.id == role_id, or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id)
        )
    )


def role_key_exists(session: Session, tenant_id: UUID, key: str) -> bool:
    return bool(
        session.scalar(
            select(func.count())
            .select_from(Role)
            .where(Role.key == key, or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id))
        )
    )


def permissions_by_role(session: Session, role_ids: Sequence[UUID]) -> dict[UUID, list[str]]:
    if not role_ids:
        return {}
    out: dict[UUID, list[str]] = {r: [] for r in role_ids}
    rows = session.execute(
        select(RolePermission.role_id, RolePermission.permission_key)
        .where(RolePermission.role_id.in_(list(role_ids)))
        .order_by(RolePermission.permission_key)
    )
    for role_id, key in rows:
        out[role_id].append(key)
    return out


def insert_role(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(Role).values(**values))


def insert_role_permissions(
    session: Session, role_id: UUID, tenant_id: UUID, keys: Collection[str]
) -> None:
    if keys:
        session.execute(
            insert(RolePermission),
            [{"role_id": role_id, "permission_key": k, "tenant_id": tenant_id} for k in keys],
        )


def delete_role_permissions(session: Session, role_id: UUID, keys: Collection[str]) -> None:
    if keys:
        session.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.permission_key.in_(list(keys))
            )
        )


def update_role(
    session: Session, role_id: UUID, expected_version: int | None, values: dict[str, Any]
) -> Role | None:
    stmt = update(Role).where(Role.id == role_id, Role.is_system.is_(False))
    if expected_version is not None:
        stmt = stmt.where(Role.row_version == expected_version)
    stmt = (
        stmt.values(**values, row_version=Role.row_version + 1)
        .returning(Role)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


# ------------------------------------------------------------------ bindings
def insert_binding(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(RoleBinding).values(**values))


@dataclass(frozen=True)
class BindingRow:
    id: UUID
    subject_type: str
    subject_id: UUID
    role_id: UUID
    role_name: str
    scope_type: str
    scope_id: UUID
    resource_type: str | None
    expires_at: datetime | None
    created_by: UUID | None
    created_at: datetime


def _binding_select() -> Any:  # Any: SQLAlchemy Select (keeps this helper private and tiny)
    return select(
        RoleBinding.id,
        RoleBinding.subject_type,
        RoleBinding.subject_id,
        RoleBinding.role_id,
        Role.name,
        RoleBinding.scope_type,
        RoleBinding.scope_id,
        RoleBinding.resource_type,
        RoleBinding.expires_at,
        RoleBinding.created_by,
        RoleBinding.created_at,
    ).join(Role, Role.id == RoleBinding.role_id)


def list_bindings(
    session: Session,
    *,
    tenant_id: UUID,
    subject_type: str | None,
    subject_id: UUID | None,
    scope_type: str | None,
    scope_id: UUID | None,
    tenant_wide: bool,
    workspace_ids: Collection[UUID],
    project_ids: Collection[UUID],
    after: UUID | None,
    limit: int,
) -> list[BindingRow]:
    stmt = _binding_select().where(RoleBinding.tenant_id == tenant_id)
    if subject_type:
        stmt = stmt.where(RoleBinding.subject_type == subject_type)
    if subject_id:
        stmt = stmt.where(RoleBinding.subject_id == subject_id)
    if scope_type:
        stmt = stmt.where(RoleBinding.scope_type == scope_type)
    if scope_id:
        stmt = stmt.where(RoleBinding.scope_id == scope_id)
    if not tenant_wide:  # a scoped role reader sees only bindings within its scopes
        stmt = stmt.where(
            or_(
                (RoleBinding.scope_type == "workspace")
                & RoleBinding.scope_id.in_(list(workspace_ids)),
                (RoleBinding.scope_type == "project") & RoleBinding.scope_id.in_(list(project_ids)),
            )
        )
    if after is not None:
        stmt = stmt.where(RoleBinding.id > after)
    rows = session.execute(stmt.order_by(RoleBinding.id).limit(limit))
    return [BindingRow(*r) for r in rows]


def get_binding(session: Session, tenant_id: UUID, binding_id: UUID) -> BindingRow | None:
    row = session.execute(
        _binding_select().where(RoleBinding.tenant_id == tenant_id, RoleBinding.id == binding_id)
    ).first()
    return BindingRow(*row) if row else None


def delete_binding(session: Session, tenant_id: UUID, binding_id: UUID) -> bool:
    res = session.execute(
        delete(RoleBinding).where(RoleBinding.tenant_id == tenant_id, RoleBinding.id == binding_id)
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]


def bindings_of_subject(
    session: Session, tenant_id: UUID, subject_type: str, subject_ids: Sequence[UUID]
) -> list[BindingRow]:
    if not subject_ids:
        return []
    rows = session.execute(
        _binding_select()
        .where(
            RoleBinding.tenant_id == tenant_id,
            RoleBinding.subject_type == subject_type,
            RoleBinding.subject_id.in_(list(subject_ids)),
        )
        .order_by(RoleBinding.id)
    )
    return [BindingRow(*r) for r in rows]


def delete_bindings_of_subject(
    session: Session, tenant_id: UUID, subject_type: str, subject_id: UUID
) -> int:
    res = session.execute(
        delete(RoleBinding).where(
            RoleBinding.tenant_id == tenant_id,
            RoleBinding.subject_type == subject_type,
            RoleBinding.subject_id == subject_id,
        )
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ groups
# Groups are platform-level (design 5.3, identity_0005, DECISIONS B-928): one row per SHI-wide
# group, no client key. Writes need the platform-administrator context (RLS, and refused here
# before the database is touched); a client context reads no group row, only `bound_groups`.
def _require_platform_admin(session: Session) -> None:
    if not db.get_context(session).platform_admin:
        raise Forbidden("platform groups are managed by the platform administrator")


def insert_group(session: Session, values: dict[str, Any]) -> None:
    _require_platform_admin(session)
    session.execute(insert(Group).values(**values))


def get_group(session: Session, group_id: UUID) -> Group | None:
    return session.scalar(select(Group).where(Group.id == group_id))


def get_group_by_slug(session: Session, slug: str) -> Group | None:
    return session.scalar(select(Group).where(Group.slug == slug))


def get_idp_group(session: Session, idp_config_id: UUID, external_id: str) -> Group | None:
    """IdP-mapped groups are keyed by (IdP, external name): two IdPs never share a group."""
    return session.scalar(
        select(Group).where(
            Group.source == "idp",
            Group.idp_config_id == idp_config_id,
            Group.external_id == external_id,
        )
    )


def list_groups(session: Session, *, after: UUID | None, limit: int) -> list[Group]:
    stmt = select(Group)
    if after is not None:
        stmt = stmt.where(Group.id > after)
    return list(session.scalars(stmt.order_by(Group.id).limit(limit)))


def update_group(
    session: Session,
    group_id: UUID,
    expected_version: int | None,
    values: dict[str, Any],
) -> Group | None:
    _require_platform_admin(session)
    stmt = update(Group).where(Group.id == group_id)
    if expected_version is not None:
        stmt = stmt.where(Group.row_version == expected_version)
    stmt = (
        stmt.values(**values, row_version=Group.row_version + 1)
        .returning(Group)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


@dataclass(frozen=True)
class BoundGroup:
    """What a client sees of a platform group bound in it: id and name, nothing else."""

    id: UUID
    name: str


def bound_groups(session: Session) -> list[BoundGroup]:
    """Groups holding a role binding in the current client (`shifde_bound_groups`, B-930)."""
    rows = session.execute(text("SELECT id, name FROM shifde.shifde_bound_groups()"))
    return [BoundGroup(r[0], r[1]) for r in rows]


@dataclass(frozen=True)
class MemberRef:
    user_id: UUID
    email: str
    display_name: str
    source: str


def group_members(session: Session, group_id: UUID) -> list[MemberRef]:
    rows = session.execute(
        text(
            "SELECT u.id, u.primary_email, u.display_name, gm.source FROM shifde.group_members gm "
            "JOIN shifde.users u ON u.id = gm.user_id "
            "WHERE gm.group_id = :g ORDER BY u.primary_email"
        ),
        {"g": group_id},
    )
    return [MemberRef(*r) for r in rows]


def member_counts(session: Session, group_ids: Sequence[UUID]) -> dict[UUID, int]:
    if not group_ids:
        return {}
    rows = session.execute(
        select(GroupMember.group_id, func.count())
        .where(GroupMember.group_id.in_(list(group_ids)))
        .group_by(GroupMember.group_id)
    )
    return {r[0]: r[1] for r in rows}


def groups_of_users(session: Session, user_ids: Sequence[UUID]) -> dict[UUID, list[Group]]:
    if not user_ids:
        return {}
    out: dict[UUID, list[Group]] = {}
    rows = session.execute(
        select(GroupMember.user_id, Group)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.user_id.in_(list(user_ids)))
        .order_by(Group.slug)
    )
    for user_id, group in rows:
        out.setdefault(user_id, []).append(group)
    return out


def member_user_ids(session: Session, group_id: UUID) -> dict[UUID, str]:
    """{user_id: source} for a group."""
    rows = session.execute(
        select(GroupMember.user_id, GroupMember.source).where(GroupMember.group_id == group_id)
    )
    return {r[0]: r[1] for r in rows}


def add_group_members(
    session: Session,
    group_id: UUID,
    user_ids: Collection[UUID],
    source: str,
    now: datetime,
) -> None:
    _require_platform_admin(session)
    if user_ids:
        session.execute(
            insert(GroupMember),
            [
                {"group_id": group_id, "user_id": u, "source": source, "added_at": now}
                for u in user_ids
            ],
        )


def remove_group_members(session: Session, group_id: UUID, user_ids: Collection[UUID]) -> None:
    _require_platform_admin(session)
    if user_ids:
        session.execute(
            delete(GroupMember).where(
                GroupMember.group_id == group_id, GroupMember.user_id.in_(list(user_ids))
            )
        )


def idp_synced_group_ids_of_user(session: Session, user_id: UUID, idp_config_id: UUID) -> set[UUID]:
    """Memberships synced from this IdP only (other IdPs' and manual memberships are untouched)."""
    rows = session.execute(
        select(GroupMember.group_id)
        .join(Group, Group.id == GroupMember.group_id)
        .where(
            GroupMember.user_id == user_id,
            GroupMember.source == "idp_sync",
            Group.idp_config_id == idp_config_id,
        )
    )
    return {r[0] for r in rows}


@dataclass(frozen=True)
class IdpGroupChange:
    """One group of the person's IdP sync (`shifde_sync_idp_groups`, identity_0006): `change`
    is `added`, `removed` or `kept`; `bound_tenant_ids` names the clients where an added or
    removed group holds a role binding (empty for `kept`), for their audit (B-1624)."""

    group_id: UUID
    change: str
    bound_tenant_ids: tuple[UUID, ...]


def sync_idp_group_changes(
    session: Session, idp_config_id: UUID, user_id: UUID, names: Sequence[str]
) -> list[IdpGroupChange]:
    """Make the person's `idp_sync` memberships of groups mapped to this installation IdP equal
    `names` (`shifde_sync_idp_groups`, B-932); never creates a group. Raises
    (insufficient_privilege) inside any client context and unless the IdP is an active
    installation IdP with sync on and the person has an identity there (B-936)."""
    rows = session.execute(
        text("SELECT * FROM shifde.shifde_sync_idp_groups(:i, :u, CAST(:n AS text[]))"),
        {"i": idp_config_id, "u": user_id, "n": list(names)},
    ).all()
    return [
        IdpGroupChange(UUID(str(r[0])), str(r[1]), tuple(UUID(str(t)) for t in r[2] or ()))
        for r in rows
    ]


def sync_idp_groups(
    session: Session, idp_config_id: UUID, user_id: UUID, names: Sequence[str]
) -> int:
    """`sync_idp_group_changes`; returns the number of synced memberships."""
    changes = sync_idp_group_changes(session, idp_config_id, user_id, names)
    return sum(1 for c in changes if c.change != "removed")


def bound_group_admin_ids(session: Session, group_id: UUID) -> list[UUID]:
    """Ids of the platform administrators in `group_id` when the group holds a role binding in
    the current client (`shifde_bound_group_admin_ids`, tenancy_0004, B-1407); empty otherwise.
    For IdP sync at sign-in, which identity runs below tenancy (B-1624)."""
    rows = session.execute(
        text("SELECT shifde.shifde_bound_group_admin_ids(:g)"), {"g": group_id}
    ).scalars()
    return [UUID(str(r)) for r in rows]


# ------------------------------------------------------------------ policy decisions
def insert_decisions(session: Session, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(PolicyDecision), rows)


def may_name_owner(session: Session, tenant_id: UUID) -> bool:
    """True when the database context may bind a person to `tenant_administrator` at
    organization scope in `tenant_id` (which names an owner, B-1010): the platform-administrator
    context, or the current principal is that client's owner in the current client
    (`shifde_is_client_owner`, tenancy_0003, B-1016). The same rule as `check_role_binding`."""
    return bool(
        session.execute(
            text("SELECT shifde.is_platform_admin_ctx() OR shifde.shifde_is_client_owner(:t)"),
            {"t": tenant_id},
        ).scalar_one()
    )
