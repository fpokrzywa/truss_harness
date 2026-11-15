"""SQL for artifacts, versions, pins, publish requests and prompt test runs (raw, parameterized)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any  # Any: column values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.core.sqlutil import update_returning

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

ARTIFACT_COLUMNS = (
    "id, tenant_id, kind, workspace_id, project_id, key, name, description, owner_principal_id, "
    "visibility, status, latest_published_version_id, row_version, created_at, updated_at"
)
ARTIFACT_UPDATABLE = frozenset({"name", "description", "visibility", "status"})
VERSION_COLUMNS = (
    "id, tenant_id, artifact_id, kind, version_no, state, spec, spec_schema_version, spec_hash, "
    "change_note, created_by, created_at, published_by, published_at, approved_by, approval_id, "
    "based_on_version_id, row_version"
)
VERSION_UPDATABLE = frozenset(
    {
        "spec",
        "state",
        "spec_hash",
        "change_note",
        "published_by",
        "published_at",
        "approved_by",
        "approval_id",
        "based_on_version_id",
    }
)
REQUEST_COLUMNS = (
    "id, tenant_id, artifact_id, kind, version_id, version_no, change_note, spec_hash, "
    "requested_by, requested_at, status, decided_by, decided_at, comment"
)
RUN_COLUMNS = (
    "id, tenant_id, artifact_id, version_id, model_id, inputs, output, usage, latency_ms, "
    "evaluation, verdict, created_by, created_at"
)


def _like(value: str) -> str:
    """Escape LIKE wildcards of a user-supplied search term."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------------ artifacts
def get_artifact(session: Session, tenant_id: UUID, artifact_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {ARTIFACT_COLUMNS} FROM shifde.artifacts WHERE id = :id AND tenant_id = :t"
            ),
            {"id": artifact_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_artifacts(
    session: Session,
    tenant_id: UUID,
    *,
    kind: str,
    after: UUID | None,
    limit: int,
    scope_type: str | None,
    scope_id: UUID | None,
    status: str | None,
    q: str | None,
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {ARTIFACT_COLUMNS} FROM shifde.artifacts "
                "WHERE tenant_id = :t AND kind = :kind "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) "
                "AND (CAST(:status AS text) IS NULL OR status = :status) "
                "AND (CAST(:q AS text) IS NULL OR key ILIKE :q ESCAPE '\\' "
                "     OR name ILIKE :q ESCAPE '\\') "
                "AND (CAST(:stype AS text) IS NULL "
                "     OR (:stype = 'tenant' AND workspace_id IS NULL AND project_id IS NULL) "
                "     OR (:stype = 'workspace' AND workspace_id = :sid AND project_id IS NULL) "
                "     OR (:stype = 'project' AND project_id = :sid)) "
                "ORDER BY id LIMIT :n"
            ),
            {
                "t": tenant_id,
                "kind": kind,
                "after": after,
                "status": status,
                "q": f"%{_like(q)}%" if q else None,
                "stype": scope_type,
                "sid": scope_id,
                "n": limit,
            },
        )
        .mappings()
        .all()
    )


def insert_artifact(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.artifacts (id, tenant_id, kind, workspace_id, project_id, key, "
            "name, description, owner_principal_id, visibility, created_by) VALUES (:id, "
            ":tenant_id, :kind, :workspace_id, :project_id, :key, :name, :description, "
            ":owner_principal_id, :visibility, :created_by)"
        ),
        values,
    )


def update_artifact(
    session: Session,
    tenant_id: UUID,
    artifact_id: UUID,
    expected: int | None,
    values: Mapping[str, Any],
) -> Row | None:
    return update_returning(
        session,
        "artifacts",
        ARTIFACT_COLUMNS,
        ARTIFACT_UPDATABLE,
        artifact_id,
        expected,
        values,
        tenant_id=tenant_id,
    )


def artifact_extras(
    session: Session, tenant_id: UUID, artifact_ids: Sequence[UUID]
) -> dict[UUID, tuple[int | None, bool]]:
    """{artifact id: (latest published version_no, has an open draft)}."""
    if not artifact_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT a.id, lv.version_no, EXISTS (SELECT 1 FROM shifde.artifact_versions d "
            "WHERE d.tenant_id = a.tenant_id AND d.artifact_id = a.id AND d.state = 'draft') "
            "AS has_draft "
            "FROM shifde.artifacts a LEFT JOIN shifde.artifact_versions lv "
            "ON lv.tenant_id = a.tenant_id AND lv.id = a.latest_published_version_id "
            "WHERE a.tenant_id = :t AND a.id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"t": tenant_id, "ids": list(artifact_ids)},
    ).all()
    return {r[0]: (r[1], bool(r[2])) for r in rows}


def lock_artifact(session: Session, tenant_id: UUID, artifact_id: UUID) -> Row | None:
    """Re-read the artifact FOR NO KEY UPDATE: the first lock of every publish / state /
    rollback / approval path (lock order: artifact row, then version rows). NO KEY UPDATE
    serialises those paths without conflicting with the FOR KEY SHARE locks taken by FK checks of
    rows inserted under the artifact (new drafts, test runs, publish requests), which would
    otherwise allow a lock-order deadlock with a concurrent draft creation."""
    return (
        session.execute(
            text(
                f"SELECT {ARTIFACT_COLUMNS} FROM shifde.artifacts "
                "WHERE id = :id AND tenant_id = :t FOR NO KEY UPDATE"
            ),
            {"id": artifact_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def refresh_latest_published(session: Session, tenant_id: UUID, artifact_id: UUID) -> None:
    """Point `latest_published_version_id` at the highest *published* version, computed inside the
    one UPDATE (never from a value read earlier)."""
    session.execute(
        text(
            "UPDATE shifde.artifacts SET latest_published_version_id = (SELECT id FROM "
            "shifde.artifact_versions WHERE tenant_id = :t AND artifact_id = :id "
            "AND state = 'published' ORDER BY version_no DESC LIMIT 1) "
            "WHERE id = :id AND tenant_id = :t"
        ),
        {"id": artifact_id, "t": tenant_id},
    )


# ------------------------------------------------------------------ versions
def normalize_json(session: Session, value: Any) -> Any:  # Any: JSON document
    """The value as PostgreSQL's jsonb stores it (numbers normalised), so hashes computed in
    Python equal hashes of what is read back later."""
    return session.execute(text("SELECT CAST(:v AS jsonb)"), {"v": json.dumps(value)}).scalar_one()


def get_version(
    session: Session, tenant_id: UUID, version_id: UUID, *, lock: bool = False
) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {VERSION_COLUMNS} FROM shifde.artifact_versions "
                "WHERE id = :id AND tenant_id = :t" + (" FOR UPDATE" if lock else "")
            ),
            {"id": version_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def get_draft(
    session: Session, tenant_id: UUID, artifact_id: UUID, *, lock: bool = False
) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {VERSION_COLUMNS} FROM shifde.artifact_versions "
                "WHERE tenant_id = :t AND artifact_id = :a AND state = 'draft'"
                + (" FOR UPDATE" if lock else "")
            ),
            {"t": tenant_id, "a": artifact_id},
        )
        .mappings()
        .first()
    )


def list_versions(session: Session, tenant_id: UUID, artifact_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {VERSION_COLUMNS} FROM shifde.artifact_versions "
                "WHERE tenant_id = :t AND artifact_id = :a ORDER BY version_no DESC"
            ),
            {"t": tenant_id, "a": artifact_id},
        )
        .mappings()
        .all()
    )


def next_version_no(session: Session, tenant_id: UUID, artifact_id: UUID) -> int:
    value = session.execute(
        text(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM shifde.artifact_versions "
            "WHERE tenant_id = :t AND artifact_id = :a"
        ),
        {"t": tenant_id, "a": artifact_id},
    ).scalar_one()
    return int(value)


def insert_version(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.artifact_versions (id, tenant_id, artifact_id, kind, version_no, "
            "state, spec, change_note, created_by, based_on_version_id) VALUES (:id, :tenant_id, "
            ":artifact_id, :kind, :version_no, 'draft', CAST(:spec AS jsonb), NULL, :created_by, "
            ":based_on_version_id)"
        ),
        {**values, "spec": json.dumps(values["spec"])},
    )


def update_version(
    session: Session,
    tenant_id: UUID,
    version_id: UUID,
    expected: int | None,
    values: Mapping[str, Any],
) -> Row | None:
    return update_returning(
        session,
        "artifact_versions",
        VERSION_COLUMNS,
        VERSION_UPDATABLE,
        version_id,
        expected,
        values,
        frozenset({"spec"}),
        tenant_id=tenant_id,
    )


def set_state(session: Session, tenant_id: UUID, version_id: UUID, state: str) -> Row | None:
    return (
        session.execute(
            text(
                "UPDATE shifde.artifact_versions SET state = :s, row_version = row_version + 1 "
                f"WHERE id = :id AND tenant_id = :t RETURNING {VERSION_COLUMNS}"
            ),
            {"s": state, "id": version_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ pins
def insert_pins(
    session: Session, tenant_id: UUID, version_id: UUID, pins: Sequence[tuple[UUID, str]]
) -> None:
    for pinned, role in dict.fromkeys(pins):
        session.execute(
            text(
                "INSERT INTO shifde.artifact_version_pins "
                "(tenant_id, version_id, pinned_version_id, role) "
                "VALUES (:t, :v, :p, :r) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant_id, "v": version_id, "p": pinned, "r": role},
        )


def list_pins(session: Session, tenant_id: UUID, version_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT p.role, v.id AS version_id, v.version_no, v.state, a.id AS artifact_id, "
                "a.kind, a.key, a.name FROM shifde.artifact_version_pins p "
                "JOIN shifde.artifact_versions v ON v.tenant_id = p.tenant_id "
                "AND v.id = p.pinned_version_id "
                "JOIN shifde.artifacts a ON a.tenant_id = v.tenant_id AND a.id = v.artifact_id "
                "WHERE p.tenant_id = :t AND p.version_id = :v "
                "ORDER BY p.role, a.key, v.version_no"
            ),
            {"t": tenant_id, "v": version_id},
        )
        .mappings()
        .all()
    )


def pinned_by_count(session: Session, tenant_id: UUID, version_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.artifact_version_pins "
                "WHERE tenant_id = :t AND pinned_version_id = :v"
            ),
            {"t": tenant_id, "v": version_id},
        ).scalar_one()
    )


# ------------------------------------------------------------------ publish requests
def insert_request(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.publish_requests (id, tenant_id, artifact_id, kind, version_id, "
            "version_no, change_note, spec_hash, requested_by) VALUES (:id, :tenant_id, "
            ":artifact_id, :kind, :version_id, :version_no, :change_note, :spec_hash, "
            ":requested_by)"
        ),
        values,
    )


def get_request(session: Session, tenant_id: UUID, request_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {REQUEST_COLUMNS} FROM shifde.publish_requests "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": request_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def pending_request_for(session: Session, tenant_id: UUID, version_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {REQUEST_COLUMNS} FROM shifde.publish_requests "
                "WHERE tenant_id = :t AND version_id = :v AND status = 'pending'"
            ),
            {"t": tenant_id, "v": version_id},
        )
        .mappings()
        .first()
    )


def list_requests(
    session: Session, tenant_id: UUID, status: str | None, after: UUID | None, limit: int
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {REQUEST_COLUMNS} FROM shifde.publish_requests "
                "WHERE tenant_id = :t AND (CAST(:status AS text) IS NULL OR status = :status) "
                "AND (CAST(:after AS uuid) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"
            ),
            {"t": tenant_id, "status": status, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def decide_request(
    session: Session,
    tenant_id: UUID,
    request_id: UUID,
    status: str,
    decided_by: UUID,
    decided_at: datetime,
    comment: str | None,
) -> Row | None:
    return (
        session.execute(
            text(
                "UPDATE shifde.publish_requests SET status = :s, decided_by = :b, "
                "decided_at = :at, comment = :c WHERE id = :id AND tenant_id = :t "
                "AND status = 'pending' "
                f"RETURNING {REQUEST_COLUMNS}"
            ),
            {
                "s": status,
                "b": decided_by,
                "at": decided_at,
                "c": comment,
                "id": request_id,
                "t": tenant_id,
            },
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ prompt test runs
def insert_run(session: Session, values: Mapping[str, Any]) -> Row:
    row = (
        session.execute(
            text(
                "INSERT INTO shifde.prompt_test_runs (id, tenant_id, artifact_id, version_id, "
                "model_id, inputs, output, usage, latency_ms, evaluation, verdict, created_by) "
                "VALUES (:id, :tenant_id, :artifact_id, :version_id, :model_id, "
                "CAST(:inputs AS jsonb), :output, CAST(:usage AS jsonb), :latency_ms, "
                f"CAST(:evaluation AS jsonb), :verdict, :created_by) RETURNING {RUN_COLUMNS}"
            ),
            {
                **values,
                "inputs": json.dumps(values["inputs"]),
                "usage": json.dumps(values["usage"]),
                "evaluation": json.dumps(values["evaluation"]),
            },
        )
        .mappings()
        .one()
    )
    return row


def list_runs(
    session: Session, tenant_id: UUID, artifact_id: UUID, after: UUID | None, limit: int
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {RUN_COLUMNS} FROM shifde.prompt_test_runs WHERE tenant_id = :t "
                "AND artifact_id = :a "
                "AND (CAST(:after AS uuid) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"
            ),
            {"t": tenant_id, "a": artifact_id, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


# ------------------------------------------------------------------ pipelines (P6)
def insert_pipeline_refs(
    session: Session,
    tenant_id: UUID,
    version_id: UUID,
    refs: Sequence[tuple[str, str, UUID]],
) -> None:
    """Typed, composite-FK-backed references of a published pipeline version."""
    column = {
        "connection": "connection_id",
        "model": "model_id",
        "agent_version": "ref_version_id",
        "prompt_version": "ref_version_id",
        "tool_version": "ref_version_id",
        "ontology_version": "ref_version_id",
    }
    for node_id, ref_type, ref_id in dict.fromkeys(refs):
        col = column[ref_type]  # constant names only (never user input)
        session.execute(
            text(
                "INSERT INTO shifde.pipeline_version_refs "
                f"(tenant_id, pipeline_version_id, node_id, ref_type, ref_id, {col}) "
                "VALUES (:t, :v, :n, :rt, :rid, :rid) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant_id, "v": version_id, "n": node_id, "rt": ref_type, "rid": ref_id},
        )


def list_pipeline_refs(session: Session, tenant_id: UUID, version_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT node_id, ref_type, ref_id FROM shifde.pipeline_version_refs "
                "WHERE tenant_id = :t AND pipeline_version_id = :v "
                "ORDER BY node_id, ref_type, ref_id"
            ),
            {"t": tenant_id, "v": version_id},
        )
        .mappings()
        .all()
    )


def insert_validation_report(
    session: Session,
    tenant_id: UUID,
    version_id: UUID,
    report: Mapping[str, Any],
    topo_order: Sequence[str],
) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.pipeline_validation_reports (tenant_id, version_id, report, "
            "topo_order) VALUES (:t, :v, CAST(:r AS jsonb), CAST(:o AS jsonb)) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "t": tenant_id,
            "v": version_id,
            "r": json.dumps(dict(report)),
            "o": json.dumps(list(topo_order)),
        },
    )


def get_validation_report(session: Session, tenant_id: UUID, version_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT report, topo_order FROM shifde.pipeline_validation_reports "
                "WHERE tenant_id = :t AND version_id = :v"
            ),
            {"t": tenant_id, "v": version_id},
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ ontology mappings (P7)
MAPPING_COLUMNS = (
    "id, tenant_id, ontology_version_id, target_kind, target_key, connection_id, relation_schema, "
    "relation_name, column_map, key_map, filters, status, created_by, created_at, updated_at, "
    "row_version"
)


def list_mappings(session: Session, tenant_id: UUID, version_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {MAPPING_COLUMNS} FROM shifde.ontology_source_mappings "
                "WHERE tenant_id = :t AND ontology_version_id = :v ORDER BY target_kind, target_key"
            ),
            {"t": tenant_id, "v": version_id},
        )
        .mappings()
        .all()
    )


def get_mapping(
    session: Session, tenant_id: UUID, version_id: UUID, kind: str, key: str
) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {MAPPING_COLUMNS} FROM shifde.ontology_source_mappings "
                "WHERE tenant_id = :tn AND ontology_version_id = :v AND target_kind = :k "
                "AND target_key = :t"
            ),
            {"tn": tenant_id, "v": version_id, "k": kind, "t": key},
        )
        .mappings()
        .first()
    )


def upsert_mapping(
    session: Session, tenant_id: UUID, values: Mapping[str, Any]
) -> Row:  # Any: column values
    """Insert or replace the draft mapping of a target. The DB trigger refuses anything that is
    not a draft version's mapping. The insert names a version of `tenant_id` only (EXISTS guard):
    another client's version id and an unknown id give the same outcome (no row, `NoResultFound`),
    and the conflict update never touches another client's row (B-1905)."""
    return (
        session.execute(
            text(
                "INSERT INTO shifde.ontology_source_mappings (id, tenant_id, ontology_version_id, "
                "target_kind, target_key, connection_id, relation_schema, relation_name, "
                "column_map, key_map, filters, status, created_by) SELECT :id, :tenant_id, "
                ":ontology_version_id, :target_kind, :target_key, :connection_id, "
                ":relation_schema, :relation_name, CAST(:column_map AS jsonb), "
                "CAST(:key_map AS jsonb), CAST(:filters AS jsonb), :status, :created_by "
                "WHERE EXISTS (SELECT 1 FROM shifde.artifact_versions "
                "WHERE tenant_id = :tenant_id AND id = :ontology_version_id) "
                "ON CONFLICT (ontology_version_id, target_kind, target_key) DO UPDATE SET "
                "connection_id = EXCLUDED.connection_id, "
                "relation_schema = EXCLUDED.relation_schema, "
                "relation_name = EXCLUDED.relation_name, column_map = EXCLUDED.column_map, "
                "key_map = EXCLUDED.key_map, filters = EXCLUDED.filters, "
                "status = EXCLUDED.status, updated_at = now(), "
                "row_version = shifde.ontology_source_mappings.row_version + 1 "
                "WHERE shifde.ontology_source_mappings.tenant_id = :tenant_id "
                f"RETURNING {MAPPING_COLUMNS}"
            ),
            {
                **values,
                "tenant_id": tenant_id,
                "column_map": json.dumps(values["column_map"]),
                "key_map": json.dumps(values["key_map"]) if values["key_map"] is not None else None,
                "filters": json.dumps(values["filters"]),
            },
        )
        .mappings()
        .one()
    )


def set_mapping_status(session: Session, tenant_id: UUID, mapping_id: UUID, status: str) -> None:
    session.execute(
        text(
            "UPDATE shifde.ontology_source_mappings SET status = :s, updated_at = now(), "
            "row_version = row_version + 1 WHERE id = :id AND tenant_id = :t"
        ),
        {"s": status, "id": mapping_id, "t": tenant_id},
    )


def delete_mapping(
    session: Session, tenant_id: UUID, version_id: UUID, kind: str, key: str
) -> bool:
    result = session.execute(
        text(
            "DELETE FROM shifde.ontology_source_mappings "
            "WHERE tenant_id = :tn AND ontology_version_id = :v AND target_kind = :k "
            "AND target_key = :t"
        ),
        {"tn": tenant_id, "v": version_id, "k": kind, "t": key},
    )
    return bool(getattr(result, "rowcount", 0))


def delete_mappings_of(session: Session, tenant_id: UUID, version_id: UUID) -> None:
    session.execute(
        text(
            "DELETE FROM shifde.ontology_source_mappings "
            "WHERE tenant_id = :t AND ontology_version_id = :v"
        ),
        {"t": tenant_id, "v": version_id},
    )


def copy_mappings(
    session: Session, tenant_id: UUID, source_version: UUID, target_version: UUID, by: UUID
) -> int:
    """Copy a version's mappings to the (draft) `target_version` (rollback)."""
    rows = list_mappings(session, tenant_id, source_version)
    for r in rows:
        upsert_mapping(
            session,
            tenant_id,
            {
                "id": uuid7(),
                "tenant_id": tenant_id,
                "ontology_version_id": target_version,
                "target_kind": r["target_kind"],
                "target_key": r["target_key"],
                "connection_id": r["connection_id"],
                "relation_schema": r["relation_schema"],
                "relation_name": r["relation_name"],
                "column_map": r["column_map"],
                "key_map": r["key_map"],
                "filters": r["filters"],
                "status": "unchecked",
                "created_by": by,
            },
        )
    return len(rows)


def insert_ontology_info(
    session: Session, tenant_id: UUID, version_id: UUID, mappings_hash: str, report: Any
) -> None:  # Any: JSON report
    session.execute(
        text(
            "INSERT INTO shifde.ontology_version_info (tenant_id, version_id, mappings_hash, "
            "report) SELECT :t, :v, :h, CAST(:r AS jsonb) "
            "WHERE EXISTS (SELECT 1 FROM shifde.artifact_versions "
            "WHERE tenant_id = :t AND id = :v)"
        ),
        {"t": tenant_id, "v": version_id, "h": mappings_hash, "r": json.dumps(report)},
    )


def get_ontology_info(session: Session, tenant_id: UUID, version_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT version_id, mappings_hash, report FROM shifde.ontology_version_info "
                "WHERE tenant_id = :t AND version_id = :v"
            ),
            {"t": tenant_id, "v": version_id},
        )
        .mappings()
        .first()
    )
