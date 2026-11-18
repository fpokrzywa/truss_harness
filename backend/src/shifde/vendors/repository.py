"""SQL for the vendor catalog (global rows have tenant_id NULL). Runs under RLS context."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

COLUMNS = (
    "id, tenant_id, key, name, category, description, version, status, "
    "configuration_requirements, adapter_keys, capabilities, row_version, created_at, updated_at"
)
UPDATABLE = frozenset(
    {"name", "description", "version", "status", "configuration_requirements", "adapter_keys"}
)
JSON_COLUMNS = frozenset({"configuration_requirements", "capabilities"})


Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values


def get_vendor(session: Session, vendor_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {COLUMNS} FROM shifde.vendors WHERE id = :id"  # nosec B608
            ),
            {"id": vendor_id},
        )
        .mappings()
        .first()
    )


def list_vendors(
    session: Session,
    *,
    category: str | None,
    status: str | None,
    only_global: bool,
    after: UUID | None,
    limit: int,
) -> list[Row]:
    clauses = ["(CAST(:category AS text) IS NULL OR category = :category)"]
    clauses.append("(CAST(:status AS text) IS NULL OR status = :status)")
    clauses.append("(CAST(:after AS uuid) IS NULL OR id > :after)")
    if only_global:
        clauses.append("tenant_id IS NULL")
    sql = (
        # fixed clauses and constant columns; values are bound
        f"SELECT {COLUMNS} FROM shifde.vendors WHERE {' AND '.join(clauses)} "  # nosec B608
        "ORDER BY id LIMIT :n"
    )
    return list(
        session.execute(
            text(sql), {"category": category, "status": status, "after": after, "n": limit}
        )
        .mappings()
        .all()
    )


def insert_vendor(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.vendors (id, tenant_id, key, name, category, description, version, "
            "status, configuration_requirements, adapter_keys, capabilities, created_by) VALUES "
            "(:id, :tenant_id, :key, :name, :category, :description, :version, :status, "
            "CAST(:configuration_requirements AS jsonb), :adapter_keys, "
            "CAST(:capabilities AS jsonb), "
            ":created_by)"
        ),
        {
            **values,
            "configuration_requirements": json.dumps(values["configuration_requirements"]),
            "capabilities": json.dumps(values.get("capabilities", [])),
        },
    )


def update_vendor(
    session: Session, vendor_id: UUID, expected_version: int | None, values: Mapping[str, Any]
) -> Row | None:
    sets: list[str] = ["row_version = row_version + 1"]
    params: dict[str, Any] = {"id": vendor_id, "expected": expected_version}
    for column, value in values.items():
        if column not in UPDATABLE:
            raise ValueError(f"column {column!r} is not updatable")
        if column in JSON_COLUMNS:
            sets.append(f"{column} = CAST(:{column} AS jsonb)")
            params[column] = json.dumps(value)
        else:
            sets.append(f"{column} = :{column}")
            params[column] = value
    sql = (
        # SET names checked against the code allowlist; values are bound
        f"UPDATE shifde.vendors SET {', '.join(sets)} WHERE id = :id "  # nosec B608
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {COLUMNS}"
    )
    return session.execute(text(sql), params).mappings().first()
