"""SQL of the ontology module: access policies, principal attributes, query lineage (raw,
parameterized). Every function takes the client's `tenant_id` (second argument) and filters on it
next to row level security; inserts carry it in their values and the composite foreign keys
refuse another client's ontology, version or run (DECISIONS B-2002)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: column values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.sqlutil import update_returning

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

POLICY_COLUMNS = (
    "id, tenant_id, ontology_id, object_type_key, property_keys, subject_type, subject_id, "
    "effect, row_filter, created_by, created_at, updated_at, row_version"
)
POLICY_UPDATABLE = frozenset({"object_type_key", "subject_type", "subject_id", "row_filter"})
LINEAGE_COLUMNS = (
    "id, tenant_id, at, principal_id, principal_type, ontology_id, ontology_version_id, run_id, "
    "channel, object_types, connection_ids, compiled_query_hashes, row_counts, duration_ms, "
    "outcome, error_code"
)


# ------------------------------------------------------------------ policies
def list_policies(session: Session, tenant_id: UUID, ontology_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {POLICY_COLUMNS} FROM shifde.ontology_access_policies "
                "WHERE tenant_id = :t AND ontology_id = :o ORDER BY object_type_key, id"
            ),
            {"t": tenant_id, "o": ontology_id},
        )
        .mappings()
        .all()
    )


def get_policy(session: Session, tenant_id: UUID, ontology_id: UUID, policy_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {POLICY_COLUMNS} FROM shifde.ontology_access_policies "
                "WHERE tenant_id = :t AND ontology_id = :o AND id = :p"
            ),
            {"t": tenant_id, "o": ontology_id, "p": policy_id},
        )
        .mappings()
        .first()
    )


def insert_policy(
    session: Session,
    tenant_id: UUID,
    v: Mapping[str, Any],  # Any: column values
) -> Row:
    return (
        session.execute(
            text(
                "INSERT INTO shifde.ontology_access_policies (id, tenant_id, ontology_id, "
                "object_type_key, property_keys, subject_type, subject_id, effect, row_filter, "
                "created_by) VALUES (:id, :tenant_id, :ontology_id, :object_type_key, "
                "CAST(:property_keys AS text[]), :subject_type, :subject_id, 'allow', "
                f":row_filter, :created_by) RETURNING {POLICY_COLUMNS}"
            ),
            {**v, "tenant_id": tenant_id},
        )
        .mappings()
        .one()
    )


def update_policy(
    session: Session,
    tenant_id: UUID,
    ontology_id: UUID,
    policy_id: UUID,
    expected: int | None,
    values: Mapping[str, Any],  # Any: column values
    property_keys: list[str] | None,
) -> Row | None:
    """Optimistic update of a whole policy of `ontology_id`; None when the policy is not that
    ontology's (ontology review 1 F1; `ontology_id` is never updatable) or `expected` does not
    match."""
    if get_policy(session, tenant_id, ontology_id, policy_id) is None:
        return None
    row = update_returning(
        session,
        "ontology_access_policies",
        POLICY_COLUMNS,
        POLICY_UPDATABLE,
        policy_id,
        expected,
        values,
        tenant_id=tenant_id,
    )
    if row is None:
        return None
    return (
        session.execute(
            text(
                "UPDATE shifde.ontology_access_policies "
                "SET property_keys = CAST(:k AS text[]) WHERE tenant_id = :t AND ontology_id = :o "
                "AND id = :id "
                f"RETURNING {POLICY_COLUMNS}"
            ),
            {"k": property_keys, "id": policy_id, "t": tenant_id, "o": ontology_id},
        )
        .mappings()
        .one()
    )


def delete_policy(session: Session, tenant_id: UUID, ontology_id: UUID, policy_id: UUID) -> bool:
    result = session.execute(
        text(
            "DELETE FROM shifde.ontology_access_policies "
            "WHERE tenant_id = :t AND ontology_id = :o AND id = :p"
        ),
        {"t": tenant_id, "o": ontology_id, "p": policy_id},
    )
    return bool(getattr(result, "rowcount", 0))


# ------------------------------------------------------------------ principal attributes
def get_attributes(
    session: Session, tenant_id: UUID, principal_type: str, principal_id: UUID
) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT principal_type, principal_id, attributes, row_version, updated_at "
                "FROM shifde.ontology_principal_attributes "
                "WHERE tenant_id = :tn AND principal_type = :t AND principal_id = :p"
            ),
            {"tn": tenant_id, "t": principal_type, "p": principal_id},
        )
        .mappings()
        .first()
    )


def list_attributes(session: Session, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT principal_type, principal_id, attributes, row_version, updated_at "
                "FROM shifde.ontology_principal_attributes WHERE tenant_id = :t "
                "ORDER BY principal_id"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .all()
    )


def put_attributes(
    session: Session,
    tenant_id: UUID,
    principal_type: str,
    principal_id: UUID,
    attributes: Mapping[str, Any],  # Any: scalar / list values
    expected: int | None,
    by: UUID,
) -> Row | None:
    """Insert or optimistic-update; None when `expected` does not match the stored version."""
    existing = get_attributes(session, tenant_id, principal_type, principal_id)
    params: dict[str, Any] = {  # Any: bound values
        "t": tenant_id,
        "pt": principal_type,
        "p": principal_id,
        "a": json.dumps(attributes),
        "by": by,
        "e": expected,
    }
    if existing is None:
        if expected not in (None, 0):
            return None
        return (
            session.execute(
                text(
                    "INSERT INTO shifde.ontology_principal_attributes (tenant_id, principal_type, "
                    "principal_id, attributes, updated_by) VALUES (:t, :pt, :p, "
                    "CAST(:a AS jsonb), :by) RETURNING principal_type, principal_id, attributes, "
                    "row_version, updated_at"
                ),
                params,
            )
            .mappings()
            .one()
        )
    return (
        session.execute(
            text(
                "UPDATE shifde.ontology_principal_attributes SET attributes = CAST(:a AS jsonb), "
                "updated_by = :by, updated_at = now(), row_version = row_version + 1 "
                "WHERE tenant_id = :t AND principal_type = :pt AND principal_id = :p "
                "AND (CAST(:e AS int) IS NULL OR row_version = :e) "
                "RETURNING principal_type, principal_id, attributes, row_version, updated_at"
            ),
            params,
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ lineage
def insert_lineage(
    session: Session,
    tenant_id: UUID,
    v: Mapping[str, Any],  # Any: column values
) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.ontology_query_lineage (id, tenant_id, principal_id, "
            "principal_type, ontology_id, ontology_version_id, run_id, channel, object_types, "
            "connection_ids, compiled_query_hashes, row_counts, duration_ms, outcome, "
            "error_code) VALUES (:id, :tenant_id, :principal_id, :principal_type, :ontology_id, "
            ":ontology_version_id, :run_id, :channel, CAST(:object_types AS text[]), "
            "CAST(:connection_ids AS uuid[]), CAST(:compiled_query_hashes AS text[]), "
            "CAST(:row_counts AS jsonb), :duration_ms, :outcome, :error_code)"
        ),
        {**v, "tenant_id": tenant_id, "row_counts": json.dumps(v["row_counts"])},
    )


def list_lineage(
    session: Session,
    tenant_id: UUID,
    ontology_id: UUID,
    run_id: UUID | None,
    after: UUID | None,
    limit: int,
) -> list[Row]:
    """Newest first (UUIDv7 ids are time ordered); `after` is the last id of the previous page."""
    return list(
        session.execute(
            text(
                f"SELECT {LINEAGE_COLUMNS} FROM shifde.ontology_query_lineage "
                "WHERE tenant_id = :t AND ontology_id = :o "
                "AND (CAST(:r AS uuid) IS NULL OR run_id = :r) "
                "AND (CAST(:after AS uuid) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"
            ),
            {"t": tenant_id, "o": ontology_id, "r": run_id, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def get_lineage(session: Session, tenant_id: UUID, lineage_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {LINEAGE_COLUMNS} FROM shifde.ontology_query_lineage "
                "WHERE tenant_id = :t AND id = :i"
            ),
            {"t": tenant_id, "i": lineage_id},
        )
        .mappings()
        .first()
    )
