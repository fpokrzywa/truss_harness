"""SQL for connections, their test/discovery history and egress policies (RLS tenant context).

Every read and write of a client row is keyed by `tenant_id` as well as by row-level security
(DECISIONS B-1503, the B-802 pattern): an owner or maintenance session, or a session whose context
names the wrong client, still cannot reach another client's connection, test, discovery or egress
policy through this module. Egress lookups take `tenant_id=None` for the installation default only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.errors import ConnectionTestBusy

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

CONNECTION_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, name, connection_type, adapter_key, vendor_id, "
    "config, auth_method, secret_ref_id, egress_policy_id, access_mode, status, health_status, "
    "last_tested_at, row_version, created_at, updated_at"
)
CONNECTION_UPDATABLE = frozenset(
    {
        "name",
        "vendor_id",
        "config",
        "auth_method",
        "secret_ref_id",
        "egress_policy_id",
        "access_mode",
        "status",
        "health_status",
        "last_tested_at",
    }
)
EGRESS_COLUMNS = (
    "id, tenant_id, name, is_default, allowed_schemes, allowed_ports, allowed_hosts, "
    "allowed_cidrs, allow_private, allow_redirects, max_response_bytes, timeout_seconds, "
    "row_version, created_at, updated_at"
)
EGRESS_UPDATABLE = frozenset(
    {
        "name",
        "is_default",
        "allowed_schemes",
        "allowed_ports",
        "allowed_hosts",
        "allowed_cidrs",
        "allow_private",
        "allow_redirects",
        "max_response_bytes",
        "timeout_seconds",
    }
)


def _update_owned(
    session: Session,
    table: str,
    columns: str,
    updatable: frozenset[str],
    row_id: UUID,
    tenant_id: UUID | None,
    expected_version: int | None,
    values: Mapping[str, Any],
    json_columns: frozenset[str] = frozenset(),
) -> Row | None:
    """Optimistic-locking ``UPDATE ... RETURNING`` keyed by id AND owning client (`tenant_id`
    None = the installation row). `table`, `columns` and `updatable` are module constants; a value
    key outside `updatable` is refused before any SQL is built; every value is a bound parameter.
    """
    sets = ["row_version = row_version + 1"]
    params: dict[str, Any] = {"id": row_id, "t": tenant_id, "expected": expected_version}
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
        "AND tenant_id IS NOT DISTINCT FROM CAST(:t AS uuid) "
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {columns}"
    )
    return session.execute(text(sql), params).mappings().first()


# ------------------------------------------------------------------ connections
def get_connection(session: Session, connection_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {CONNECTION_COLUMNS} FROM shifde.connections "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": connection_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_connections(
    session: Session,
    *,
    tenant_id: UUID,
    connection_type: str | None,
    status: str | None,
    workspace_id: UUID | None,
    project_id: UUID | None,
    tenant_wide: bool,
    visible_workspaces: Sequence[UUID],
    visible_projects: Sequence[UUID],
    after: UUID | None,
    limit: int,
) -> list[Row]:
    sql = (
        f"SELECT {CONNECTION_COLUMNS} FROM shifde.connections WHERE tenant_id = :t "
        "AND (CAST(:type AS text) IS NULL OR connection_type = :type) "
        "AND (CAST(:status AS text) IS NULL OR status = :status) "
        "AND (CAST(:workspace AS uuid) IS NULL OR workspace_id = :workspace) "
        "AND (CAST(:project AS uuid) IS NULL OR project_id = :project) "
        "AND (CAST(:after AS uuid) IS NULL OR id > :after) "
        "AND (:wide OR workspace_id = ANY(CAST(:vws AS uuid[])) "
        "     OR project_id = ANY(CAST(:vps AS uuid[]))) "
        "ORDER BY id LIMIT :n"
    )
    return list(
        session.execute(
            text(sql),
            {
                "t": tenant_id,
                "type": connection_type,
                "status": status,
                "workspace": workspace_id,
                "project": project_id,
                "after": after,
                "wide": tenant_wide,
                "vws": list(visible_workspaces),
                "vps": list(visible_projects),
                "n": limit,
            },
        )
        .mappings()
        .all()
    )


def insert_connection(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, workspace_id, project_id, name, "
            "connection_type, adapter_key, vendor_id, config, auth_method, secret_ref_id, "
            "egress_policy_id, access_mode, status, created_by) VALUES (:id, :tenant_id, "
            ":workspace_id, :project_id, :name, :connection_type, :adapter_key, :vendor_id, "
            "CAST(:config AS jsonb), :auth_method, :secret_ref_id, :egress_policy_id, "
            ":access_mode, 'draft', :created_by)"
        ),
        {**values, "config": json.dumps(values["config"])},
    )


def update_connection(
    session: Session,
    connection_id: UUID,
    expected_version: int | None,
    values: Mapping[str, Any],
    *,
    tenant_id: UUID,
) -> Row | None:
    return _update_owned(
        session,
        "connections",
        CONNECTION_COLUMNS,
        CONNECTION_UPDATABLE,
        connection_id,
        tenant_id,
        expected_version,
        values,
        frozenset({"config"}),
    )


def record_test_outcome(
    session: Session,
    connection_id: UUID,
    *,
    tenant_id: UUID,
    at: datetime,
    health_status: str,
    status: str | None,
) -> None:
    """Persist the result of a test on the connection (not a user edit: no row_version bump
    semantics are needed beyond the standard increment)."""
    session.execute(
        text(
            "UPDATE shifde.connections SET last_tested_at = :at, health_status = :h, "
            "status = COALESCE(:s, status), row_version = row_version + 1 "
            "WHERE id = :id AND tenant_id = :t"
        ),
        {"at": at, "h": health_status, "s": status, "id": connection_id, "t": tenant_id},
    )


# ------------------------------------------------------------------ tests / discoveries
def insert_test(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.connection_tests (id, tenant_id, connection_id, started_at, "
            "finished_at, duration_ms, outcome, checks, error_class, redacted_message, "
            "config_version, config_hash, tested_by) VALUES (:id, :tenant_id, "
            ":connection_id, :started_at, "
            ":finished_at, :duration_ms, :outcome, CAST(:checks AS jsonb), :error_class, "
            ":redacted_message, :config_version, :config_hash, :tested_by)"
        ),
        {**values, "checks": json.dumps(values["checks"])},
    )


TEST_COLUMNS = (
    "id, connection_id, started_at, finished_at, duration_ms, outcome, checks, error_class, "
    "redacted_message, config_version, config_hash"
)


def list_tests(
    session: Session, connection_id: UUID, after: UUID | None, limit: int, *, tenant_id: UUID
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {TEST_COLUMNS} FROM shifde.connection_tests WHERE connection_id = :c "
                "AND tenant_id = :t "
                "AND (CAST(:after AS uuid) IS NULL OR id < :after) ORDER BY id DESC LIMIT :n"
            ),
            {"c": connection_id, "t": tenant_id, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def latest_test(session: Session, connection_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {TEST_COLUMNS} FROM shifde.connection_tests WHERE connection_id = :c "
                "AND tenant_id = :t ORDER BY finished_at DESC, id DESC LIMIT 1"
            ),
            {"c": connection_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def get_test(session: Session, test_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {TEST_COLUMNS} FROM shifde.connection_tests WHERE id = :id "
                "AND tenant_id = :t"
            ),
            {"id": test_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ test jobs (ARCH M2)
JOB_COLUMNS = "id, kind, status, payload, last_error, created_at"
TEST_JOB_ATTEMPTS = 3


def enqueue_test_job(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    job_id: UUID,
    connection_id: UUID,
    payload: Mapping[str, Any],
) -> Row:
    """Queue the connection's test job in `tenant_id`, or return the one already active (queued or
    leased): the conflict target is the client's own dedupe key (`uq_jobs__dedupe`). The active
    job is looked for `TEST_JOB_ATTEMPTS` times at most; then `ConnectionTestBusy` (409)."""
    dedupe_key = f"connection-test:{connection_id}"
    for _ in range(TEST_JOB_ATTEMPTS):
        session.execute(
            text(
                "INSERT INTO shifde.jobs (id, tenant_id, kind, payload, dedupe_key, run_after) "
                "VALUES (:id, :t, :k, CAST(:p AS jsonb), :dk, now()) "
                "ON CONFLICT (tenant_id, kind, dedupe_key) WHERE status IN ('queued', 'leased') "
                "DO NOTHING"
            ),
            {"id": job_id, "t": tenant_id, "k": kind, "p": json.dumps(payload), "dk": dedupe_key},
        )
        row = _active_test_job(session, tenant_id=tenant_id, kind=kind, dedupe_key=dedupe_key)
        if row is not None:  # else the active job ended between the two statements: queue again
            session.execute(text("SELECT pg_notify('shifde_jobs', '')"))
            return row
    raise ConnectionTestBusy("a test of this connection is starting; retry the request")


def _active_test_job(
    session: Session, *, tenant_id: UUID, kind: str, dedupe_key: str
) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {JOB_COLUMNS} FROM shifde.jobs WHERE tenant_id = :t AND kind = :k "
                "AND dedupe_key = :dk AND status IN ('queued', 'leased')"
            ),
            {"t": tenant_id, "k": kind, "dk": dedupe_key},
        )
        .mappings()
        .first()
    )


def get_test_job(
    session: Session,
    job_id: UUID,
    *,
    tenant_id: UUID,
    kind: str,
    connection_id: UUID | None = None,
) -> Row | None:
    """The `kind` job `job_id` of `tenant_id` (and of `connection_id`, when given)."""
    return (
        session.execute(
            text(
                f"SELECT {JOB_COLUMNS} FROM shifde.jobs WHERE id = :id AND tenant_id = :t "
                "AND kind = :k AND (CAST(:c AS text) IS NULL OR payload->>'connection_id' = :c)"
            ),
            {
                "id": job_id,
                "t": tenant_id,
                "k": kind,
                "c": str(connection_id) if connection_id else None,
            },
        )
        .mappings()
        .first()
    )


def insert_discovery(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.connection_discoveries (id, tenant_id, connection_id, "
            "discovered_at, kind, payload, payload_hash, discovered_by) VALUES (:id, :tenant_id, "
            ":connection_id, :discovered_at, :kind, CAST(:payload AS jsonb), :payload_hash, "
            ":discovered_by)"
        ),
        {**values, "payload": json.dumps(values["payload"])},
    )


def latest_discovery(session: Session, connection_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT id, connection_id, discovered_at, kind, payload FROM "
                "shifde.connection_discoveries WHERE connection_id = :c AND tenant_id = :t "
                "ORDER BY discovered_at DESC, id DESC LIMIT 1"
            ),
            {"c": connection_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ egress policies
def get_egress_policy(session: Session, policy_id: UUID, *, tenant_id: UUID | None) -> Row | None:
    """The policy `policy_id` owned by `tenant_id` (None: an installation row)."""
    return (
        session.execute(
            text(
                f"SELECT {EGRESS_COLUMNS} FROM shifde.egress_policies WHERE id = :id "
                "AND tenant_id IS NOT DISTINCT FROM CAST(:t AS uuid)"
            ),
            {"id": policy_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def context_tenant_id(session: Session) -> UUID | None:
    """The client of the session's transaction as the database sees it (`app.tenant_id`)."""
    value = session.execute(text("SELECT shifde.current_tenant_id()")).scalar_one()
    return None if value is None else UUID(str(value))


def get_default_policy(session: Session, tenant_id: UUID | None) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {EGRESS_COLUMNS} FROM shifde.egress_policies WHERE is_default "
                "AND tenant_id IS NOT DISTINCT FROM CAST(:t AS uuid)"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_egress_policies(
    session: Session, tenant_id: UUID, after: UUID | None, limit: int
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {EGRESS_COLUMNS} FROM shifde.egress_policies WHERE tenant_id = :t "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {"t": tenant_id, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def insert_egress_policy(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.egress_policies (id, tenant_id, name, is_default, allowed_schemes, "
            "allowed_ports, allowed_hosts, allowed_cidrs, allow_private, allow_redirects, "
            "max_response_bytes, timeout_seconds, created_by) VALUES (:id, :tenant_id, :name, "
            ":is_default, :allowed_schemes, :allowed_ports, :allowed_hosts, :allowed_cidrs, "
            ":allow_private, :allow_redirects, :max_response_bytes, :timeout_seconds, :created_by)"
        ),
        values,
    )


def update_egress_policy(
    session: Session,
    policy_id: UUID,
    expected_version: int | None,
    values: Mapping[str, Any],
    *,
    tenant_id: UUID | None,
) -> Row | None:
    return _update_owned(
        session,
        "egress_policies",
        EGRESS_COLUMNS,
        EGRESS_UPDATABLE,
        policy_id,
        tenant_id,
        expected_version,
        values,
    )
