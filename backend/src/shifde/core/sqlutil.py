"""Small SQL helpers shared by raw-SQL repositories (values are always bound parameters)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any  # Any: column values
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_PROTECTED = frozenset({"id", "tenant_id"})


def _identifier(kind: str, name: str) -> None:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"{kind} {name!r} is not a plain lower-case SQL identifier")


def update_returning(
    session: Session,
    table: str,
    columns: str,
    updatable: frozenset[str],
    row_id: UUID,
    expected_version: int | None,
    values: Mapping[str, Any],
    json_columns: frozenset[str] = frozenset(),
    *,
    tenant_id: UUID | None = None,
) -> Mapping[Any, Any] | None:  # Any: RowMapping keys
    """Optimistic-locking ``UPDATE ... RETURNING``. Table/column names come from code-owned
    allowlists (`updatable`); every value is a bound parameter."""
    _identifier("table", table)
    for column in (*updatable, *json_columns, *values):
        _identifier("column", column)
    for column in updatable:
        if column in _PROTECTED:
            raise ValueError(f"column {column!r} may never be updatable")
    sets = ["row_version = row_version + 1"]
    params: dict[str, Any] = {"id": row_id, "expected": expected_version}
    scope = ""
    if tenant_id is not None:  # second fence next to row-level security (DECISIONS B-1902)
        scope = " AND tenant_id = :tenant_id"
        params["tenant_id"] = tenant_id
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
        f"UPDATE shifde.{table} SET {', '.join(sets)} WHERE id = :id{scope} "
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {columns}"
    )
    return session.execute(text(sql), params).mappings().first()
