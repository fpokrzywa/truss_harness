"""SQL for the assist settings and specialists (assist_0001), under the caller's client context.

Every read and write is keyed by `tenant_id` as well as by row-level security (the B-802
pattern). A status change is a conditional UPDATE on the states the caller expects, so of two
racing changes exactly one wins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

SETTINGS_COLUMNS = (
    "assist_enabled, catalog_version, model_routing_enabled, router_model_id, "
    "specialist_max_cost, updated_by, updated_at"
)
SPECIALIST_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, key, agent_id, agent_version_id, catalog_version, "
    "install_progress, status, installed_by, created_at, updated_at"
)


# ====================================================================== settings
def get_settings(session: Session, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SETTINGS_COLUMNS} FROM shifde.assist_settings "  # nosec B608
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .first()
    )


def put_routing(
    session: Session,
    *,
    tenant_id: UUID,
    model_routing_enabled: bool,
    router_model_id: UUID | None,
    specialist_max_cost: Decimal,
    updated_by: UUID,
) -> None:
    """Upsert the routing fields and the cost cap; the switch and the pin are left as they are."""
    session.execute(
        text(
            "INSERT INTO shifde.assist_settings (tenant_id, model_routing_enabled, "
            "router_model_id, specialist_max_cost, updated_by, updated_at) "
            "VALUES (:t, :r, :m, :c, :u, now()) ON CONFLICT (tenant_id) DO UPDATE SET "
            "model_routing_enabled = excluded.model_routing_enabled, "
            "router_model_id = excluded.router_model_id, "
            "specialist_max_cost = excluded.specialist_max_cost, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at"
        ),
        {
            "t": tenant_id,
            "r": model_routing_enabled,
            "m": router_model_id,
            "c": specialist_max_cost,
            "u": updated_by,
        },
    )


def put_switch(
    session: Session, *, tenant_id: UUID, enabled: bool, pin: str | None, updated_by: UUID
) -> None:
    """Upsert the organization switch; `pin` set replaces the catalog pin, None keeps it."""
    session.execute(
        text(
            "INSERT INTO shifde.assist_settings (tenant_id, assist_enabled, catalog_version, "
            "updated_by, updated_at) VALUES (:t, :e, :p, :u, now()) "
            "ON CONFLICT (tenant_id) DO UPDATE SET assist_enabled = excluded.assist_enabled, "
            "catalog_version = coalesce(CAST(:p AS text), shifde.assist_settings.catalog_version), "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at"
        ),
        {"t": tenant_id, "e": enabled, "p": pin, "u": updated_by},
    )


# ====================================================================== specialists
def get_specialist(
    session: Session, *, tenant_id: UUID, project_id: UUID, key: str, lock: bool = False
) -> Row | None:
    if lock:
        # locked first, read after: a read in the locking statement keeps the snapshot from
        # before the wait, so it would miss the progress the lock holder just committed
        session.execute(
            text(
                "SELECT 1 FROM shifde.assist_specialists "
                "WHERE tenant_id = :t AND project_id = :p AND key = :k FOR UPDATE"
            ),
            {"t": tenant_id, "p": project_id, "k": key},
        )
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SPECIALIST_COLUMNS} FROM shifde.assist_specialists "  # nosec B608
                "WHERE tenant_id = :t AND project_id = :p AND key = :k"
            ),
            {"t": tenant_id, "p": project_id, "k": key},
        )
        .mappings()
        .first()
    )


def list_specialists(
    session: Session, *, tenant_id: UUID, project_id: UUID | None = None
) -> list[Row]:
    """The client's specialists (one project's when given)."""
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SPECIALIST_COLUMNS} FROM shifde.assist_specialists "  # nosec B608
                "WHERE tenant_id = :t AND (CAST(:p AS uuid) IS NULL OR project_id = :p) "
                "ORDER BY project_id, id"
            ),
            {"t": tenant_id, "p": project_id},
        )
        .mappings()
        .all()
    )


def insert_specialist(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.assist_specialists (tenant_id, id, workspace_id, project_id, key, "
            "catalog_version, installed_by) VALUES (:tenant_id, :id, :workspace_id, :project_id, "
            ":key, :catalog_version, :installed_by)"
        ),
        dict(values),
    )


def set_progress(
    session: Session, specialist_id: UUID, *, tenant_id: UUID, progress: Sequence[Any]
) -> None:
    session.execute(
        text(
            "UPDATE shifde.assist_specialists SET install_progress = CAST(:pr AS jsonb), "
            "updated_at = now() WHERE tenant_id = :t AND id = :id"
        ),
        {"t": tenant_id, "id": specialist_id, "pr": json.dumps(list(progress))},
    )


def finish_install(
    session: Session,
    specialist_id: UUID,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    agent_version_id: UUID,
    catalog_version: str,
) -> bool:
    """Record the installed agent and set `enabled`; False when the row was disabled meanwhile."""
    changed = session.execute(
        text(
            "UPDATE shifde.assist_specialists SET agent_id = :a, agent_version_id = :v, "
            "catalog_version = :c, status = 'enabled', updated_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status IN ('installing', 'enabled') "
            "RETURNING id"
        ),
        {"t": tenant_id, "id": specialist_id, "a": agent_id, "v": agent_version_id,
         "c": catalog_version},
    ).first()  # fmt: skip
    return changed is not None


def change_status(
    session: Session, specialist_id: UUID, *, tenant_id: UUID, expected: Sequence[str], status: str
) -> bool:
    changed = session.execute(
        text(
            "UPDATE shifde.assist_specialists SET status = :s, updated_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status = ANY (CAST(:e AS text[])) RETURNING id"
        ),
        {"t": tenant_id, "id": specialist_id, "s": status, "e": list(expected)},
    ).first()
    return changed is not None
