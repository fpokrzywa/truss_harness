"""SQL for secrets and the secret access log (RLS: tenant, or platform context for tenant NULL).

Every lookup and update is also keyed by the owning tenant (`tenant_id`, `None` = installation
level), so a caller that names another client's secret id finds nothing even where row-level
security would not apply (DECISIONS B-802).
"""

from __future__ import annotations

from typing import Any  # Any: column value dicts
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.orm import Session

from shifde.secrets.models import Secret


def _owned_by(tenant_id: UUID | None) -> Any:  # Any: a SQLAlchemy boolean clause
    return Secret.tenant_id.is_(None) if tenant_id is None else Secret.tenant_id == tenant_id


def insert_secret(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(Secret).values(**values))


def get_secret(session: Session, secret_id: UUID, *, tenant_id: UUID | None) -> Secret | None:
    return session.scalar(select(Secret).where(Secret.id == secret_id, _owned_by(tenant_id)))


def list_secrets(
    session: Session, *, tenant_id: UUID | None, after: UUID | None, limit: int
) -> list[Secret]:
    stmt = select(Secret).where(_owned_by(tenant_id))
    if after is not None:
        stmt = stmt.where(Secret.id > after)
    return list(session.scalars(stmt.order_by(Secret.id).limit(limit)))


def update_secret(
    session: Session,
    secret_id: UUID,
    expected_version: int | None,
    values: dict[str, Any],
    *,
    tenant_id: UUID | None,
    bump: bool = True,
) -> Secret | None:
    stmt = update(Secret).where(Secret.id == secret_id, _owned_by(tenant_id))
    if expected_version is not None:
        stmt = stmt.where(Secret.row_version == expected_version)
    stmt = (
        stmt.values(**values, **({"row_version": Secret.row_version + 1} if bump else {}))
        .returning(Secret)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


def all_secrets(session: Session) -> list[Secret]:
    """Every secret row (owner/maintenance sessions only: relies on BYPASSRLS)."""
    return list(session.scalars(select(Secret).order_by(Secret.id)))


def project_workspace_id(session: Session, project_id: UUID, *, tenant_id: UUID) -> UUID | None:
    found: UUID | None = session.scalar(
        text("SELECT workspace_id FROM shifde.projects WHERE id = :p AND tenant_id = :t"),
        {"p": project_id, "t": tenant_id},
    )
    return found


def insert_access(session: Session, values: dict[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.secret_access_log "
            "(id, tenant_id, secret_id, at, principal_id, purpose, run_id, outcome) "
            "VALUES (:id, :tenant_id, :secret_id, :at, :principal_id, :purpose, :run_id, :outcome)"
        ),
        values,
    )
