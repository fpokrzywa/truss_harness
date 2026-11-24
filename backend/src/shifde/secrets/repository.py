"""SQL for secrets and the secret access log (RLS: tenant, or platform context for tenant NULL).

Every lookup and update is also keyed by the owning tenant (`tenant_id`, `None` = installation
level), so a caller that names another client's secret id finds nothing even where row-level
security would not apply (DECISIONS B-802).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any  # Any: column value dicts
from uuid import UUID

from sqlalchemy import insert, or_, select, text, update
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


def touch_secret(
    session: Session,
    secret_id: UUID,
    now: datetime,
    min_interval_seconds: int,
    *,
    tenant_id: UUID | None,
) -> bool:
    """Throttled, non-blocking last-used update: at most once per interval per secret, and only
    if the row can be locked at once (`FOR UPDATE SKIP LOCKED`), so a read never waits on another
    transaction's lock on the secret row (B-3968). Every UPDATE of a secret is a key update (the
    stored generated `installation_secret_id` is in a unique constraint), so the lock must also
    skip a row another read holds `FOR KEY SHARE` through its access-log foreign key; a weaker
    lock would then wait in the UPDATE itself. True if the row changed."""
    locked = (
        select(Secret.id)
        .where(
            Secret.id == secret_id,
            _owned_by(tenant_id),
            or_(
                Secret.last_used_at.is_(None),
                Secret.last_used_at < now - timedelta(seconds=min_interval_seconds),
            ),
        )
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    res = session.execute(
        update(Secret)
        .where(Secret.id == locked)
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]  # DML returns a CursorResult


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


def installation_idp_secret(session: Session, idp_id: UUID) -> Secret | None:
    """The envelope of the active installation IdP `idp_id`'s client secret through
    `shifde_installation_idp_secret` (secrets_0003, B-2260), which also writes the access-log row
    and `last_used_at`. Refused (42501) under a client context; no row for a client's, disabled
    or unknown IdP. The returned `Secret` is transient (never added to the session)."""
    row = (
        session.execute(
            text(
                "SELECT secret_id, secret_name, provider_key, external_ref, ciphertext, "
                "wrapped_dek, nonce, kek_id, secret_version "
                "FROM shifde.shifde_installation_idp_secret(:i)"
            ),
            {"i": idp_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Secret(
        id=row["secret_id"],
        tenant_id=None,
        name=row["secret_name"],
        provider_key=row["provider_key"],
        external_ref=row["external_ref"],
        ciphertext=row["ciphertext"],
        wrapped_dek=row["wrapped_dek"],
        nonce=row["nonce"],
        kek_id=row["kek_id"],
        secret_version=row["secret_version"],
        status="active",
    )
