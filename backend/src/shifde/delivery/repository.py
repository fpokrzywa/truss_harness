"""SQL for deployment targets and platform claims (RLS tenant context; design 3, B-2501).

Every read and write is keyed by `tenant_id` as well as by row-level security. Claims are created
only by the `shifde.delivery_claim_platform` definer (lead ruling B-2524): it reads the client from
the session context and answers NULL, naming nobody, when another organization holds the address.
Everything that creates or releases a claim takes the same transaction-scoped advisory lock on
(platform kind, address key), so a register and an archive on one address never interleave.

Executors and their credentials (B-2664): `shifde_app` cannot read `secret_hash`; a token is
resolved only through the `shifde.delivery_resolve_executor` definer, which answers
(tenant_id, executor_id) or nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

TARGET_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, name, platform_kind, address, claim_id, "
    "environment, bindings, credential_ref, status, revision, created_by, created_at, updated_at"
)


def claim_platform(session: Session, platform_kind: str, address_key: str) -> UUID | None:
    """The caller's claim on the address (existing or new), or None when another organization
    holds it."""
    claim: UUID | None = session.execute(
        text("SELECT shifde.delivery_claim_platform(:k, :a)"),
        {"k": platform_kind, "a": address_key},
    ).scalar_one()
    return claim


def lock_claim(session: Session, claim_id: UUID, *, tenant_id: UUID) -> bool:
    """Take the claim's advisory lock (the one the definer takes); False if the claim is gone."""
    locked = session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "c.platform_kind || ':' || c.address_key, 0)) "
            "FROM shifde.delivery_platform_claims c WHERE c.id = :c AND c.tenant_id = :t"
        ),
        {"c": claim_id, "t": tenant_id},
    ).first()
    return locked is not None


def claim_in_use(session: Session, claim_id: UUID, *, tenant_id: UUID) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM shifde.delivery_targets WHERE tenant_id = :t "
                "AND claim_id = :c AND status = 'active')"
            ),
            {"c": claim_id, "t": tenant_id},
        ).scalar_one()
    )


def delete_claim(session: Session, claim_id: UUID, *, tenant_id: UUID) -> None:
    session.execute(
        text("DELETE FROM shifde.delivery_platform_claims WHERE id = :c AND tenant_id = :t"),
        {"c": claim_id, "t": tenant_id},
    )


def insert_target(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, project_id, name, "
            "platform_kind, address, claim_id, environment, bindings, credential_ref, status, "
            "revision, created_by) VALUES (:id, :tenant_id, :workspace_id, :project_id, :name, "
            ":platform_kind, :address, :claim_id, :environment, CAST(:bindings AS jsonb), "
            ":credential_ref, 'active', 1, :created_by)"
        ),
        {**values, "bindings": json.dumps(values["bindings"])},
    )


def get_target(session: Session, target_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {TARGET_COLUMNS} FROM shifde.delivery_targets "  # nosec B608
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": target_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_targets(
    session: Session,
    *,
    tenant_id: UUID,
    status: str | None,
    workspace_id: UUID | None,
    project_id: UUID | None,
    tenant_wide: bool,
    visible_workspaces: Sequence[UUID],
    visible_projects: Sequence[UUID],
    after: UUID | None,
    limit: int,
) -> list[Row]:
    sql = (
        # columns are module constants; values are bound
        f"SELECT {TARGET_COLUMNS} FROM shifde.delivery_targets WHERE tenant_id = :t "  # nosec B608
        "AND (CAST(:status AS text) IS NULL OR status = :status) "
        "AND (CAST(:workspace AS uuid) IS NULL OR workspace_id = :workspace) "
        "AND (CAST(:project AS uuid) IS NULL OR project_id = :project) "
        "AND (CAST(:after AS uuid) IS NULL OR id > :after) "
        "AND (:wide OR workspace_id = ANY(CAST(:vws AS uuid[])) "
        "     OR project_id = ANY(CAST(:vps AS uuid[]))) "
        "ORDER BY id LIMIT :n"
    )
    return list(
        session.execute(
            text(sql),
            {
                "t": tenant_id,
                "status": status,
                "workspace": workspace_id,
                "project": project_id,
                "after": after,
                "wide": tenant_wide,
                "vws": list(visible_workspaces),
                "vps": list(visible_projects),
                "n": limit,
            },
        )
        .mappings()
        .all()
    )


def archive_target(
    session: Session, target_id: UUID, claim_id: UUID, *, tenant_id: UUID
) -> Row | None:
    """Archive an active target still on `claim_id`; None if it changed meanwhile."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.delivery_targets SET status = 'archived', claim_id = "  # nosec B608
                "NULL, "
                "revision = revision + 1 WHERE id = :id AND tenant_id = :t "
                f"AND status = 'active' AND claim_id = :c RETURNING {TARGET_COLUMNS}"
            ),
            {"id": target_id, "t": tenant_id, "c": claim_id},
        )
        .mappings()
        .first()
    )


# ====================================================================== executors (B-2664)
EXECUTOR_COLUMNS = (
    "id, tenant_id, name, status, last_seen_at, version, created_by, created_at, updated_at"
)
# every column but `secret_hash`, which `shifde_app` cannot read (only the resolver definer does)
CREDENTIAL_COLUMNS = (
    "id, tenant_id, executor_id, prefix, created_by, expires_at, revoked_at, created_at, updated_at"
)


def insert_executor(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.delivery_executors (id, tenant_id, name, status, created_by) "
            "VALUES (:id, :tenant_id, :name, 'active', :created_by)"
        ),
        dict(values),
    )


def get_executor(
    session: Session, executor_id: UUID, *, tenant_id: UUID, for_update: bool = False
) -> Row | None:
    lock = " FOR UPDATE" if for_update else ""
    return (
        session.execute(
            text(
                # columns and the lock clause are module constants; values are bound
                f"SELECT {EXECUTOR_COLUMNS} FROM shifde.delivery_executors "  # nosec B608
                f"WHERE id = :id AND tenant_id = :t{lock}"
            ),
            {"id": executor_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_executors(
    session: Session, *, tenant_id: UUID, status: str | None, after: UUID | None, limit: int
) -> list[Row]:
    sql = (
        # columns are module constants; values are bound
        f"SELECT {EXECUTOR_COLUMNS} FROM shifde.delivery_executors "  # nosec B608
        "WHERE tenant_id = :t AND (CAST(:status AS text) IS NULL OR status = :status) "
        "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
    )
    return list(
        session.execute(text(sql), {"t": tenant_id, "status": status, "after": after, "n": limit})
        .mappings()
        .all()
    )


def set_executor_status(
    session: Session, executor_id: UUID, status: str, *, tenant_id: UUID
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.delivery_executors SET status = :s "  # nosec B608
                f"WHERE id = :id AND tenant_id = :t RETURNING {EXECUTOR_COLUMNS}"
            ),
            {"id": executor_id, "t": tenant_id, "s": status},
        )
        .mappings()
        .first()
    )


def touch_executor(
    session: Session, executor_id: UUID, version: str | None, *, tenant_id: UUID
) -> Row | None:
    """`last_seen_at = now()` and, when given, the executor's reported version."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.delivery_executors SET last_seen_at = now(), "  # nosec B608
                "version = COALESCE(CAST(:v AS text), version) WHERE id = :id AND tenant_id = :t "
                f"RETURNING {EXECUTOR_COLUMNS}"
            ),
            {"id": executor_id, "t": tenant_id, "v": version},
        )
        .mappings()
        .first()
    )


def resolve_executor(session: Session, prefix: str, secret_hash: bytes) -> Row | None:
    """(tenant_id, executor_id) of a usable credential, or None (the definer names no reason)."""
    return (
        session.execute(
            text("SELECT tenant_id, executor_id FROM shifde.delivery_resolve_executor(:p, :h)"),
            {"p": prefix, "h": secret_hash},
        )
        .mappings()
        .first()
    )


def insert_credential(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.delivery_executor_credentials (id, tenant_id, executor_id, prefix, "
            "secret_hash, created_by, expires_at) VALUES (:id, :tenant_id, :executor_id, :prefix, "
            ":secret_hash, :created_by, :expires_at)"
        ),
        dict(values),
    )


def get_credential(
    session: Session, credential_id: UUID, executor_id: UUID, *, tenant_id: UUID
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {CREDENTIAL_COLUMNS} FROM "  # nosec B608
                "shifde.delivery_executor_credentials "
                "WHERE id = :id AND executor_id = :e AND tenant_id = :t"
            ),
            {"id": credential_id, "e": executor_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_credentials(session: Session, executor_id: UUID, *, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {CREDENTIAL_COLUMNS} FROM "  # nosec B608
                "shifde.delivery_executor_credentials "
                "WHERE executor_id = :e AND tenant_id = :t ORDER BY id"
            ),
            {"e": executor_id, "t": tenant_id},
        )
        .mappings()
        .all()
    )


def count_live_credentials(session: Session, executor_id: UUID, *, tenant_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.delivery_executor_credentials WHERE executor_id = :e "
                "AND tenant_id = :t AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"e": executor_id, "t": tenant_id},
        ).scalar_one()
    )


def revoke_credential(session: Session, credential_id: UUID, *, tenant_id: UUID) -> Row | None:
    """Revoke an unrevoked credential; None if it was already revoked."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.delivery_executor_credentials "  # nosec B608
                "SET revoked_at = now() WHERE id = :id AND tenant_id = :t "
                f"AND revoked_at IS NULL RETURNING {CREDENTIAL_COLUMNS}"
            ),
            {"id": credential_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def revoke_executor_credentials(
    session: Session, executor_id: UUID, *, tenant_id: UUID
) -> list[Row]:
    """Revoke every unrevoked credential of the executor; the revoked rows."""
    return list(
        session.execute(
            text(
                "UPDATE shifde.delivery_executor_credentials SET revoked_at = now() "
                "WHERE executor_id = :e AND tenant_id = :t AND revoked_at IS NULL "
                "RETURNING id, prefix"
            ),
            {"e": executor_id, "t": tenant_id},
        )
        .mappings()
        .all()
    )
