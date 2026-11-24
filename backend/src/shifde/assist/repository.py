"""SQL for the assist settings, specialists, requests and proposals (assist_0001) and the
`assist.route` jobs, under the caller's client context.

Every read and write is keyed by `tenant_id` as well as by row-level security (the B-802
pattern). A status change is a conditional UPDATE on the states the caller expects, so of two
racing changes exactly one wins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

SETTINGS_COLUMNS = (
    "assist_enabled, catalog_version, model_routing_enabled, router_model_id, "
    "specialist_max_cost, updated_by, updated_at"
)
SPECIALIST_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, key, agent_id, agent_version_id, catalog_version, "
    "install_progress, status, installed_by, created_at, updated_at"
)
REQUEST_COLUMNS = (
    "id, tenant_id, workspace_id, project_id, requested_by, text, requested_specialist, context, "
    "status, status_reason, specialist_key, route_method, rule_id, router_version, job_id, "
    "run_id, created_at, updated_at"
)
PROPOSAL_COLUMNS = (
    "id, tenant_id, request_id, run_id, step_key, kind, payload, status, status_reason, "
    "result_ref, decided_by, decided_at, created_at, updated_at"
)
# the request columns a state change may set besides `status` (the UPDATE grant of assist_0001)
CHANGEABLE = frozenset(
    {"status_reason", "specialist_key", "route_method", "rule_id", "router_version", "job_id",
     "run_id"}
)  # fmt: skip


# ====================================================================== settings
def get_settings(session: Session, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SETTINGS_COLUMNS} FROM shifde.assist_settings "  # nosec B608
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .first()
    )


def put_routing(
    session: Session,
    *,
    tenant_id: UUID,
    model_routing_enabled: bool,
    router_model_id: UUID | None,
    specialist_max_cost: Decimal,
    updated_by: UUID,
) -> None:
    """Upsert the routing fields and the cost cap; the switch and the pin are left as they are."""
    session.execute(
        text(
            "INSERT INTO shifde.assist_settings (tenant_id, model_routing_enabled, "
            "router_model_id, specialist_max_cost, updated_by, updated_at) "
            "VALUES (:t, :r, :m, :c, :u, now()) ON CONFLICT (tenant_id) DO UPDATE SET "
            "model_routing_enabled = excluded.model_routing_enabled, "
            "router_model_id = excluded.router_model_id, "
            "specialist_max_cost = excluded.specialist_max_cost, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at"
        ),
        {
            "t": tenant_id,
            "r": model_routing_enabled,
            "m": router_model_id,
            "c": specialist_max_cost,
            "u": updated_by,
        },
    )


def put_switch(
    session: Session, *, tenant_id: UUID, enabled: bool, pin: str | None, updated_by: UUID
) -> None:
    """Upsert the organization switch; `pin` set replaces the catalog pin, None keeps it."""
    session.execute(
        text(
            "INSERT INTO shifde.assist_settings (tenant_id, assist_enabled, catalog_version, "
            "updated_by, updated_at) VALUES (:t, :e, :p, :u, now()) "
            "ON CONFLICT (tenant_id) DO UPDATE SET assist_enabled = excluded.assist_enabled, "
            "catalog_version = coalesce(CAST(:p AS text), shifde.assist_settings.catalog_version), "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at"
        ),
        {"t": tenant_id, "e": enabled, "p": pin, "u": updated_by},
    )


# ====================================================================== specialists
def get_specialist(
    session: Session, *, tenant_id: UUID, project_id: UUID, key: str, lock: bool = False
) -> Row | None:
    if lock:
        # locked first, read after: a read in the locking statement keeps the snapshot from
        # before the wait, so it would miss the progress the lock holder just committed
        session.execute(
            text(
                "SELECT 1 FROM shifde.assist_specialists "
                "WHERE tenant_id = :t AND project_id = :p AND key = :k FOR UPDATE"
            ),
            {"t": tenant_id, "p": project_id, "k": key},
        )
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SPECIALIST_COLUMNS} FROM shifde.assist_specialists "  # nosec B608
                "WHERE tenant_id = :t AND project_id = :p AND key = :k"
            ),
            {"t": tenant_id, "p": project_id, "k": key},
        )
        .mappings()
        .first()
    )


def list_specialists(
    session: Session, *, tenant_id: UUID, project_id: UUID | None = None
) -> list[Row]:
    """The client's specialists (one project's when given)."""
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {SPECIALIST_COLUMNS} FROM shifde.assist_specialists "  # nosec B608
                "WHERE tenant_id = :t AND (CAST(:p AS uuid) IS NULL OR project_id = :p) "
                "ORDER BY project_id, id"
            ),
            {"t": tenant_id, "p": project_id},
        )
        .mappings()
        .all()
    )


def insert_specialist(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.assist_specialists (tenant_id, id, workspace_id, project_id, key, "
            "catalog_version, installed_by) VALUES (:tenant_id, :id, :workspace_id, :project_id, "
            ":key, :catalog_version, :installed_by)"
        ),
        dict(values),
    )


def set_progress(
    session: Session, specialist_id: UUID, *, tenant_id: UUID, progress: Sequence[Any]
) -> None:
    session.execute(
        text(
            "UPDATE shifde.assist_specialists SET install_progress = CAST(:pr AS jsonb), "
            "updated_at = now() WHERE tenant_id = :t AND id = :id"
        ),
        {"t": tenant_id, "id": specialist_id, "pr": json.dumps(list(progress))},
    )


def finish_install(
    session: Session,
    specialist_id: UUID,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    agent_version_id: UUID,
    catalog_version: str,
) -> bool:
    """Record the installed agent and set `enabled`; False when the row was disabled meanwhile."""
    changed = session.execute(
        text(
            "UPDATE shifde.assist_specialists SET agent_id = :a, agent_version_id = :v, "
            "catalog_version = :c, status = 'enabled', updated_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status IN ('installing', 'enabled') "
            "RETURNING id"
        ),
        {"t": tenant_id, "id": specialist_id, "a": agent_id, "v": agent_version_id,
         "c": catalog_version},
    ).first()  # fmt: skip
    return changed is not None


def change_status(
    session: Session, specialist_id: UUID, *, tenant_id: UUID, expected: Sequence[str], status: str
) -> bool:
    changed = session.execute(
        text(
            "UPDATE shifde.assist_specialists SET status = :s, updated_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status = ANY (CAST(:e AS text[])) RETURNING id"
        ),
        {"t": tenant_id, "id": specialist_id, "s": status, "e": list(expected)},
    ).first()
    return changed is not None


# ====================================================================== requests
def insert_request(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.assist_requests (tenant_id, id, workspace_id, project_id, "
            "requested_by, text, requested_specialist, context, status, status_reason, "
            "specialist_key, route_method, rule_id, router_version, job_id) VALUES (:tenant_id, "
            ":id, :workspace_id, :project_id, :requested_by, :text, :requested_specialist, "
            "CAST(:context AS jsonb), :status, :status_reason, :specialist_key, :route_method, "
            ":rule_id, :router_version, :job_id)"
        ),
        {**values, "context": json.dumps(values["context"]) if values["context"] else None},
    )


def get_request(session: Session, *, tenant_id: UUID, request_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {REQUEST_COLUMNS} FROM shifde.assist_requests "  # nosec B608
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"t": tenant_id, "id": request_id},
        )
        .mappings()
        .first()
    )


def list_requests(
    session: Session,
    *,
    tenant_id: UUID,
    project_id: UUID,
    status: str | None,
    before: UUID | None,
    limit: int,
) -> list[Row]:
    """The project's requests, newest first (ids are UUIDv7), after the cursor `before`."""
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {REQUEST_COLUMNS} FROM shifde.assist_requests "  # nosec B608
                "WHERE tenant_id = :t AND project_id = :p "
                "AND (CAST(:s AS text) IS NULL OR status = :s) "
                "AND (CAST(:b AS uuid) IS NULL OR id < :b) ORDER BY id DESC LIMIT :n"
            ),
            {"t": tenant_id, "p": project_id, "s": status, "b": before, "n": limit},
        )
        .mappings()
        .all()
    )


def change_request(
    session: Session,
    request_id: UUID,
    *,
    tenant_id: UUID,
    expected: Sequence[str],
    status: str,
    **fields: Any,
) -> Row | None:
    """Set `status` (and `fields`) while the request is in one of `expected`; the changed row, or
    None when it was in another state."""
    unknown = set(fields) - CHANGEABLE
    if unknown:
        raise ValueError(f"not changeable request columns: {', '.join(sorted(unknown))}")
    sets = "".join(f", {name} = :{name}" for name in sorted(fields))
    return (
        session.execute(
            text(
                # column names are checked against CHANGEABLE; values are bound
                f"UPDATE shifde.assist_requests SET status = :s{sets}, "  # nosec B608
                "updated_at = now() WHERE tenant_id = :t AND id = :id "
                f"AND status = ANY (CAST(:e AS text[])) RETURNING {REQUEST_COLUMNS}"
            ),
            {"t": tenant_id, "id": request_id, "s": status, "e": list(expected), **fields},
        )
        .mappings()
        .first()
    )


def bound_requests(
    session: Session,
    *,
    tenant_id: UUID,
    run_id: UUID,
    named: UUID | None,
    project_id: UUID,
    requested_by: UUID,
) -> list[Row]:
    """The project's requests by `requested_by` that name the run (`run_id`) or that its stored
    idempotency key names (`named`), while they hand over to it or are routed to it."""
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {REQUEST_COLUMNS} FROM shifde.assist_requests "  # nosec B608
                "WHERE tenant_id = :t AND (id = :n OR run_id = :r) AND project_id = :p "
                "AND requested_by = :u AND status IN ('handing_over', 'routed') "
                "AND (run_id IS NULL OR run_id = :r) ORDER BY id"
            ),
            {"t": tenant_id, "n": named, "r": run_id, "p": project_id, "u": requested_by},
        )
        .mappings()
        .all()
    )


# ====================================================================== proposals
def insert_proposal(session: Session, values: Mapping[str, Any]) -> UUID | None:
    """Insert the proposal of a run step; None when that step already has one (a replay)."""
    found = session.execute(
        text(
            "INSERT INTO shifde.assist_proposals (tenant_id, id, request_id, run_id, step_key, "
            "kind, payload) VALUES (:tenant_id, :id, :request_id, :run_id, :step_key, :kind, "
            "CAST(:payload AS jsonb)) ON CONFLICT (tenant_id, run_id, step_key) DO NOTHING "
            "RETURNING id"
        ),
        {**values, "payload": json.dumps(values["payload"])},
    ).first()
    return None if found is None else UUID(str(found[0]))


def step_proposal(session: Session, *, tenant_id: UUID, run_id: UUID, step_key: str) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {PROPOSAL_COLUMNS} FROM shifde.assist_proposals "  # nosec B608
                "WHERE tenant_id = :t AND run_id = :r AND step_key = :k"
            ),
            {"t": tenant_id, "r": run_id, "k": step_key},
        )
        .mappings()
        .first()
    )


def get_proposal(session: Session, *, tenant_id: UUID, proposal_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {PROPOSAL_COLUMNS} FROM shifde.assist_proposals "  # nosec B608
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"t": tenant_id, "id": proposal_id},
        )
        .mappings()
        .first()
    )


def claim_proposal(
    session: Session, proposal_id: UUID, *, tenant_id: UUID, decided_by: UUID
) -> Row | None:
    """Claim an open, unclaimed proposal for one accept (`decided_by` set); None otherwise."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.assist_proposals SET decided_by = :u, "  # nosec B608
                "updated_at = now() WHERE tenant_id = :t AND id = :id AND status = 'open' "
                f"AND decided_by IS NULL RETURNING {PROPOSAL_COLUMNS}"
            ),
            {"t": tenant_id, "id": proposal_id, "u": decided_by},
        )
        .mappings()
        .first()
    )


def decide_proposal(
    session: Session,
    proposal_id: UUID,
    *,
    tenant_id: UUID,
    expected: Sequence[str],
    status: str,
    decided_by: UUID,
    status_reason: str | None = None,
    result_ref: Mapping[str, Any] | None = None,
) -> Row | None:
    """Set the decision while the proposal is in one of `expected`; None otherwise."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "UPDATE shifde.assist_proposals SET status = :s, "  # nosec B608
                "status_reason = :sr, result_ref = CAST(:rr AS jsonb), decided_by = :u, "
                "decided_at = now(), updated_at = now() WHERE tenant_id = :t AND id = :id "
                f"AND status = ANY (CAST(:e AS text[])) RETURNING {PROPOSAL_COLUMNS}"
            ),
            {
                "t": tenant_id,
                "id": proposal_id,
                "s": status,
                "e": list(expected),
                "u": decided_by,
                "sr": status_reason,
                "rr": json.dumps(dict(result_ref)) if result_ref else None,
            },
        )
        .mappings()
        .first()
    )


def list_proposals(session: Session, *, tenant_id: UUID, request_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {PROPOSAL_COLUMNS} FROM shifde.assist_proposals "  # nosec B608
                "WHERE tenant_id = :t AND request_id = :r ORDER BY id"
            ),
            {"t": tenant_id, "r": request_id},
        )
        .mappings()
        .all()
    )


# ====================================================================== retention (Call S7-J)
# an ended request: routed, refused or failed, created before the cutoff, its run gone (or ended)
_ENDED_REQUEST = (
    "r.tenant_id = :t AND r.created_at < :c AND r.status IN ('routed', 'refused', 'failed') "
    "AND (r.run_id IS NULL OR NOT EXISTS (SELECT 1 FROM shifde.runs x WHERE x.tenant_id = :t "
    "AND x.id = r.run_id{live}))"
)
_SCRUBBABLE = (
    _ENDED_REQUEST.format(
        live=" AND x.status NOT IN ('completed', 'failed', 'cancelled', 'timed_out')"
    )
    + " AND (r.text <> '' OR r.context IS NOT NULL)"
)
# every proposal of a deletable request is older than the cutoff too (the delete guard's rule)
# (a dry run counts the runs the same pass would delete, `:gone`, as gone)
_DELETABLE = _ENDED_REQUEST.format(live=" AND x.id <> ALL (CAST(:gone AS uuid[]))") + (
    " AND NOT EXISTS (SELECT 1 FROM shifde.assist_proposals p WHERE p.tenant_id = :t "
    "AND p.request_id = r.id AND p.created_at >= :c)"
)


def scrub_requests(session: Session, *, tenant_id: UUID, cutoff: datetime, dry_run: bool) -> int:
    """Blank the text and context of the client's ended requests created before `cutoff` whose
    run is gone or has ended; the number of requests (only counted on `dry_run`)."""
    params = {"t": tenant_id, "c": cutoff}
    if dry_run:
        # fragments are module constants; values are bound
        sql = f"SELECT count(*) FROM shifde.assist_requests r WHERE {_SCRUBBABLE}"  # nosec B608
        return int(session.execute(text(sql), params).scalar_one())
    sql = (
        # fragments are module constants; values are bound
        "UPDATE shifde.assist_requests r SET text = '', context = NULL, "  # nosec B608
        f"updated_at = now() WHERE {_SCRUBBABLE}"
    )
    return int(session.execute(text(sql), params).rowcount)  # type: ignore[attr-defined]


def delete_requests(
    session: Session, *, tenant_id: UUID, cutoff: datetime, gone: Sequence[UUID], dry_run: bool
) -> tuple[int, int]:
    """Delete the client's ended requests created before `cutoff` whose run is gone and whose
    proposals are older than `cutoff` too, proposals first; (requests, proposals). Unless
    `dry_run` (only counted then) the retention delete has set `shifde.retention_delete_cutoff`."""
    ids = list(
        session.execute(
            # fragments are module constants; values are bound
            text(f"SELECT r.id FROM shifde.assist_requests r WHERE {_DELETABLE}"),  # nosec B608
            {"t": tenant_id, "c": cutoff, "gone": list(gone)},
        ).scalars()
    )
    if not ids:
        return 0, 0
    params = {"t": tenant_id, "ids": ids}
    where = "FROM shifde.assist_proposals WHERE tenant_id = :t AND request_id = ANY(:ids)"
    if dry_run:
        proposals = session.execute(text(f"SELECT count(*) {where}"), params).scalar_one()
        return len(ids), int(proposals)
    proposals = session.execute(text(f"DELETE {where}"), params).rowcount  # type: ignore[attr-defined]
    requests = session.execute(
        text("DELETE FROM shifde.assist_requests WHERE tenant_id = :t AND id = ANY(:ids)"), params
    ).rowcount  # type: ignore[attr-defined]
    return int(requests), int(proposals)


# ====================================================================== jobs
def enqueue_job(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    job_id: UUID,
    dedupe_key: str,
    payload: Mapping[str, Any],
) -> None:
    """Queue the job in `tenant_id` (once per dedupe key while queued or leased) and wake the
    workers."""
    session.execute(
        text(
            "INSERT INTO shifde.jobs (id, tenant_id, kind, payload, dedupe_key, run_after) "
            "VALUES (:id, :t, :k, CAST(:p AS jsonb), :dk, now()) "
            "ON CONFLICT (tenant_id, kind, dedupe_key) WHERE status IN ('queued', 'leased') "
            "DO NOTHING"
        ),
        {"id": job_id, "t": tenant_id, "k": kind, "p": json.dumps(payload), "dk": dedupe_key},
    )
    session.execute(text("SELECT pg_notify('shifde_jobs', '')"))


def get_job(session: Session, *, tenant_id: UUID, job_id: UUID, kind: str) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT id, kind, status, payload FROM shifde.jobs "
                "WHERE tenant_id = :t AND id = :id AND kind = :k"
            ),
            {"t": tenant_id, "id": job_id, "k": kind},
        )
        .mappings()
        .first()
    )
