"""SQL for deployment targets and platform claims (RLS tenant context; design 3, B-2501).

Every read and write is keyed by `tenant_id` as well as by row-level security. Claims are created
only by the `shifde.delivery_claim_platform` definer (lead ruling B-2524): it reads the client from
the session context and answers NULL, naming nobody, when another organization holds the address.
Everything that creates or releases a claim takes the same transaction-scoped advisory lock on
(platform kind, address key), so a register and an archive on one address never interleave.
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
