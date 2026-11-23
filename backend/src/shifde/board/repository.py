"""Board reads (sub-project 7 design 7.1): SELECTs only, over other modules' tables in raw SQL.

Every statement filters `tenant_id` and runs under forced row-level security in the caller's
client. Each function takes lists of ids (`= ANY(...)`), so one board request issues a fixed
number of statements, whatever the number of solutions, releases or targets. A plan with no
release (`release_id` NULL) never joins a release, so it is never shown. The expiry of a sealed
or approved plan is computed here on the database clock; the stored row is left as it is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any  # Any: SQLAlchemy RowMapping keys / column values
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values


def _rows(session: Session, sql: str, params: Mapping[str, Any]) -> list[Row]:
    return list(session.execute(text(sql), dict(params)).mappings().all())


def list_projects(
    session: Session,
    *,
    tenant_id: UUID,
    tenant_wide: bool,
    workspace_ids: Sequence[UUID],
    project_ids: Sequence[UUID],
    workspace_id: UUID | None,
    project_id: UUID | None,
    include_archived: bool,
    after: UUID | None,
    limit: int,
) -> list[Row]:
    """Projects with their workspace, visible through (`tenant_wide`, `workspace_ids`,
    `project_ids`), optionally of one workspace or one project, ordered by project id."""
    return _rows(
        session,
        "SELECT p.id, p.slug, p.name, p.workspace_id, w.slug AS workspace_slug, "
        "w.name AS workspace_name FROM shifde.projects p "
        "JOIN shifde.workspaces w ON w.tenant_id = p.tenant_id AND w.id = p.workspace_id "
        "WHERE p.tenant_id = :t "
        "AND (:wide OR p.workspace_id = ANY(CAST(:vws AS uuid[])) "
        "     OR p.id = ANY(CAST(:vps AS uuid[]))) "
        "AND (CAST(:ws AS uuid) IS NULL OR p.workspace_id = :ws) "
        "AND (CAST(:pr AS uuid) IS NULL OR p.id = :pr) "
        "AND (:archived OR p.status = 'active') "
        "AND (CAST(:after AS uuid) IS NULL OR p.id > :after) "
        "ORDER BY p.id LIMIT :n",
        {
            "t": tenant_id,
            "wide": tenant_wide,
            "vws": list(workspace_ids),
            "vps": list(project_ids),
            "ws": workspace_id,
            "pr": project_id,
            "archived": include_archived,
            "after": after,
            "n": limit,
        },
    )


def solutions(
    session: Session, *, tenant_id: UUID, project_ids: Sequence[UUID], per_project: int
) -> list[Row]:
    """The first `per_project` solutions of each project by id, with the highest version number
    (NULL without a version)."""
    return _rows(
        session,
        "SELECT s.id, s.project_id, s.name, s.status, "
        "(SELECT max(v.version_no) FROM shifde.solution_versions v "
        " WHERE v.tenant_id = s.tenant_id AND v.solution_id = s.id) AS latest_version_no "
        "FROM (SELECT x.*, row_number() OVER (PARTITION BY x.project_id ORDER BY x.id) AS rn "
        "      FROM shifde.solutions x WHERE x.tenant_id = :t "
        "      AND x.project_id = ANY(CAST(:ps AS uuid[]))) s "
        "WHERE s.rn <= :n ORDER BY s.project_id, s.id",
        {"t": tenant_id, "ps": list(project_ids), "n": per_project},
    )


def releases(
    session: Session, *, tenant_id: UUID, solution_ids: Sequence[UUID], per_solution: int
) -> list[Row]:
    """The newest `per_solution` releases of each solution (`sealed_at`, then id, descending)."""
    return _rows(
        session,
        "SELECT x.id, x.solution_id, x.version_no, x.digest, x.status, x.sealed_at FROM ("
        "  SELECT r.id, v.solution_id, v.version_no, r.digest, r.status, r.sealed_at, "
        "  row_number() OVER (PARTITION BY v.solution_id "
        "                     ORDER BY r.sealed_at DESC, r.id DESC) AS rn "
        "  FROM shifde.releases r JOIN shifde.solution_versions v "
        "  ON v.tenant_id = r.tenant_id AND v.id = r.solution_version_id "
        "  WHERE r.tenant_id = :t AND v.solution_id = ANY(CAST(:ss AS uuid[]))) x "
        "WHERE x.rn <= :n ORDER BY x.solution_id, x.sealed_at DESC, x.id DESC",
        {"t": tenant_id, "ss": list(solution_ids), "n": per_solution},
    )


def latest_evaluations(
    session: Session, *, tenant_id: UUID, release_ids: Sequence[UUID]
) -> list[Row]:
    """Each release's latest evaluation run (`created_at`, then id)."""
    return _rows(
        session,
        "SELECT DISTINCT ON (e.release_id) e.release_id, e.id, e.status "
        "FROM shifde.evaluation_runs e WHERE e.tenant_id = :t "
        "AND e.release_id = ANY(CAST(:rs AS uuid[])) "
        "ORDER BY e.release_id, e.created_at DESC, e.id DESC",
        {"t": tenant_id, "rs": list(release_ids)},
    )


def targets(
    session: Session,
    *,
    tenant_id: UUID,
    project_ids: Sequence[UUID],
    workspace_ids: Sequence[UUID],
) -> list[Row]:
    """Every target (any status) of the projects, or of the workspaces with no project, by id."""
    return _rows(
        session,
        "SELECT t.id, t.workspace_id, t.project_id, t.name, t.environment, t.platform_kind "
        "FROM shifde.delivery_targets t WHERE t.tenant_id = :t "
        "AND (t.project_id = ANY(CAST(:ps AS uuid[])) "
        "     OR (t.project_id IS NULL AND t.workspace_id = ANY(CAST(:ws AS uuid[])))) "
        "ORDER BY t.id",
        {"t": tenant_id, "ps": list(project_ids), "ws": list(workspace_ids)},
    )


def current_deployments(
    session: Session, *, tenant_id: UUID, solution_ids: Sequence[UUID], target_ids: Sequence[UUID]
) -> list[Row]:
    """Per (solution, target): the latest `verified` deployment of a release of the solution
    (`updated_at`, then id); `verified_at` is its `updated_at`."""
    return _rows(
        session,
        "SELECT DISTINCT ON (v.solution_id, d.target_id) v.solution_id, d.target_id, "
        "d.id AS deployment_id, p.release_id, v.version_no, "
        "COALESCE(d.updated_at, d.created_at) AS verified_at "
        "FROM shifde.delivery_deployments d "
        "JOIN shifde.delivery_plans p ON p.tenant_id = d.tenant_id AND p.id = d.plan_id "
        "JOIN shifde.releases r ON r.tenant_id = p.tenant_id AND r.id = p.release_id "
        "JOIN shifde.solution_versions v "
        "ON v.tenant_id = r.tenant_id AND v.id = r.solution_version_id "
        "WHERE d.tenant_id = :t AND d.status = 'verified' "
        "AND v.solution_id = ANY(CAST(:ss AS uuid[])) AND d.target_id = ANY(CAST(:gs AS uuid[])) "
        "ORDER BY v.solution_id, d.target_id, COALESCE(d.updated_at, d.created_at) DESC, "
        "d.id DESC",
        {"t": tenant_id, "ss": list(solution_ids), "gs": list(target_ids)},
    )


def last_attempts(
    session: Session, *, tenant_id: UUID, solution_ids: Sequence[UUID], target_ids: Sequence[UUID]
) -> list[Row]:
    """Per (solution, target): the latest deployment of any status (`created_at`, then id)."""
    return _rows(
        session,
        "SELECT DISTINCT ON (v.solution_id, d.target_id) v.solution_id, d.target_id, "
        "d.id AS deployment_id, p.release_id, d.status, d.status_reason, d.updated_at "
        "FROM shifde.delivery_deployments d "
        "JOIN shifde.delivery_plans p ON p.tenant_id = d.tenant_id AND p.id = d.plan_id "
        "JOIN shifde.releases r ON r.tenant_id = p.tenant_id AND r.id = p.release_id "
        "JOIN shifde.solution_versions v "
        "ON v.tenant_id = r.tenant_id AND v.id = r.solution_version_id "
        "WHERE d.tenant_id = :t "
        "AND v.solution_id = ANY(CAST(:ss AS uuid[])) AND d.target_id = ANY(CAST(:gs AS uuid[])) "
        "ORDER BY v.solution_id, d.target_id, d.created_at DESC, d.id DESC",
        {"t": tenant_id, "ss": list(solution_ids), "gs": list(target_ids)},
    )


def open_plans(
    session: Session, *, tenant_id: UUID, solution_ids: Sequence[UUID], target_ids: Sequence[UUID]
) -> list[Row]:
    """Per (solution, target): the latest plan (`created_at`, then id) in `requested`,
    `planning`, `sealed` or `approved` that no deployment names; a sealed or approved plan whose
    `expires_at` has passed on the database clock reads `expired`."""
    return _rows(
        session,
        "SELECT DISTINCT ON (v.solution_id, p.target_id) v.solution_id, p.target_id, "
        "p.id AS plan_id, p.release_id, p.expires_at, "
        "CASE WHEN p.status IN ('sealed', 'approved') AND p.expires_at <= now() "
        "THEN 'expired' ELSE p.status END AS status "
        "FROM shifde.delivery_plans p "
        "JOIN shifde.releases r ON r.tenant_id = p.tenant_id AND r.id = p.release_id "
        "JOIN shifde.solution_versions v "
        "ON v.tenant_id = r.tenant_id AND v.id = r.solution_version_id "
        "WHERE p.tenant_id = :t "
        "AND p.status IN ('requested', 'planning', 'sealed', 'approved') "
        "AND NOT EXISTS (SELECT 1 FROM shifde.delivery_deployments d "
        "                WHERE d.tenant_id = p.tenant_id AND d.plan_id = p.id) "
        "AND v.solution_id = ANY(CAST(:ss AS uuid[])) "
        "AND p.target_id = ANY(CAST(:gs AS uuid[])) "
        "ORDER BY v.solution_id, p.target_id, p.created_at DESC, p.id DESC",
        {"t": tenant_id, "ss": list(solution_ids), "gs": list(target_ids)},
    )
