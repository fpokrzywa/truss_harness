"""Small SQL helpers shared by raw-SQL repositories (values are always bound parameters)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: column values
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def update_returning(
    session: Session,
    table: str,
    columns: str,
    updatable: frozenset[str],
    row_id: UUID,
    expected_version: int | None,
    values: Mapping[str, Any],
    json_columns: frozenset[str] = frozenset(),
) -> Mapping[Any, Any] | None:  # Any: RowMapping keys
    """Optimistic-locking ``UPDATE ... RETURNING``. Table/column names come from code-owned
    allowlists (`updatable`); every value is a bound parameter."""
    sets = ["row_version = row_version + 1"]
    params: dict[str, Any] = {"id": row_id, "expected": expected_version}
    for column, value in values.items():
        if column not in updatable:
            raise ValueError(f"column {column!r} is not updatable")
        if column in json_columns:
            sets.append(f"{column} = CAST(:{column} AS jsonb)")
            params[column] = json.dumps(value)
        else:
            sets.append(f"{column} = :{column}")
            params[column] = value
    sql = (
        f"UPDATE shifde.{table} SET {', '.join(sets)} WHERE id = :id "
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {columns}"
    )
    return session.execute(text(sql), params).mappings().first()
