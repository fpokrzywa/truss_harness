"""SQL for solutions, solution versions and releases (RLS tenant context; sub-project 3 design
section 4).

Every read and write is keyed by `tenant_id` as well as by row-level security, at every join.
Solution versions are immutable (`shifde.solution_versions_guard`), and the guard also refuses an
entrypoint that is not a published version of the row's kind; a release changes only from
`sealed` to `withdrawn` (`shifde.releases_guard`). Build tables are never read here: build data
comes from `shifde.build.service`. One read touches an evaluation table: `get_dataset_placement`
selects a dataset's id, workspace, project and digest (tenant-keyed) to check a version's
evaluation contract, since `packaging` sits below `evaluation` and cannot import it.
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

# a release with its scope: releases -> solution_versions -> solutions, each join on tenant_id
RELEASE_SELECT = (
    "SELECT r.id, r.tenant_id, r.solution_version_id, r.digest, r.manifest, r.compatibility, "
    "r.status, r.sealed_by, r.sealed_at, r.created_at, r.updated_at, v.solution_id, "
    "v.version_no, s.workspace_id, s.project_id, s.name AS solution_name "
    "FROM shifde.releases r JOIN shifde.solution_versions v "
    "ON v.tenant_id = r.tenant_id AND v.id = r.solution_version_id "
    "JOIN shifde.solutions s ON s.tenant_id = v.tenant_id AND s.id = v.solution_id "
    "WHERE r.tenant_id = :t "
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


def active_solution_id(
    session: Session, project_id: UUID, name: str, *, tenant_id: UUID
) -> UUID | None:
    """The id of the project's active solution named `name`, if any."""
    found = session.execute(
        text(
            "SELECT id FROM shifde.solutions WHERE tenant_id = :t AND project_id = :p "
            "AND name = :n AND status = 'active'"
        ),
        {"t": tenant_id, "p": project_id, "n": name},
    ).scalar_one_or_none()
    return UUID(str(found)) if found is not None else None


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


# ------------------------------------------------------------------ releases
def insert_release(session: Session, values: Mapping[str, Any]) -> UUID | None:
    """Insert a sealed release; None when this client already has a release with its digest."""
    inserted = session.execute(
        text(
            "INSERT INTO shifde.releases (id, tenant_id, solution_version_id, digest, manifest, "
            "compatibility, status, sealed_by) VALUES (:id, :tenant_id, :solution_version_id, "
            ":digest, CAST(:manifest AS jsonb), CAST(:compatibility AS jsonb), 'sealed', "
            ":sealed_by) ON CONFLICT (tenant_id, digest) DO NOTHING RETURNING id"
        ),
        {
            **values,
            "manifest": json.dumps(values["manifest"]),
            "compatibility": json.dumps(values["compatibility"]),
        },
    ).scalar_one_or_none()
    return UUID(str(inserted)) if inserted is not None else None


def get_release(session: Session, release_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(text(RELEASE_SELECT + "AND r.id = :id"), {"t": tenant_id, "id": release_id})
        .mappings()
        .first()
    )


def get_release_by_digest(session: Session, digest: str, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(text(RELEASE_SELECT + "AND r.digest = :d"), {"t": tenant_id, "d": digest})
        .mappings()
        .first()
    )


def list_releases(
    session: Session, solution_version_id: UUID, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[Row]:
    """Newest first (ids are time-ordered); `after` is the last id of the previous page."""
    sql = (
        RELEASE_SELECT + "AND r.solution_version_id = :v "
        "AND (CAST(:after AS uuid) IS NULL OR r.id < :after) "
        "ORDER BY r.id DESC LIMIT :n"
    )
    return list(
        session.execute(
            text(sql), {"t": tenant_id, "v": solution_version_id, "after": after, "n": limit}
        )
        .mappings()
        .all()
    )


def withdraw_release(session: Session, release_id: UUID, *, tenant_id: UUID) -> bool:
    """Withdraw a sealed release; False if it is not sealed (any more)."""
    return (
        session.execute(
            text(
                "UPDATE shifde.releases SET status = 'withdrawn' "
                "WHERE tenant_id = :t AND id = :id AND status = 'sealed' RETURNING id"
            ),
            {"t": tenant_id, "id": release_id},
        ).first()
        is not None
    )


# ------------------------------------------------------------------ evaluation datasets
def get_dataset_placement(session: Session, dataset_id: UUID, *, tenant_id: UUID) -> Row | None:
    """The evaluation dataset `dataset_id` of `tenant_id`: its id, workspace, project and
    digest only (never its cases)."""
    return (
        session.execute(
            text(
                "SELECT id, tenant_id, workspace_id, project_id, digest "
                "FROM shifde.evaluation_datasets WHERE tenant_id = :t AND id = :id"
            ),
            {"t": tenant_id, "id": dataset_id},
        )
        .mappings()
        .first()
    )
