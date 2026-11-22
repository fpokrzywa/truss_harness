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
# a connection with the outcome of its latest test by `started_at` (null: never tested), one
# index lookup per row through `ix_connection_tests__connection`, no per-row query
CONNECTION_SELECT = (
    "SELECT c.id, c.tenant_id, c.workspace_id, c.project_id, c.name, c.connection_type, "
    "c.adapter_key, c.vendor_id, c.config, c.auth_method, c.secret_ref_id, c.egress_policy_id, "
    "c.access_mode, c.status, c.health_status, c.last_tested_at, c.row_version, c.created_at, "
    "c.updated_at, lt.outcome AS last_test_outcome FROM shifde.connections c "
    "LEFT JOIN LATERAL (SELECT t.outcome FROM shifde.connection_tests t "
    "WHERE t.tenant_id = c.tenant_id AND t.connection_id = c.id "
    "ORDER BY t.started_at DESC LIMIT 1) lt ON true"
)
# `workspace`/`project` filter: that exact scope, or (`include_shared`) the project's own rows plus
# the workspace-wide and client-wide ones it shares
SCOPE_EXACT = (
    "AND (CAST(:workspace AS uuid) IS NULL OR c.workspace_id = :workspace) "
    "AND (CAST(:project AS uuid) IS NULL OR c.project_id = :project) "
)
SCOPE_SHARED = (
    "AND ((CAST(:project AS uuid) IS NOT NULL AND c.project_id = :project) "
    "     OR (c.workspace_id = :workspace AND c.project_id IS NULL) "
    "     OR c.workspace_id IS NULL) "
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
        # SET names checked against the code allowlist; values are bound
        f"UPDATE shifde.{table} SET {', '.join(sets)} WHERE id = :id "  # nosec B608
        "AND tenant_id IS NOT DISTINCT FROM CAST(:t AS uuid) "
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {columns}"
    )
    return session.execute(text(sql), params).mappings().first()


# ------------------------------------------------------------------ connections
def get_connection(session: Session, connection_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"{CONNECTION_SELECT} "  # nosec B608
                "WHERE c.id = :id AND c.tenant_id = :t"
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
    include_shared: bool,
    tenant_wide: bool,
    visible_workspaces: Sequence[UUID],
    visible_projects: Sequence[UUID],
    after: UUID | None,
    limit: int,
) -> list[Row]:
    sql = (
        # columns are module constants; values are bound
        f"{CONNECTION_SELECT} WHERE c.tenant_id = :t "  # nosec B608
        "AND (CAST(:type AS text) IS NULL OR c.connection_type = :type) "
        "AND (CAST(:status AS text) IS NULL OR c.status = :status) "
        + (SCOPE_SHARED if include_shared else SCOPE_EXACT)
        + "AND (CAST(:after AS uuid) IS NULL OR c.id > :after) "
        "AND (:wide OR c.workspace_id = ANY(CAST(:vws AS uuid[])) "
        "     OR c.project_id = ANY(CAST(:vps AS uuid[]))) "
        "ORDER BY c.id LIMIT :n"
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
                # columns are module constants; values are bound
                f"SELECT {TEST_COLUMNS} FROM shifde.connection_tests WHERE "  # nosec B608
                "connection_id = :c "
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
                # columns are module constants; values are bound
                f"SELECT {TEST_COLUMNS} FROM shifde.connection_tests WHERE "  # nosec B608
                "connection_id = :c "
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
                # columns are module constants; values are bound
                f"SELECT {TEST_COLUMNS} FROM shifde.connection_tests WHERE id = :id "  # nosec B608
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
                # columns are module constants; values are bound
                f"SELECT {JOB_COLUMNS} FROM shifde.jobs WHERE tenant_id = :t AND "  # nosec B608
                "kind = :k "
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
                # columns are module constants; values are bound
                f"SELECT {JOB_COLUMNS} FROM shifde.jobs WHERE id = :id AND tenant_id "  # nosec B608
                "= :t "
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
                # columns are module constants; values are bound
                f"SELECT {EGRESS_COLUMNS} FROM shifde.egress_policies WHERE id = :id "  # nosec B608
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
                # columns are module constants; values are bound
                f"SELECT {EGRESS_COLUMNS} FROM shifde.egress_policies WHERE "  # nosec B608
                "is_default "
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
                # columns are module constants; values are bound
                f"SELECT {EGRESS_COLUMNS} FROM shifde.egress_policies WHERE "  # nosec B608
                "tenant_id = :t "
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
