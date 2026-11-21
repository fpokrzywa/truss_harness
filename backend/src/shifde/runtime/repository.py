"""SQL for the runtime tables (raw, parameterized; table/column names come from code constants).

Ported from SETH 3ba0e06 (RT-3, B-2115..). Every statement that reads or writes a client table
names the client: the functions take the client's `tenant_id` (second argument, or the
`tenant_id` of the row or values they are given) and filter on it next to row-level security
(B-1812, B-1902), so a client naming another client's run, step, job, invocation or approval id
gets the answer of an unknown id. The inserts take no caller-supplied id: the primary keys of the
runtime tables are on `id` alone, so a caller-chosen id could probe another client's rows through
a unique-key collision or a lock wait (B-1905, isolation review 1 F5); the id is a fresh `uuid7`
and callers read it from the returned row. Only the queue and reaper definers
(`shifde_claim_jobs`, `shifde_reap_expired_leases`) and the global `workers` table are
cross-client (B-2103, B-2104).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any  # Any: column values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.ids import uuid7
from shifde.observability import tracing

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

RUN_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, kind, status, state_version, lease_token, "
    "lease_job_id, "
    "initiated_by_principal_id, principal_type, api_key_id, agent_id, agent_version_id, "
    "prompt_version_id, orchestration_id, orchestration_version_id, pipeline_id, "
    "pipeline_version_id, ontology_id, ontology_version_id, action_key, parent_run_id, "
    "root_run_id, "
    "node_id, depth, is_test, input_ref, output_ref, error_class, error_message, waiting_reason, "
    "idempotency_key, request_hash, trace_id, correlation_id, queued_at, started_at, finished_at, "
    "deadline_at, queue_deadline_at, cancel_requested_at, cancel_requested_by, attempt_count, "
    "input_tokens, output_tokens, cached_tokens, model_calls, tool_calls, created_at"
)
STEP_COLUMNS = (
    "id, tenant_id, run_id, seq, attempt, kind, name, node_id, parent_seq, call_index, status, "
    "model_id, tool_version_id, tool_invocation_id, child_run_id, idempotency_key, error_class, "
    "error_message, input_ref, output_ref, usage, started_at, finished_at"
)
APPROVAL_COLUMNS = (
    "id, tenant_id, run_id, workspace_id, project_id, step_seq, call_ref, epoch, reason, "
    "tool_version_id, tool_key, tool_version_no, side_effect_class, arguments, context_summary, "
    "required_permission, requested_by, requested_at, expires_at, status, decision, decided_by, "
    "decided_at, decision_note"
)
INVOCATION_COLUMNS = (
    "id, tenant_id, run_id, call_ref, epoch, tool_version_id, side_effect_class, idempotency_key, "
    "status, attempts, request_hash, response_ref, error_class, started_at, finished_at"
)
RUN_UPDATABLE = frozenset(
    {
        "waiting_reason",
        "error_class",
        "error_message",
        "output_ref",
        "input_ref",
        "started_at",
        "finished_at",
        "cancel_requested_at",
        "cancel_requested_by",
        "lease_token",
        "lease_job_id",
        "attempt_count",
    }
)


def j(value: Any) -> str:  # Any: JSON document
    return json.dumps(value, default=str)


def sha256_hex(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _no_caller_id(table: str, v: Mapping[str, Any]) -> None:  # Any: column values
    """The runtime tables are keyed on `id` alone: the repository chooses every new id (F5)."""
    if "id" in v:
        raise ValueError(f"{table}: the id of a new row is generated here, not given")


# ------------------------------------------------------------------ runs
def insert_run(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    """Insert a run of `v["tenant_id"]` with a new id. A run with no `root_run_id` (or None) is
    its own root."""
    _no_caller_id("runs", v)
    cols = [
        "tenant_id", "workspace_id", "project_id", "kind", "initiated_by_principal_id",
        "principal_type", "api_key_id", "agent_id", "agent_version_id", "prompt_version_id",
        "orchestration_id", "orchestration_version_id", "pipeline_id", "pipeline_version_id",
        "ontology_id", "ontology_version_id", "action_key",
        "parent_run_id", "node_id",
        "depth", "is_test", "input_ref", "idempotency_key", "request_hash", "trace_id",
        "correlation_id", "deadline_at", "queue_deadline_at", "status", "error_class",
        "error_message", "finished_at",
    ]  # fmt: skip
    present = ["id", "root_run_id", *(c for c in cols if c in v)]
    run_id = uuid7()
    params = {c: v[c] for c in present[2:]}
    params["id"] = run_id
    params["root_run_id"] = v.get("root_run_id") or run_id
    sql = (
        # column names come from the code-owned list; values bound
        f"INSERT INTO shifde.runs ({', '.join(present)}) "  # nosec B608
        f"VALUES ({', '.join(':' + c for c in present)}) RETURNING {RUN_COLUMNS}"
    )
    row = session.execute(text(sql), params).mappings().one()
    return row


def create_run(
    session: Session,
    values: Mapping[str, Any],  # Any: column values
    input_content: Any,  # Any: JSON input document
) -> Row:
    """Insert a run, store its input as a run artifact and link it; writes the first event."""
    row = insert_run(session, values)
    tenant_id = row["tenant_id"]
    ref = insert_artifact(session, tenant_id, row["id"], "input", input_content)
    session.execute(
        text("UPDATE shifde.runs SET input_ref = :ref WHERE id = :id AND tenant_id = :t"),
        {"ref": ref, "id": row["id"], "t": tenant_id},
    )
    add_event(session, row, "state_change", None, row["status"], details={"kind": row["kind"]})
    fresh = get_run(session, tenant_id, row["id"])
    assert fresh is not None
    return fresh


def get_run(session: Session, tenant_id: UUID, run_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {RUN_COLUMNS} FROM shifde.runs WHERE id = :id AND "  # nosec B608
                "tenant_id = :t"
            ),
            {"id": run_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def run_by_idempotency(
    session: Session, tenant_id: UUID, principal_id: UUID, key: str
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {RUN_COLUMNS} FROM shifde.runs "  # nosec B608
                "WHERE tenant_id = :t AND initiated_by_principal_id = :p AND idempotency_key = :k"
            ),
            {"t": tenant_id, "p": principal_id, "k": key},
        )
        .mappings()
        .first()
    )


def run_totals(session: Session, tenant_id: UUID, run_ids: Sequence[UUID]) -> dict[UUID, Row]:
    """Usage summed over each run's whole family (itself + descendants)."""
    if not run_ids:
        return {}
    rows = (
        session.execute(
            text(
                "SELECT root_run_id AS id, COALESCE(sum(input_tokens), 0) AS input_tokens, "
                "COALESCE(sum(output_tokens), 0) AS output_tokens, "
                "COALESCE(sum(cached_tokens), 0) AS cached_tokens, "
                "COALESCE(sum(model_calls), 0) AS model_calls, "
                "COALESCE(sum(tool_calls), 0) AS tool_calls FROM shifde.runs "
                "WHERE tenant_id = :t AND root_run_id = ANY(:ids) GROUP BY root_run_id"
            ),
            {"t": tenant_id, "ids": list(run_ids)},
        )
        .mappings()
        .all()
    )
    return {r["id"]: r for r in rows}


def list_runs(
    session: Session,
    tenant_id: UUID,
    *,
    after: UUID | None,
    limit: int,
    status: str | None,
    kind: str | None,
    agent_id: UUID | None,
    workspace_id: UUID | None,
    project_id: UUID | None,
    initiated_by: UUID | None,
    since: datetime | None,
    until: datetime | None,
    top_level_only: bool = True,
    pipeline_id: UUID | None = None,
    outcome_unknown: bool = False,
    scope: tuple[bool, Sequence[UUID], Sequence[UUID]] = (True, (), ()),
) -> list[Row]:
    where = [
        "tenant_id = :t",
        "(CAST(:after AS uuid) IS NULL OR id < :after)",
        "(:wide OR workspace_id = ANY(:wsids) OR project_id = ANY(:pids))",
    ]
    params: dict[str, Any] = {
        "t": tenant_id,
        "after": after,
        "limit": limit,
        "wide": scope[0],
        "wsids": list(scope[1]),
        "pids": list(scope[2]),
    }
    for column, key, value in (
        ("status", "status", status),
        ("kind", "kind", kind),
        ("agent_id", "agent_id", agent_id),
        ("pipeline_id", "pipeline_id", pipeline_id),
        ("workspace_id", "ws", workspace_id),
        ("project_id", "proj", project_id),
        ("initiated_by_principal_id", "by", initiated_by),
    ):
        if value is not None:
            where.append(f"{column} = :{key}")
            params[key] = value
    if since is not None:
        where.append("created_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("created_at <= :until")
        params["until"] = until
    if top_level_only:
        where.append("parent_run_id IS NULL")
    if outcome_unknown:
        where.append(
            "EXISTS (SELECT 1 FROM shifde.approvals a JOIN shifde.runs d ON d.id = a.run_id "
            "AND d.tenant_id = a.tenant_id "
            "WHERE a.tenant_id = :t AND a.reason = 'indeterminate_side_effect' "
            "AND a.status = 'pending' AND (d.id = runs.id OR d.root_run_id = runs.id))"
        )
    sql = (
        # filters are fixed fragments; values are bound
        f"SELECT {RUN_COLUMNS} FROM shifde.runs WHERE {' AND '.join(where)} "  # nosec B608
        "ORDER BY id DESC LIMIT :limit"
    )
    return list(session.execute(text(sql), params).mappings().all())


def fence(session: Session, tenant_id: UUID, run_id: UUID, token: UUID) -> bool:
    """Take the run row lock iff `token` is still the lease holder of a running run. Every
    worker write transaction starts here (lease fencing); FOR NO KEY UPDATE keeps readers and
    foreign-key checks unblocked while serializing state changes."""
    row = session.execute(
        text(
            "SELECT 1 FROM shifde.runs WHERE id = :id AND tenant_id = :t AND lease_token = :tok "
            "AND status = 'running' FOR NO KEY UPDATE"
        ),
        {"id": run_id, "t": tenant_id, "tok": token},
    ).first()
    return row is not None


def run_lease(session: Session, tenant_id: UUID, run_id: UUID) -> Row | None:
    """(status, lease_job_id) of a run: who, if anyone, executes it."""
    return (
        session.execute(
            text("SELECT status, lease_job_id FROM shifde.runs WHERE id = :id AND tenant_id = :t"),
            {"id": run_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def principal_type_of(session: Session, tenant_id: UUID, principal_id: UUID) -> str:
    row = session.execute(
        text("SELECT 1 FROM shifde.service_accounts WHERE id = :p AND tenant_id = :t"),
        {"p": principal_id, "t": tenant_id},
    ).first()
    return "service_account" if row else "user"


def initiator_controllers(session: Session, run: Mapping[Any, Any]) -> set[UUID]:  # Any: run row
    """People who control the principal a run was started as: the owner of a service account and
    the creator of the API key used. They may not approve that run's actions either."""
    out: set[UUID] = set()
    if run["principal_type"] == "service_account":
        row = session.execute(
            text(
                "SELECT owner_principal_id FROM shifde.service_accounts "
                "WHERE id = :p AND tenant_id = :t"
            ),
            {"p": run["initiated_by_principal_id"], "t": run["tenant_id"]},
        ).first()
        if row and row[0]:
            out.add(row[0])
    if run["api_key_id"] is not None:
        row = session.execute(
            text("SELECT created_by FROM shifde.api_keys WHERE id = :k AND tenant_id = :t"),
            {"k": run["api_key_id"], "t": run["tenant_id"]},
        ).first()
        if row and row[0]:
            out.add(row[0])
    return out


def control_state(session: Session, tenant_id: UUID, run_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT status, lease_token, cancel_requested_at, deadline_at "
                "FROM shifde.runs WHERE id = :id AND tenant_id = :t"
            ),
            {"id": run_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def transition(
    session: Session,
    tenant_id: UUID,
    run_id: UUID,
    from_status: str,
    to_status: str,
    *,
    token: UUID | None = None,
    clear_lease: bool = False,
    **values: Any,  # Any: column values (RUN_UPDATABLE)
) -> Row | None:
    """Optimistic state change; None when the run is no longer in `from_status` (or the lease
    token no longer matches). Writes the `state_change` event in the same statement chain."""
    sets = ["status = :to", "state_version = state_version + 1"]
    params: dict[str, Any] = {"id": run_id, "t": tenant_id, "frm": from_status, "to": to_status}
    for column, value in values.items():
        if column not in RUN_UPDATABLE:
            raise ValueError(f"column {column!r} is not updatable")
        sets.append(f"{column} = :{column}")
        params[column] = value
    if clear_lease:
        sets.append("lease_token = NULL")
        sets.append("lease_job_id = NULL")
    where = "id = :id AND tenant_id = :t AND status = :frm"
    if token is not None:
        where += " AND lease_token = :tok"
        params["tok"] = token
    row = (
        session.execute(
            text(
                # SET names checked against the code allowlist; values are bound
                f"UPDATE shifde.runs SET {', '.join(sets)} WHERE {where} RETURNING "  # nosec B608
                f"{RUN_COLUMNS}"
            ),
            params,
        )
        .mappings()
        .first()
    )
    if row is not None:
        detail: dict[str, Any] = {}
        if values.get("error_class"):
            detail["error_class"] = values["error_class"]
        if values.get("waiting_reason"):
            detail["waiting_reason"] = values["waiting_reason"]
        add_event(session, row, "state_change", from_status, to_status, details=detail)
    return row


def add_event(
    session: Session,
    run: Mapping[Any, Any],  # Any: run row
    type_: str,
    from_status: str | None = None,
    to_status: str | None = None,
    *,
    step_id: UUID | None = None,
    details: Mapping[str, Any] | None = None,  # Any: JSON detail
) -> None:
    """Append an event to `run` (a run row: its own `tenant_id` and `id`)."""
    session.execute(
        text(
            "WITH n AS (UPDATE shifde.runs SET event_seq = event_seq + 1 "
            "WHERE id = :run AND tenant_id = :t "
            "RETURNING event_seq) INSERT INTO shifde.run_events "
            "(id, tenant_id, run_id, seq, type, from_status, to_status, step_id, details) "
            "SELECT :id, :t, :run, n.event_seq, :type, :frm, :to, :step, CAST(:d AS jsonb) FROM n"
        ),
        {
            "id": uuid7(),
            "t": run["tenant_id"],
            "run": run["id"],
            "type": type_,
            "frm": from_status,
            "to": to_status,
            "step": step_id,
            "d": j(dict(details or {})),
        },
    )


def add_usage(session: Session, tenant_id: UUID, run_id: UUID, **deltas: int) -> None:
    allowed = {"input_tokens", "output_tokens", "cached_tokens", "model_calls", "tool_calls"}
    sets = []
    for column in deltas:
        if column not in allowed:
            raise ValueError(column)
        sets.append(f"{column} = {column} + :{column}")
    if sets:
        session.execute(
            text(
                # SET names checked against the code allowlist; values are bound
                f"UPDATE shifde.runs SET {', '.join(sets)} WHERE id = :id AND "  # nosec B608
                "tenant_id = :t"
            ),
            {"id": run_id, "t": tenant_id, **deltas},
        )


def list_events(session: Session, tenant_id: UUID, run_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT seq, at, type, from_status, to_status, step_id, details "
                "FROM shifde.run_events WHERE run_id = :id AND tenant_id = :t ORDER BY seq"
            ),
            {"id": run_id, "t": tenant_id},
        )
        .mappings()
        .all()
    )


def tree_classifications(session: Session, tenant_id: UUID, root_run_id: UUID) -> list[str]:
    """The levels of every `data_classification_raised` event of the runs of `tenant_id` whose
    root run is `root_run_id` (the root included)."""
    return [
        str(level)
        for level in session.execute(
            text(
                "SELECT e.details->>'classification' FROM shifde.run_events e "
                "JOIN shifde.runs r ON r.id = e.run_id AND r.tenant_id = e.tenant_id "
                "WHERE e.tenant_id = :t AND r.root_run_id = :root "
                "AND e.type = 'data_classification_raised'"
            ),
            {"t": tenant_id, "root": root_run_id},
        ).scalars()
    ]


def children_of(session: Session, tenant_id: UUID, parent_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {RUN_COLUMNS} FROM shifde.runs WHERE parent_run_id = :p "  # nosec B608
                "AND tenant_id = :t ORDER BY id"
            ),
            {"p": parent_id, "t": tenant_id},
        )
        .mappings()
        .all()
    )


def lock_run(session: Session, tenant_id: UUID, run_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {RUN_COLUMNS} FROM shifde.runs WHERE id = :id AND tenant_id "  # nosec B608
                "= :t "
                "FOR NO KEY UPDATE"
            ),
            {"id": run_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def active_children(session: Session, tenant_id: UUID, parent_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.runs WHERE parent_run_id = :p AND tenant_id = :t "
                "AND status NOT IN ('completed', 'failed', 'cancelled', 'timed_out')"
            ),
            {"p": parent_id, "t": tenant_id},
        ).scalar_one()
    )


# ------------------------------------------------------------------ artifacts
def insert_artifact(
    session: Session,
    tenant_id: UUID,
    run_id: UUID,
    kind: str,
    content: Any,  # Any: JSON content
    step_id: UUID | None = None,
) -> UUID:
    raw = j(content)
    art_id = uuid7()
    session.execute(
        text(
            "INSERT INTO shifde.run_artifacts (id, tenant_id, run_id, step_id, kind, size_bytes, "
            "content, sha256) VALUES (:id, :t, :run, :step, :kind, :size, CAST(:c AS jsonb), :h)"
        ),
        {
            "id": art_id,
            "t": tenant_id,
            "run": run_id,
            "step": step_id,
            "kind": kind,
            "size": len(raw.encode()),
            "c": raw,
            "h": hashlib.sha256(raw.encode()).hexdigest(),
        },
    )
    return art_id


SCRUBBED = {"scrubbed": True}


def scrub_run_content(session: Session, tenant_id: UUID, run_id: UUID) -> None:
    """Capture off (F-12, B-2514): replace the content of every artifact of this run of
    `tenant_id` by `SCRUBBED` (id, kind, step, size and hash stay), clear its tool invocations'
    arguments and replace the arguments of its non-pending approvals by `SCRUBBED` (a pending
    approval keeps them for its decider, #33). Idempotent; another client's run or an unknown id
    scrubs nothing. The database allows these updates only on a run that has ended
    (`run_artifacts_guard`, `tool_invocations_arguments_guard`, `approvals_guard`)."""
    session.execute(
        text(
            "UPDATE shifde.run_artifacts SET content = CAST(:c AS jsonb) "
            "WHERE tenant_id = :t AND run_id = :r AND content <> CAST(:c AS jsonb)"
        ),
        {"t": tenant_id, "r": run_id, "c": j(SCRUBBED)},
    )
    session.execute(
        text(
            "UPDATE shifde.tool_invocations SET arguments = NULL "
            "WHERE tenant_id = :t AND run_id = :r AND arguments IS NOT NULL"
        ),
        {"t": tenant_id, "r": run_id},
    )
    session.execute(
        text(
            "UPDATE shifde.approvals SET arguments = CAST(:c AS jsonb) "
            "WHERE tenant_id = :t AND run_id = :r AND status <> 'pending' "
            "AND arguments <> CAST(:c AS jsonb)"
        ),
        {"t": tenant_id, "r": run_id, "c": j(SCRUBBED)},
    )


def unscrubbed_counts(session: Session, tenant_id: UUID, run_id: UUID) -> tuple[int, int]:
    """(artifacts, tool invocations) of this run of `tenant_id` that `scrub_run_content` would
    change: artifact content not yet `SCRUBBED`, invocation arguments not yet NULL."""
    row = session.execute(
        text(
            "SELECT (SELECT count(*) FROM shifde.run_artifacts WHERE tenant_id = :t "
            "AND run_id = :r AND content <> CAST(:c AS jsonb)), "
            "(SELECT count(*) FROM shifde.tool_invocations WHERE tenant_id = :t "
            "AND run_id = :r AND arguments IS NOT NULL)"
        ),
        {"t": tenant_id, "r": run_id, "c": j(SCRUBBED)},
    ).one()
    return int(row[0]), int(row[1])


def unscrubbed_ended_runs(session: Session, tenant_id: UUID) -> list[UUID]:
    """The terminal runs of `tenant_id` whose root run has ended too and that still hold content
    `scrub_run_content` would change (the capture-off sweep's stragglers)."""
    return list(
        session.execute(
            text(
                "SELECT r.id FROM shifde.runs r JOIN shifde.runs root "
                "ON root.id = r.root_run_id AND root.tenant_id = :t "
                "WHERE r.tenant_id = :t "
                "AND r.status IN ('completed', 'failed', 'cancelled', 'timed_out') "
                "AND root.status IN ('completed', 'failed', 'cancelled', 'timed_out') "
                "AND (EXISTS (SELECT 1 FROM shifde.run_artifacts a WHERE a.tenant_id = :t "
                "AND a.run_id = r.id AND a.content <> CAST(:c AS jsonb)) "
                "OR EXISTS (SELECT 1 FROM shifde.tool_invocations i WHERE i.tenant_id = :t "
                "AND i.run_id = r.id AND i.arguments IS NOT NULL) "
                "OR EXISTS (SELECT 1 FROM shifde.approvals p WHERE p.tenant_id = :t "
                "AND p.run_id = r.id AND p.status <> 'pending' "
                "AND p.arguments <> CAST(:c AS jsonb))) ORDER BY r.id"
            ),
            {"t": tenant_id, "c": j(SCRUBBED)},
        ).scalars()
    )


def ended_runs_of_root(session: Session, tenant_id: UUID, root_run_id: UUID) -> list[UUID]:
    """The terminal runs of `tenant_id` whose root run is `root_run_id` (the root included)."""
    return list(
        session.execute(
            text(
                "SELECT id FROM shifde.runs WHERE tenant_id = :t AND root_run_id = :root "
                "AND status IN ('completed', 'failed', 'cancelled', 'timed_out') ORDER BY id"
            ),
            {"t": tenant_id, "root": root_run_id},
        ).scalars()
    )


def get_artifact(
    session: Session, tenant_id: UUID, artifact_id: UUID | None
) -> Any:  # Any: JSON content
    if artifact_id is None:
        return None
    row = session.execute(
        text("SELECT content FROM shifde.run_artifacts WHERE id = :id AND tenant_id = :t"),
        {"id": artifact_id, "t": tenant_id},
    ).first()
    return None if row is None else row[0]


# ------------------------------------------------------------------ steps
def next_seq(session: Session, tenant_id: UUID, run_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT COALESCE(max(seq), 0) + 1 FROM shifde.run_steps "
                "WHERE run_id = :id AND tenant_id = :t"
            ),
            {"id": run_id, "t": tenant_id},
        ).scalar_one()
    )


def insert_step(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    """Insert a step of `v["tenant_id"]` with a new id."""
    _no_caller_id("run_steps", v)
    cols = [
        "tenant_id", "run_id", "seq", "attempt", "kind", "name", "node_id", "parent_seq",
        "call_index", "status", "model_id", "tool_version_id", "tool_invocation_id",
        "child_run_id", "idempotency_key", "input_ref",
    ]  # fmt: skip
    present = ["id", *(c for c in cols if c in v)]
    params = {c: v[c] for c in present[1:]}
    params["id"] = uuid7()
    sql = (
        # column names come from the code-owned list; values bound
        f"INSERT INTO shifde.run_steps ({', '.join(present)}) "  # nosec B608
        f"VALUES ({', '.join(':' + c for c in present)}) RETURNING {STEP_COLUMNS}"
    )
    return session.execute(text(sql), params).mappings().one()


STEP_UPDATABLE = frozenset(
    {
        "status",
        "tool_invocation_id",
        "child_run_id",
        "error_class",
        "error_message",
        "input_ref",
        "output_ref",
        "finished_at",
    }
)


def update_step(
    session: Session,
    tenant_id: UUID,
    step_id: UUID,
    usage: Mapping[str, Any] | None = None,  # Any: JSON usage
    **values: Any,  # Any: column values (STEP_UPDATABLE)
) -> Row:
    sets: list[str] = []
    params: dict[str, Any] = {"id": step_id, "t": tenant_id}
    for column, value in values.items():
        if column not in STEP_UPDATABLE:
            raise ValueError(column)
        sets.append(f"{column} = :{column}")
        params[column] = value
    if usage is not None:
        sets.append("usage = CAST(:usage AS jsonb)")
        params["usage"] = j(dict(usage))
    row = (
        session.execute(
            text(
                # SET names checked against the code allowlist; values are bound
                f"UPDATE shifde.run_steps SET {', '.join(sets)} WHERE id = :id AND "  # nosec B608
                "tenant_id = :t "
                f"RETURNING {STEP_COLUMNS}"
            ),
            params,
        )
        .mappings()
        .one()
    )
    return row


def list_steps(session: Session, tenant_id: UUID, run_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {STEP_COLUMNS} FROM shifde.run_steps WHERE run_id = :id "  # nosec B608
                "AND tenant_id = :t ORDER BY seq, attempt"
            ),
            {"id": run_id, "t": tenant_id},
        )
        .mappings()
        .all()
    )


# ------------------------------------------------------------------ tool invocations
def get_invocation(session: Session, tenant_id: UUID, key: str) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {INVOCATION_COLUMNS} FROM shifde.tool_invocations "  # nosec B608
                "WHERE tenant_id = :t AND idempotency_key = :k"
            ),
            {"t": tenant_id, "k": key},
        )
        .mappings()
        .first()
    )


def get_invocation_by_id(session: Session, tenant_id: UUID, inv_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {INVOCATION_COLUMNS} FROM shifde.tool_invocations "  # nosec B608
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": inv_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def insert_invocation(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    """Insert a tool invocation of `v["tenant_id"]` with a new id."""
    _no_caller_id("tool_invocations", v)
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "INSERT INTO shifde.tool_invocations (id, tenant_id, run_id, "  # nosec B608
                "call_ref, epoch, "
                "tool_version_id, side_effect_class, idempotency_key, status, request_hash, "
                "arguments) "
                "VALUES (:id, :tenant_id, :run_id, :call_ref, :epoch, :tool_version_id, "
                ":side_effect_class, :idempotency_key, 'started', :request_hash, "
                "CAST(:arguments AS jsonb)) "
                f"RETURNING {INVOCATION_COLUMNS}"
            ),
            {
                **v,
                "id": uuid7(),
                "arguments": None if v.get("arguments") is None else j(dict(v["arguments"])),
            },
        )
        .mappings()
        .one()
    )


def update_invocation(
    session: Session,
    tenant_id: UUID,
    inv_id: UUID,
    status: str,
    **values: Any,  # Any: column values
) -> None:
    sets = ["status = :status"]
    params: dict[str, Any] = {"id": inv_id, "t": tenant_id, "status": status}
    for column, value in values.items():
        if column not in {"attempts", "response_ref", "error_class", "finished_at", "started_at"}:
            raise ValueError(column)
        sets.append(f"{column} = :{column}")
        params[column] = value
    session.execute(
        text(
            # SET names checked against the code allowlist; values are bound
            f"UPDATE shifde.tool_invocations SET {', '.join(sets)} "  # nosec B608
            "WHERE id = :id AND tenant_id = :t"
        ),
        params,
    )


# ------------------------------------------------------------------ approvals
def insert_approval(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    """Insert an approval of `v["tenant_id"]` with a new id."""
    _no_caller_id("approvals", v)
    cols = [
        "id", "tenant_id", "run_id", "workspace_id", "project_id", "step_seq", "call_ref",
        "epoch", "reason", "tool_version_id", "tool_key", "tool_version_no", "side_effect_class",
        "requested_by", "expires_at",
    ]  # fmt: skip
    params = {c: v.get(c) for c in cols}
    params["id"] = uuid7()
    params["arguments"] = j(v.get("arguments", {}))
    params["context_summary"] = j(v.get("context_summary", {}))
    params["required_permission"] = v.get("required_permission") or "approval:decide"
    return (
        session.execute(
            text(
                # column names come from the code-owned list; values bound
                f"INSERT INTO shifde.approvals ({', '.join(cols)}, arguments, "  # nosec B608
                "context_summary, "
                f"required_permission) VALUES ({', '.join(':' + c for c in cols)}, "
                "CAST(:arguments AS jsonb), CAST(:context_summary AS jsonb), :required_permission) "
                f"RETURNING {APPROVAL_COLUMNS}"
            ),
            params,
        )
        .mappings()
        .one()
    )


def get_approval(
    session: Session, tenant_id: UUID, approval_id: UUID, lock: bool = False
) -> Row | None:
    suffix = " FOR NO KEY UPDATE" if lock else ""
    return (
        session.execute(
            text(
                # columns and lock suffix are constants; values are bound
                f"SELECT {APPROVAL_COLUMNS} FROM shifde.approvals "  # nosec B608
                f"WHERE id = :id AND tenant_id = :t{suffix}"
            ),
            {"id": approval_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def find_approval(
    session: Session, tenant_id: UUID, run_id: UUID, call_ref: str, reason: str, epoch: int
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {APPROVAL_COLUMNS} FROM shifde.approvals WHERE tenant_id = "  # nosec B608
                ":t "
                "AND run_id = :r AND call_ref = :c AND reason = :reason AND epoch = :e"
            ),
            {"t": tenant_id, "r": run_id, "c": call_ref, "reason": reason, "e": epoch},
        )
        .mappings()
        .first()
    )


def list_approvals(
    session: Session,
    tenant_id: UUID,
    *,
    statuses: Sequence[str],
    run_id: UUID | None,
    after: UUID | None,
    limit: int,
    scope: tuple[bool, Sequence[UUID], Sequence[UUID]] = (True, (), ()),
    tree_run_id: UUID | None = None,
) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                "WITH RECURSIVE tree(id) AS ("  # nosec B608
                " SELECT id FROM shifde.runs WHERE id = CAST(:tree AS uuid) AND tenant_id = :t"
                " UNION ALL"
                " SELECT c.id FROM shifde.runs c JOIN tree ON c.parent_run_id = tree.id"
                " WHERE c.tenant_id = :t) "
                f"SELECT {APPROVAL_COLUMNS} FROM shifde.approvals "
                "WHERE tenant_id = :t AND status = ANY(:st) "
                "AND (CAST(:run AS uuid) IS NULL OR run_id = :run) "
                "AND (CAST(:tree AS uuid) IS NULL OR run_id IN (SELECT id FROM tree)) "
                "AND (CAST(:after AS uuid) IS NULL OR id < :after) "
                "AND (:wide OR workspace_id = ANY(:wsids) OR project_id = ANY(:pids)) "
                "ORDER BY id DESC LIMIT :limit"
            ),
            {
                "t": tenant_id,
                "st": list(statuses),
                "run": run_id,
                "tree": tree_run_id,
                "after": after,
                "limit": limit,
                "wide": scope[0],
                "wsids": list(scope[1]),
                "pids": list(scope[2]),
            },
        )
        .mappings()
        .all()
    )


def decide_approval(
    session: Session,
    tenant_id: UUID,
    approval_id: UUID,
    status: str,
    decision: str,
    decided_by: UUID,
    note: str | None,
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.approvals SET status = :st, decision = :d, decided_by "  # nosec B608
                "= :by, "
                "decided_at = now(), decision_note = :note WHERE id = :id AND tenant_id = :t "
                f"AND status = 'pending' RETURNING {APPROVAL_COLUMNS}"
            ),
            {
                "id": approval_id,
                "t": tenant_id,
                "st": status,
                "d": decision,
                "by": decided_by,
                "note": note,
            },
        )
        .mappings()
        .first()
    )


def step_id_of(session: Session, tenant_id: UUID, run_id: UUID, seq: int | None) -> UUID | None:
    if seq is None:
        return None
    row = session.execute(
        text(
            "SELECT id FROM shifde.run_steps WHERE tenant_id = :t AND run_id = :r AND seq = :s "
            "ORDER BY attempt DESC LIMIT 1"
        ),
        {"t": tenant_id, "r": run_id, "s": seq},
    ).first()
    return None if row is None else row[0]


def pending_approvals(session: Session, tenant_id: UUID, run_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.approvals WHERE tenant_id = :t AND run_id = :r "
                "AND status = 'pending'"
            ),
            {"t": tenant_id, "r": run_id},
        ).scalar_one()
    )


def unknown_outcome_runs(session: Session, tenant_id: UUID, run_ids: Sequence[UUID]) -> set[UUID]:
    """The runs among `run_ids` that have an open (pending) unknown-outcome decision."""
    if not run_ids:
        return set()
    if (
        session.execute(
            text(
                "SELECT 1 FROM shifde.approvals WHERE tenant_id = :t "
                "AND reason = 'indeterminate_side_effect' AND status = 'pending' LIMIT 1"
            ),
            {"t": tenant_id},
        ).first()
        is None
    ):
        return set()  # the common case costs one indexed probe, not a tree walk per row
    return {
        r[0]
        for r in session.execute(
            text(
                "WITH RECURSIVE t(root_id, id) AS ("
                " SELECT id, id FROM shifde.runs WHERE id = ANY(:ids) AND tenant_id = :tenant"
                " UNION ALL"
                " SELECT t.root_id, c.id FROM shifde.runs c JOIN t ON c.parent_run_id = t.id"
                " WHERE c.tenant_id = :tenant)"
                " SELECT DISTINCT t.root_id FROM t JOIN shifde.approvals a ON a.run_id = t.id"
                " WHERE a.tenant_id = :tenant AND a.reason = 'indeterminate_side_effect'"
                " AND a.status = 'pending'"
            ),
            {"ids": list(run_ids), "tenant": tenant_id},
        )
    }


def settle_ended_run(session: Session, tenant_id: UUID, run_id: UUID) -> int:
    """`shifde.settle_ended_run` (see migration runtime_0001): applies verdicts already given,
    opens missing unknown-outcome decisions, closes what must not outlive an ended run. Called
    only for a run of `tenant_id`; another client's run or an unknown id settles nothing (0)."""
    value = session.execute(
        text(
            "SELECT shifde.settle_ended_run(r.id) FROM shifde.runs r "
            "WHERE r.id = :r AND r.tenant_id = :t"
        ),
        {"r": run_id, "t": tenant_id},
    ).scalar_one_or_none()
    return int(value or 0)


def land_verdicts(session: Session, tenant_id: UUID, run_id: UUID) -> None:
    """`shifde.land_verdicts`: an operator's verdict on a call lands on every earlier unknown
    delivery of that call (see migration runtime_0001). Only for a run of `tenant_id`."""
    session.execute(
        text(
            "SELECT shifde.land_verdicts(r.id) FROM shifde.runs r "
            "WHERE r.id = :r AND r.tenant_id = :t"
        ),
        {"r": run_id, "t": tenant_id},
    )


def verdict_summary(session: Session, tenant_id: UUID, run_id: UUID) -> str:
    """How an ended run's unknown writes were settled, with the cause the run ended for: correct
    for any number of decisions."""
    cause = session.execute(
        text(
            "SELECT COALESCE(details->>'previous_message', details->>'previous_error_class') "
            "FROM shifde.run_events "
            "WHERE tenant_id = :t AND run_id = :r AND type = 'outcome_unknown' ORDER BY seq LIMIT 1"
        ),
        {"t": tenant_id, "r": run_id},
    ).scalar_one_or_none()
    if not cause:
        cause = session.execute(
            text("SELECT status FROM shifde.runs WHERE id = :r AND tenant_id = :t"),
            {"r": run_id, "t": tenant_id},
        ).scalar_one()
    # one line per CALL (a call is a tool call or a pipeline write node): an operator's verdict
    # settled it, or an authorized resend succeeded. A call with neither is not counted as
    # settled.
    rows = session.execute(
        text(
            "SELECT bool_or(a.decision = 'mark_succeeded') AS ok, "
            "bool_or(a.decision = 'fail') AS bad, "
            "(bool_or(a.decision = 'retry') AND (EXISTS (SELECT 1 FROM shifde.tool_invocations i "
            "WHERE i.tenant_id = a.tenant_id AND i.run_id = a.run_id AND i.call_ref = a.call_ref "
            "AND i.status = 'succeeded' AND i.epoch > min(a.epoch)) "
            "OR EXISTS (SELECT 1 FROM shifde.run_steps s WHERE s.tenant_id = a.tenant_id "
            "AND s.run_id = a.run_id "
            "AND s.kind = 'pipeline_node' AND s.idempotency_key IS NOT NULL "
            "AND 'node:' || s.node_id = a.call_ref AND s.status = 'succeeded' "
            "AND s.attempt > min(a.epoch) + 1))) AS resent "
            "FROM shifde.approvals a WHERE a.tenant_id = :t AND a.run_id = :r "
            "AND a.reason = 'indeterminate_side_effect' AND a.status IN ('approved', 'rejected') "
            "GROUP BY a.tenant_id, a.run_id, a.call_ref"
        ),
        {"t": tenant_id, "r": run_id},
    ).all()
    ok = sum(1 for r in rows if r[0])
    bad = sum(1 for r in rows if r[1] and not r[0])
    resent = sum(1 for r in rows if r[2] and not r[0] and not r[1])
    parts = []
    if ok:
        parts.append(f"{ok} confirmed succeeded")
    if bad:
        parts.append(f"{bad} declared failed or unverifiable")
    if resent:
        parts.append(f"{resent} resent with an operator's approval and the resend succeeded")
    return f"the run ended ({cause}) while a write's outcome was unknown; operators: " + (
        ", ".join(parts) or "no verdict recorded"
    )


def open_decisions_text(session: Session, tenant_id: UUID, run_id: UUID) -> str | None:
    """`shifde.unknown_outcome_text`: the message of a run of `tenant_id` that still has open
    unknown-outcome decisions (their count and the calls), None when none is open."""
    value = session.execute(
        text(
            "SELECT shifde.unknown_outcome_text(r.id, NULL) FROM shifde.runs r "
            "WHERE r.id = :r AND r.tenant_id = :t"
        ),
        {"r": run_id, "t": tenant_id},
    ).scalar_one_or_none()
    return str(value) if value is not None else None


def set_ended_run_message(session: Session, tenant_id: UUID, run_id: UUID, message: str) -> None:
    """Reword the error message of an ended run that is flagged `indeterminate_side_effect`
    (the database guard allows exactly this on a terminal run)."""
    session.execute(
        text(
            "UPDATE shifde.runs SET error_message = :m WHERE id = :id AND tenant_id = :t "
            "AND error_class = 'indeterminate_side_effect'"
        ),
        {"id": run_id, "t": tenant_id, "m": message},
    )


# ------------------------------------------------------------------ jobs
def enqueue(
    session: Session, tenant_id: UUID, run_id: UUID, *, run_after: datetime | None = None
) -> bool:
    """Queue one `run.advance` job for the run. The dedupe key carries the run's current
    `state_version`, so one job per state of the run is queued (never two for the same state, and
    the job of the advance that is still finishing cannot swallow the next one). The conflict
    target is the client's own dedupe key (`uq_jobs__dedupe`), never another client's row."""
    trace = session.execute(
        text("SELECT trace_id FROM shifde.runs WHERE id = :id AND tenant_id = :t"),
        {"id": run_id, "t": tenant_id},
    ).scalar_one()
    traceparent = (
        tracing.format_traceparent(trace, tracing.current_span_id() or tracing.new_span_id())
        if isinstance(trace, str) and tracing.parse_traceparent(f"00-{trace}-{'1' * 16}-01")
        else None
    )
    row = session.execute(
        text(
            "INSERT INTO shifde.jobs (id, tenant_id, kind, run_id, dedupe_key, run_after, "
            "traceparent) VALUES (:id, :t, 'run.advance', :run, :dk, COALESCE(:after, now()), :tp) "
            "ON CONFLICT (tenant_id, kind, dedupe_key) WHERE status IN ('queued', 'leased') "
            "DO NOTHING RETURNING id"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "run": run_id,
            "dk": f"run:{run_id}:{_state_version(session, tenant_id, run_id)}",
            "after": run_after,
            "tp": traceparent,
        },
    ).first()
    session.execute(text("SELECT pg_notify('shifde_jobs', '')"))
    return row is not None


def _state_version(session: Session, tenant_id: UUID, run_id: UUID) -> int:
    return int(
        session.execute(
            text("SELECT state_version FROM shifde.runs WHERE id = :id AND tenant_id = :t"),
            {"id": run_id, "t": tenant_id},
        ).scalar_one()
    )


def claim_jobs(
    session: Session, worker_id: str, max_n: int, lease_seconds: int, tenant_max: int
) -> list[Row]:
    """Cross-client by design (B-2103): the definer refuses a session with a client context and
    returns only `(job_id, tenant_id, kind, run_id)`."""
    return list(
        session.execute(
            text("SELECT * FROM shifde.shifde_claim_jobs(:w, :n, :lease, :cap)"),
            {"w": worker_id, "n": max_n, "lease": lease_seconds, "cap": tenant_max},
        )
        .mappings()
        .all()
    )


def heartbeat_job(
    session: Session, tenant_id: UUID, job_id: UUID, worker_id: str, lease_seconds: int
) -> bool:
    return (
        session.execute(
            text(
                "UPDATE shifde.jobs SET heartbeat_at = now(), "
                "lease_expires_at = now() + make_interval(secs => :lease) "
                "WHERE id = :id AND tenant_id = :t AND lease_owner = :w AND status = 'leased' "
                "RETURNING id"
            ),
            {"id": job_id, "t": tenant_id, "w": worker_id, "lease": lease_seconds},
        ).first()
        is not None
    )


def finish_job(
    session: Session,
    tenant_id: UUID,
    job_id: UUID,
    worker_id: str,
    status: str = "done",
    *,
    last_error: str | None = None,
) -> None:
    session.execute(
        text(
            "UPDATE shifde.jobs SET status = :st, finished_at = now(), "
            "last_error = COALESCE(:e, last_error) "
            "WHERE id = :id AND tenant_id = :t AND lease_owner = :w AND status = 'leased'"
        ),
        {"id": job_id, "t": tenant_id, "w": worker_id, "st": status, "e": last_error},
    )


def release_job(
    session: Session, tenant_id: UUID, job_id: UUID, worker_id: str, delay_seconds: float = 0.0
) -> None:
    """Give a leased job back without counting the attempt (graceful shutdown, or a duplicate
    claim of a job this worker is still running). `delay_seconds` pushes `run_after` forward so a
    handed-back claim is not immediately re-claimed by the same worker in a tight loop."""
    session.execute(
        text(
            "UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL, "
            "lease_expires_at = NULL, attempts = GREATEST(attempts - 1, 0), "
            "run_after = GREATEST(run_after, now() + make_interval(secs => :d)) "
            "WHERE id = :id AND tenant_id = :t AND lease_owner = :w AND status = 'leased'"
        ),
        {"id": job_id, "t": tenant_id, "w": worker_id, "d": delay_seconds},
    )


def finish_run_jobs(session: Session, tenant_id: UUID, run_id: UUID) -> None:
    session.execute(
        text(
            "UPDATE shifde.jobs SET status = 'done', finished_at = now() "
            "WHERE run_id = :r AND tenant_id = :t AND status = 'queued'"
        ),
        {"r": run_id, "t": tenant_id},
    )


# ------------------------------------------------------------------ usage
def insert_usage_event(session: Session, v: Mapping[str, Any]) -> bool:  # Any: column values
    """Compatibility wrapper over the usage ledger (priced events use `ledger.record`).

    `provider_key` in `v` must stay the platform adapter key (the registry's provider key), never a
    string a client chooses: `shifde_ops_stats` groups every client's events by it and the
    metrics endpoint exports it as a label, so a client-chosen value would be a channel between
    clients (isolation review 1 F5 (d)). `ledger.record` refuses a `provider_key` / `model_key`
    that is not the event model's own (runtime isolation review 3 follow-up, B-2141)."""
    return ledger.record(session, v).inserted


def run_cost(session: Session, tenant_id: UUID, run_id: UUID, currency: str) -> Decimal:
    """Known cost of one run of `tenant_id` in `currency` (its own usage events; unmeasured
    events have no amount and add nothing): `limits.max_cost` (carry_forward Item 10)."""
    return Decimal(
        session.execute(
            text(
                "SELECT COALESCE(sum(cost_amount), 0) FROM shifde.usage_events "
                "WHERE tenant_id = :t AND run_id = :r AND currency = :cur"
            ),
            {"t": tenant_id, "r": run_id, "cur": currency},
        ).scalar_one()
    )


def family_cost(
    session: Session, tenant_id: UUID, root_run_id: UUID, currency: str
) -> tuple[Decimal, bool]:
    """(known cost in `currency` of the run family of `root_run_id` of `tenant_id`, whether every
    model call of the family has a cost in `currency`): a model call event with no amount
    (unpriced or unmeasured) or in another currency makes the cost unknown."""
    row = session.execute(
        text(
            "SELECT COALESCE(sum(cost_amount) FILTER (WHERE currency = :cur), 0), "
            "count(*) FILTER (WHERE event_type = 'model_call' AND (cost_amount IS NULL "
            "OR currency IS DISTINCT FROM :cur)) "
            "FROM shifde.usage_events WHERE tenant_id = :t AND root_run_id = :root"
        ),
        {"t": tenant_id, "root": root_run_id, "cur": currency},
    ).one()
    return Decimal(row[0]), int(row[1]) == 0


def root_pipeline_id(session: Session, tenant_id: UUID, root_run_id: UUID) -> UUID | None:
    row = session.execute(
        text("SELECT pipeline_id FROM shifde.runs WHERE id = :id AND tenant_id = :t"),
        {"id": root_run_id, "t": tenant_id},
    ).first()
    return row[0] if row else None


# ------------------------------------------------------------------ misc
def reap(session: Session) -> list[Row]:
    """Cross-client by design (B-2103): the definer refuses a session with a client context and
    returns only `(tenant_id, run_id, reason)`."""
    return list(
        session.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).mappings().all()
    )


def mark_reap(session: Session, worker_id: str) -> None:
    # `workers` is a global table (no client data, B-2104)
    session.execute(
        text("UPDATE shifde.workers SET last_reap_at = now() WHERE id = :id"), {"id": worker_id}
    )


def scope_statuses(
    session: Session, tenant_id: UUID, workspace_id: UUID, project_id: UUID
) -> tuple[str | None, str | None]:
    """(workspace status, project status) of `tenant_id`'s workspace and of its project in that
    workspace; None for an id that is not one (another client's or unknown) (findings Item 12)."""
    ws = session.execute(
        text("SELECT status FROM shifde.workspaces WHERE id = :w AND tenant_id = :t"),
        {"w": workspace_id, "t": tenant_id},
    ).scalar_one_or_none()
    proj = session.execute(
        text(
            "SELECT status FROM shifde.projects WHERE id = :p AND tenant_id = :t "
            "AND workspace_id = :w"
        ),
        {"p": project_id, "w": workspace_id, "t": tenant_id},
    ).scalar_one_or_none()
    return ws, proj


def principal_display(
    session: Session, tenant_id: UUID, principal_type: str, principal_id: UUID
) -> str:
    if principal_type == "service_account":
        row = session.execute(
            text("SELECT name FROM shifde.service_accounts WHERE id = :p AND tenant_id = :t"),
            {"p": principal_id, "t": tenant_id},
        ).first()
    else:
        # `users` is global (no tenant_id); `users_select` shows only the caller, members of the
        # current client and, to a platform administrator, everyone. The membership predicate
        # names the client too, so a user of another client only is displayed as the id
        # (runtime isolation review 2, finding 1)
        row = session.execute(
            text(
                "SELECT u.display_name FROM shifde.users u WHERE u.id = :p AND EXISTS "
                "(SELECT 1 FROM shifde.memberships m WHERE m.user_id = u.id AND m.tenant_id = :t)"
            ),
            {"p": principal_id, "t": tenant_id},
        ).first()
    return str(row[0]) if row else str(principal_id)


def upsert_worker(
    session: Session, worker_id: str, hostname: str, pid: int, version: str, concurrency: int
) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.workers (id, hostname, pid, version, concurrency) "
            "VALUES (:id, :h, :pid, :v, :c) ON CONFLICT (id) DO UPDATE SET hostname = :h, "
            "pid = :pid, version = :v, concurrency = :c, started_at = now(), heartbeat_at = now(), "
            "status = 'running'"
        ),
        {"id": worker_id, "h": hostname, "pid": pid, "v": version, "c": concurrency},
    )


def beat_worker(session: Session, worker_id: str, status: str = "running") -> None:
    session.execute(
        text("UPDATE shifde.workers SET heartbeat_at = now(), status = :s WHERE id = :id"),
        {"id": worker_id, "s": status},
    )


# ------------------------------------------------------------------ pipelines (P6)
def pending_approval_refs(session: Session, tenant_id: UUID, run_id: UUID) -> set[str]:
    """`call_ref`s (`node:<id>`) of the run's pending approvals."""
    return {
        str(r[0])
        for r in session.execute(
            text(
                "SELECT call_ref FROM shifde.approvals WHERE tenant_id = :t AND run_id = :r "
                "AND status = 'pending'"
            ),
            {"t": tenant_id, "r": run_id},
        )
    }


def pending_unknown_refs(session: Session, tenant_id: UUID, run_id: UUID) -> set[str]:
    """`call_ref`s of the run's open unknown-outcome decisions (`node:<id>` for pipeline nodes,
    including the tool calls a `tool` node makes)."""
    return {
        str(r[0])
        for r in session.execute(
            text(
                "SELECT call_ref FROM shifde.approvals WHERE tenant_id = :t AND run_id = :r "
                "AND status = 'pending' AND reason = 'indeterminate_side_effect'"
            ),
            {"t": tenant_id, "r": run_id},
        )
    }


def request_cancel(session: Session, tenant_id: UUID, run_id: UUID, by: UUID | None) -> None:
    """Flag a run for cooperative cancellation (idempotent)."""
    session.execute(
        text(
            "UPDATE shifde.runs SET cancel_requested_at = COALESCE(cancel_requested_at, now()), "
            "cancel_requested_by = COALESCE(cancel_requested_by, :by) "
            "WHERE id = :id AND tenant_id = :t"
        ),
        {"id": run_id, "t": tenant_id, "by": by},
    )


def get_artifact_row(session: Session, tenant_id: UUID, artifact_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT id, run_id, kind, size_bytes, content, content_type, created_at "
                "FROM shifde.run_artifacts WHERE id = :id AND tenant_id = :t"
            ),
            {"id": artifact_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def finished_awaited_children(session: Session, tenant_id: UUID, run_id: UUID) -> int:
    """Open node steps of the run whose child run is already terminal (a wake-up that raced the
    parent's parking)."""
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.run_steps s "
                "JOIN shifde.runs c ON c.id = s.child_run_id AND c.tenant_id = s.tenant_id "
                "WHERE s.tenant_id = :t AND s.run_id = :r AND s.status = 'running' "
                "AND c.status IN ('completed', 'failed', 'cancelled', 'timed_out')"
            ),
            {"t": tenant_id, "r": run_id},
        ).scalar_one()
    )


# ------------------------------------------------------------------ lifecycle (RT-10)
def job_of_run(session: Session, tenant_id: UUID, run_id: UUID, job_id: UUID) -> Row | None:
    """`(status, lease_owner, traceparent)` of `job_id` when it is a `run.advance` job of
    `tenant_id`'s run `run_id`; None for another client's job, another run's job or an unknown id
    (B-2240)."""
    return (
        session.execute(
            text(
                "SELECT status, lease_owner, traceparent FROM shifde.jobs "
                "WHERE id = :j AND tenant_id = :t AND run_id = :r AND kind = 'run.advance'"
            ),
            {"j": job_id, "t": tenant_id, "r": run_id},
        )
        .mappings()
        .first()
    )


def last_status_change(session: Session, tenant_id: UUID, run_id: UUID) -> Row | None:
    """`(from_status, to_status)` of the run's latest status event (a transition or the reaper's
    `lease_lost`), or None."""
    return (
        session.execute(
            text(
                "SELECT from_status, to_status FROM shifde.run_events "
                "WHERE tenant_id = :t AND run_id = :r AND type IN ('state_change', 'lease_lost') "
                "ORDER BY seq DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": run_id},
        )
        .mappings()
        .first()
    )
