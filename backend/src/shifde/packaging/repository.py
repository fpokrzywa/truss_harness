"""SQL for solutions and solution versions (RLS tenant context; sub-project 3 design section 4).

Every read and write is keyed by `tenant_id` as well as by row-level security. Solution versions
are immutable (`shifde.solution_versions_guard`), and the guard also refuses an entrypoint that is
not a published version of the row's kind. Build tables are never read here: build data comes
from `shifde.build.service`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

SOLUTION_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, name, target_kind, status, created_by, created_at, "
    "updated_at"
)
VERSION_COLUMNS = (
    "id, tenant_id, solution_id, version_no, entrypoint_kind, entrypoint_version_id, spec, "
    "spec_hash, created_by, created_at"
)


def insert_solution(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.solutions (id, tenant_id, workspace_id, project_id, name, "
            "target_kind, status, created_by) VALUES (:id, :tenant_id, :workspace_id, "
            ":project_id, :name, :target_kind, 'active', :created_by)"
        ),
        dict(values),
    )


def get_solution(
    session: Session, solution_id: UUID, *, tenant_id: UUID, lock: bool = False
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SOLUTION_COLUMNS} FROM shifde.solutions "  # nosec B608
                "WHERE id = :id AND tenant_id = :t" + (" FOR UPDATE" if lock else "")
            ),
            {"id": solution_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def name_taken(session: Session, project_id: UUID, name: str, *, tenant_id: UUID) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM shifde.solutions WHERE tenant_id = :t "
                "AND project_id = :p AND name = :n)"
            ),
            {"t": tenant_id, "p": project_id, "n": name},
        ).scalar_one()
    )


def list_solutions(
    session: Session,
    *,
    tenant_id: UUID,
    project_id: UUID,
    status: str | None,
    after: UUID | None,
    limit: int,
) -> list[Row]:
    sql = (
        # columns are module constants; values are bound
        f"SELECT {SOLUTION_COLUMNS} FROM shifde.solutions WHERE tenant_id = :t "  # nosec B608
        "AND project_id = :p "
        "AND (CAST(:status AS text) IS NULL OR status = :status) "
        "AND (CAST(:after AS uuid) IS NULL OR id > :after) "
        "ORDER BY id LIMIT :n"
    )
    return list(
        session.execute(
            text(sql),
            {"t": tenant_id, "p": project_id, "status": status, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def archive_solution(session: Session, solution_id: UUID, *, tenant_id: UUID) -> Row | None:
    """Archive an active solution; None if it is not active (any more)."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.solutions SET status = 'archived' "  # nosec B608
                "WHERE id = :id AND tenant_id = :t AND status = 'active' "
                f"RETURNING {SOLUTION_COLUMNS}"
            ),
            {"id": solution_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def next_version_no(session: Session, solution_id: UUID, *, tenant_id: UUID) -> int:
    """max + 1 of the solution's version numbers (the caller holds the solution row lock)."""
    return int(
        session.execute(
            text(
                "SELECT coalesce(max(version_no), 0) + 1 FROM shifde.solution_versions "
                "WHERE tenant_id = :t AND solution_id = :s"
            ),
            {"t": tenant_id, "s": solution_id},
        ).scalar_one()
    )


def insert_version(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.solution_versions (id, tenant_id, solution_id, version_no, "
            "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES (:id, "
            ":tenant_id, :solution_id, :version_no, :entrypoint_kind, :entrypoint_version_id, "
            "CAST(:spec AS jsonb), :spec_hash, :created_by)"
        ),
        {**values, "spec": json.dumps(values["spec"])},
    )


def get_version(session: Session, version_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {VERSION_COLUMNS} FROM shifde.solution_versions "  # nosec B608
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": version_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_versions(
    session: Session, solution_id: UUID, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[Row]:
    """Newest first; `after` is the id of the last version of the previous page."""
    sql = (
        # columns are module constants; values are bound
        f"SELECT {VERSION_COLUMNS} FROM shifde.solution_versions "  # nosec B608
        "WHERE tenant_id = :t AND solution_id = :s "
        "AND (CAST(:after AS uuid) IS NULL OR version_no < (SELECT a.version_no "
        "FROM shifde.solution_versions a WHERE a.tenant_id = :t AND a.solution_id = :s "
        "AND a.id = :after)) "
        "ORDER BY version_no DESC LIMIT :n"
    )
    return list(
        session.execute(text(sql), {"t": tenant_id, "s": solution_id, "after": after, "n": limit})
        .mappings()
        .all()
    )
