"""SQL for evaluation datasets and evaluation runs (RLS tenant context; sub-project 3 design
sections 4 and 6).

Every read and write is keyed by `tenant_id` as well as by row-level security. Datasets are
immutable (`shifde.evaluation_datasets_guard`); an evaluation run's subject, digests and
thresholds never change, its case runs are recorded once (queued -> running) and an ended run
never changes (`shifde.evaluation_runs_guard`). Release, build and run data come from the
`packaging` and `runtime` services, never from SQL here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

DATASET_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, name, version_no, cases, digest, created_by, "
    "created_at"
)
RUN_COLUMNS = (
    "id, tenant_id, release_id, release_digest, dataset_id, dataset_digest, thresholds, "
    "case_runs, status, status_reason, accuracy, p95_latency_ms, total_cost_usd, results, "
    "requested_by, started_at, finished_at, created_at"
)


def current_tenant_id(session: Session) -> UUID | None:
    """The client in the session's context (`shifde.current_tenant_id()`), None without one."""
    value = session.execute(text("SELECT shifde.current_tenant_id()")).scalar_one()
    return UUID(str(value)) if value is not None else None


# ------------------------------------------------------------------ datasets
def next_dataset_version_no(
    session: Session, project_id: UUID, name: str, *, tenant_id: UUID
) -> int:
    return int(
        session.execute(
            text(
                "SELECT coalesce(max(version_no), 0) + 1 FROM shifde.evaluation_datasets "
                "WHERE tenant_id = :t AND project_id = :p AND name = :n"
            ),
            {"t": tenant_id, "p": project_id, "n": name},
        ).scalar_one()
    )


def insert_dataset(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.evaluation_datasets (id, tenant_id, workspace_id, project_id, "
            "name, version_no, cases, digest, created_by) VALUES (:id, :tenant_id, "
            ":workspace_id, :project_id, :name, :version_no, CAST(:cases AS jsonb), :digest, "
            ":created_by)"
        ),
        {**values, "cases": json.dumps(values["cases"], ensure_ascii=False)},
    )


def get_dataset(session: Session, dataset_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {DATASET_COLUMNS} FROM shifde.evaluation_datasets "  # nosec B608
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"t": tenant_id, "id": dataset_id},
        )
        .mappings()
        .first()
    )


def list_datasets(
    session: Session, project_id: UUID, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[Row]:
    """Newest first (ids are time-ordered); `after` is the last id of the previous page."""
    sql = (
        # columns are module constants; values are bound
        f"SELECT {DATASET_COLUMNS} FROM shifde.evaluation_datasets "  # nosec B608
        "WHERE tenant_id = :t AND project_id = :p "
        "AND (CAST(:after AS uuid) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"
    )
    return list(
        session.execute(text(sql), {"t": tenant_id, "p": project_id, "after": after, "n": limit})
        .mappings()
        .all()
    )


# ------------------------------------------------------------------ evaluation runs
def insert_run(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.evaluation_runs (id, tenant_id, release_id, release_digest, "
            "dataset_id, dataset_digest, thresholds, requested_by) VALUES (:id, :tenant_id, "
            ":release_id, :release_digest, :dataset_id, :dataset_digest, "
            "CAST(:thresholds AS jsonb), :requested_by)"
        ),
        {**values, "thresholds": json.dumps(values["thresholds"])},
    )


def get_run(session: Session, run_id: UUID, *, tenant_id: UUID, lock: bool = False) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {RUN_COLUMNS} FROM shifde.evaluation_runs "  # nosec B608
                "WHERE tenant_id = :t AND id = :id" + (" FOR UPDATE" if lock else "")
            ),
            {"t": tenant_id, "id": run_id},
        )
        .mappings()
        .first()
    )


def list_runs(
    session: Session, release_id: UUID, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[Row]:
    """The release's evaluation runs, newest first; `after` is the last id of the previous page."""
    sql = (
        # columns are module constants; values are bound
        f"SELECT {RUN_COLUMNS} FROM shifde.evaluation_runs "  # nosec B608
        "WHERE tenant_id = :t AND release_id = :r "
        "AND (CAST(:after AS uuid) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"
    )
    return list(
        session.execute(text(sql), {"t": tenant_id, "r": release_id, "after": after, "n": limit})
        .mappings()
        .all()
    )


def running_run_ids(session: Session, release_id: UUID, *, tenant_id: UUID) -> list[UUID]:
    return list(
        session.execute(
            text(
                "SELECT id FROM shifde.evaluation_runs WHERE tenant_id = :t AND release_id = :r "
                "AND status = 'running' ORDER BY id"
            ),
            {"t": tenant_id, "r": release_id},
        ).scalars()
    )


def passing_run(
    session: Session,
    release_id: UUID,
    *,
    tenant_id: UUID,
    release_digest: str,
    dataset_digest: str | None,
) -> Row | None:
    """The newest `passed` run of the release with these digests (none for a null digest)."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {RUN_COLUMNS} FROM shifde.evaluation_runs "  # nosec B608
                "WHERE tenant_id = :t AND release_id = :r AND status = 'passed' "
                "AND release_digest = :rd AND dataset_digest = :dd ORDER BY id DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id, "rd": release_digest, "dd": dataset_digest},
        )
        .mappings()
        .first()
    )


def start_run(
    session: Session, run_id: UUID, case_runs: list[dict[str, str]], *, tenant_id: UUID
) -> None:
    """queued -> running with the started case runs."""
    session.execute(
        text(
            "UPDATE shifde.evaluation_runs SET status = 'running', "
            "case_runs = CAST(:c AS jsonb), started_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status = 'queued'"
        ),
        {"t": tenant_id, "id": run_id, "c": json.dumps(case_runs)},
    )


def finish_run(
    session: Session, run_id: UUID, values: Mapping[str, Any], *, tenant_id: UUID
) -> None:
    """Write the result columns and `finished_at` of a queued or running evaluation run once."""
    session.execute(
        text(
            "UPDATE shifde.evaluation_runs SET status = :status, status_reason = :status_reason, "
            "accuracy = :accuracy, p95_latency_ms = :p95_latency_ms, "
            "total_cost_usd = :total_cost_usd, results = CAST(:results AS jsonb), "
            "finished_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status IN ('queued', 'running')"
        ),
        {
            "status_reason": None,
            "accuracy": None,
            "p95_latency_ms": None,
            "total_cost_usd": None,
            **values,
            "results": json.dumps(values["results"]) if values.get("results") is not None else None,
            "t": tenant_id,
            "id": run_id,
        },
    )
