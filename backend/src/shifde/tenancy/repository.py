"""SQL for tenants, workspaces and projects. All queries run under RLS tenant context."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, insert, or_, select, text, update
from sqlalchemy.orm import Session

from shifde.tenancy.models import InstallationSettings, Project, Tenant, Workspace


# ------------------------------------------------------------------ workers (platform status)
def worker_heartbeats(session: Session, window_seconds: int) -> tuple[int, Any]:
    """(running workers that beat within `window_seconds`, latest heartbeat or None). `workers`
    is installation-level (runtime_0001: heartbeat rows of host, pid and version, no client
    column); the caller is the platform administrator (`service.platform_status`)."""
    row = session.execute(
        text(
            "SELECT count(*) FILTER (WHERE status = 'running' "
            "AND heartbeat_at > now() - make_interval(secs => :win)), max(heartbeat_at) "
            "FROM shifde.workers"
        ),
        {"win": window_seconds},
    ).one()
    return int(row[0]), row[1]


def installation_idp_count(session: Session) -> int:
    """Active installation identity providers (`tenant_id IS NULL`), counted as the sign-in
    page lists them (one per display name and issuer, `installation.service._oidc_providers`).
    A client's IdP is that client's content and is never counted (B-2441)."""
    return int(
        session.execute(
            text(
                "SELECT count(DISTINCT (display_name, issuer)) FROM shifde.idp_configs "
                "WHERE tenant_id IS NULL AND status = 'active'"
            )
        ).scalar_one()
    )


# ------------------------------------------------------------------ tenants
def get_tenant(session: Session, tenant_id: UUID) -> Tenant | None:
    return session.get(Tenant, tenant_id)


def list_tenants(
    session: Session, *, include_archived: bool, after: UUID | None, limit: int
) -> list[Tenant]:
    """All tenants; only meaningful in platform-admin context (tenants RLS policy)."""
    stmt = select(Tenant)
    if not include_archived:
        stmt = stmt.where(Tenant.status == "active")
    if after is not None:
        stmt = stmt.where(Tenant.id > after)
    return list(session.scalars(stmt.order_by(Tenant.id).limit(limit)))


def get_tenant_by_slug(session: Session, slug: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.slug == slug))


def tenant_slug_exists(session: Session, slug: str) -> bool:
    # Runs in platform-admin context (tenants SELECT policy), so all slugs are visible.
    return session.scalar(select(func.count()).select_from(Tenant).where(Tenant.slug == slug)) == 1


def insert_tenant(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(Tenant).values(**values))


def update_tenant(
    session: Session, tenant_id: UUID, expected_version: int, values: dict[str, Any]
) -> Tenant | None:
    stmt = (
        update(Tenant)
        .where(Tenant.id == tenant_id, Tenant.row_version == expected_version)
        .values(**values, row_version=Tenant.row_version + 1)
        .returning(Tenant)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


def get_installation_settings(session: Session) -> dict[str, Any]:
    row = session.get(InstallationSettings, 1)
    if row is None:
        return {}
    return {
        "branding": row.branding,
        "feature_flags": row.feature_flags,
        "auth_modes": row.auth_modes,
        "default_limits": row.default_limits,
    }


# ------------------------------------------------------------------ workspaces
def get_workspace_by_slug(session: Session, tenant_id: UUID, slug: str) -> Workspace | None:
    return session.scalar(
        select(Workspace).where(Workspace.tenant_id == tenant_id, Workspace.slug == slug)
    )


def get_project_by_id(session: Session, tenant_id: UUID, project_id: UUID) -> Project | None:
    return session.scalar(
        select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id)
    )


def get_workspace(session: Session, tenant_id: UUID, workspace_id: UUID) -> Workspace | None:
    return session.scalar(
        select(Workspace).where(Workspace.tenant_id == tenant_id, Workspace.id == workspace_id)
    )


def list_workspaces(
    session: Session,
    *,
    tenant_id: UUID,
    only_ids: Collection[UUID] | None,
    include_archived: bool,
    after: UUID | None,
    limit: int,
) -> list[Workspace]:
    stmt = select(Workspace).where(Workspace.tenant_id == tenant_id)
    if only_ids is not None:
        stmt = stmt.where(Workspace.id.in_(list(only_ids)))
    if not include_archived:
        stmt = stmt.where(Workspace.status == "active")
    if after is not None:
        stmt = stmt.where(Workspace.id > after)
    return list(session.scalars(stmt.order_by(Workspace.id).limit(limit)))


def workspace_ids(
    session: Session, tenant_id: UUID, only_ids: Collection[UUID] | None
) -> list[UUID]:
    stmt = select(Workspace.id).where(
        Workspace.tenant_id == tenant_id, Workspace.status == "active"
    )
    if only_ids is not None:
        stmt = stmt.where(Workspace.id.in_(list(only_ids)))
    return list(session.scalars(stmt))


def count_workspaces(session: Session, tenant_id: UUID, only_ids: Collection[UUID] | None) -> int:
    stmt = (
        select(func.count())
        .select_from(Workspace)
        .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
    )
    if only_ids is not None:
        stmt = stmt.where(Workspace.id.in_(list(only_ids)))
    return session.scalar(stmt) or 0


def insert_workspace(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(Workspace).values(**values))


def update_workspace(
    session: Session,
    tenant_id: UUID,
    workspace_id: UUID,
    expected_version: int,
    values: dict[str, Any],
) -> Workspace | None:
    stmt = (
        update(Workspace)
        .where(
            Workspace.tenant_id == tenant_id,
            Workspace.id == workspace_id,
            Workspace.row_version == expected_version,
        )
        .values(**values, row_version=Workspace.row_version + 1)
        .returning(Workspace)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


# ------------------------------------------------------------------ projects
def get_project_by_slug(
    session: Session, tenant_id: UUID, workspace_id: UUID, slug: str
) -> Project | None:
    return session.scalar(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.workspace_id == workspace_id,
            Project.slug == slug,
        )
    )


def _visibility(
    workspace_ids_: Collection[UUID] | None, project_ids: Collection[UUID]
) -> ColumnElement[bool] | None:
    """None = unrestricted. Otherwise the predicate for visible projects."""
    if workspace_ids_ is None:
        return None
    return or_(Project.workspace_id.in_(list(workspace_ids_)), Project.id.in_(list(project_ids)))


def list_projects(
    session: Session,
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    visible_workspace_ids: Collection[UUID] | None,
    visible_project_ids: Collection[UUID],
    include_archived: bool,
    after: UUID | None,
    limit: int,
) -> list[Project]:
    stmt = select(Project).where(
        Project.tenant_id == tenant_id, Project.workspace_id == workspace_id
    )
    clause = _visibility(visible_workspace_ids, visible_project_ids)
    if clause is not None:
        stmt = stmt.where(clause)
    if not include_archived:
        stmt = stmt.where(Project.status == "active")
    if after is not None:
        stmt = stmt.where(Project.id > after)
    return list(session.scalars(stmt.order_by(Project.id).limit(limit)))


def count_projects(
    session: Session,
    *,
    tenant_id: UUID,
    workspace_id: UUID | None,
    visible_workspace_ids: Collection[UUID] | None,
    visible_project_ids: Collection[UUID],
) -> int:
    stmt = (
        select(func.count())
        .select_from(Project)
        .where(Project.tenant_id == tenant_id, Project.status == "active")
    )
    if workspace_id is not None:
        stmt = stmt.where(Project.workspace_id == workspace_id)
    clause = _visibility(visible_workspace_ids, visible_project_ids)
    if clause is not None:
        stmt = stmt.where(clause)
    return session.scalar(stmt) or 0


def insert_project(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(Project).values(**values))


def update_project(
    session: Session,
    tenant_id: UUID,
    project_id: UUID,
    expected_version: int,
    values: dict[str, Any],
) -> Project | None:
    stmt = (
        update(Project)
        .where(
            Project.tenant_id == tenant_id,
            Project.id == project_id,
            Project.row_version == expected_version,
        )
        .values(**values, row_version=Project.row_version + 1)
        .returning(Project)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


def may_bind_group(session: Session, tenant_id: UUID, group_id: UUID) -> bool:
    """True when the current principal is the owner of `tenant_id`, that is the database's
    current client, and `group_id` is an active platform group (`shifde_may_bind_group`,
    tenancy_0002, DECISIONS B-1010). False for every other case alike."""
    return bool(
        session.execute(
            text("SELECT shifde.shifde_may_bind_group(:t, :g)"), {"t": tenant_id, "g": group_id}
        ).scalar_one()
    )


def actor_is_platform_admin(session: Session, user_id: UUID) -> bool:
    """The `users.is_platform_admin` flag of the current principal (a person reads their own row,
    `users_select`); False for anyone else. The principal's flag is off on a session pinned to a
    client, which is where a self-added administrator acts as an owner (B-1017)."""
    return bool(
        session.execute(
            text(
                "SELECT COALESCE(bool_or(u.is_platform_admin), false) FROM shifde.users u "
                "WHERE u.id = :u AND u.id = shifde.current_principal_id()"
            ),
            {"u": user_id},
        ).scalar_one()
    )


def current_tenant_id(session: Session) -> UUID | None:
    """The client the database holds for this transaction (B-703: never `session.info`)."""
    value = session.execute(text("SELECT shifde.current_tenant_id()")).scalar_one()
    return value if value is None else UUID(str(value))


def bound_group_admin_ids(session: Session, group_id: UUID) -> list[UUID]:
    """Ids of the platform administrators who are members of `group_id`, only when the group
    holds a role binding in the current client (`shifde_bound_group_admin_ids`, tenancy_0004,
    DECISIONS B-1407); empty otherwise."""
    rows = session.execute(
        text("SELECT shifde.shifde_bound_group_admin_ids(:g)"), {"g": group_id}
    ).scalars()
    return [UUID(str(r)) for r in rows]
