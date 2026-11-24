"""SQL for app requests, their drafts and their `appgen.draft` jobs (RLS tenant context).

Every read and write is keyed by `tenant_id` as well as by row-level security (the B-802
pattern). A status change is a conditional UPDATE on the status the caller read, so of two racing
changes exactly one wins. The job insert follows `connections.repository.enqueue_job`; the stored
discovery is read by one tenant-keyed SELECT, as `packaging.repository` reads datasets.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

REQUEST_COLUMNS = (
    "r.id, r.tenant_id, r.workspace_id, r.project_id, r.connection_id, r.discovery_id, r.tables, "
    "r.brief, r.model_id, r.status, r.status_reason, r.job_id, r.accept_progress, "
    "r.accepted_revision_no, r.solution_version_id, r.requested_by, r.accepted_by, r.created_at, "
    "r.updated_at, (SELECT max(d.revision_no) FROM shifde.appgen_drafts d "
    "WHERE d.tenant_id = r.tenant_id AND d.request_id = r.id) AS latest_revision_no"
)
DRAFT_COLUMNS = (
    "id, request_id, revision_no, source, draft, draft_hash, validation, created_by, created_at"
)


def insert_request(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.appgen_requests (tenant_id, id, workspace_id, project_id, "
            "connection_id, discovery_id, tables, brief, model_id, status, job_id, requested_by) "
            "VALUES (:tenant_id, :id, :workspace_id, :project_id, :connection_id, :discovery_id, "
            "CAST(:tables AS jsonb), :brief, :model_id, 'queued', :job_id, :requested_by)"
        ),
        {**values, "tables": json.dumps(list(values["tables"]))},
    )


def get_request(
    session: Session, request_id: UUID, *, tenant_id: UUID, lock: bool = False
) -> Row | None:
    if lock:
        # locked first, read after: a read in the locking statement keeps the snapshot from
        # before the wait, so it would miss the revision the lock holder just committed
        session.execute(
            text(
                "SELECT 1 FROM shifde.appgen_requests WHERE tenant_id = :t AND id = :id FOR UPDATE"
            ),
            {"t": tenant_id, "id": request_id},
        )
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {REQUEST_COLUMNS} FROM shifde.appgen_requests r "  # nosec B608
                "WHERE r.tenant_id = :t AND r.id = :id"
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
                f"SELECT {REQUEST_COLUMNS} FROM shifde.appgen_requests r "  # nosec B608
                "WHERE r.tenant_id = :t AND r.project_id = :p "
                "AND (CAST(:s AS text) IS NULL OR r.status = :s) "
                "AND (CAST(:b AS uuid) IS NULL OR r.id < :b) ORDER BY r.id DESC LIMIT :n"
            ),
            {"t": tenant_id, "p": project_id, "s": status, "b": before, "n": limit},
        )
        .mappings()
        .all()
    )


def change_status(
    session: Session,
    request_id: UUID,
    *,
    tenant_id: UUID,
    expected: Sequence[str],
    status: str,
    status_reason: str | None = None,
    job_id: UUID | None = None,
    for_job: UUID | None = None,
) -> bool:
    """Set `status` (and `status_reason`; a new `job_id` when given) when the request is in one of
    the `expected` states (and, with `for_job`, still names that job); False when it was not."""
    changed = session.execute(
        text(
            "UPDATE shifde.appgen_requests SET status = :st, status_reason = :reason, "
            "job_id = coalesce(CAST(:job AS uuid), job_id), updated_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status = ANY (CAST(:expected AS text[])) "
            "AND (CAST(:for_job AS uuid) IS NULL OR job_id = :for_job) RETURNING id"
        ),
        {
            "t": tenant_id,
            "id": request_id,
            "st": status,
            "reason": status_reason,
            "job": job_id,
            "for_job": for_job,
            "expected": list(expected),
        },
    ).first()
    return changed is not None


# ------------------------------------------------------------------ accept (design 5.5)
# `accept_progress.claim`: the token of the accept call that runs the steps and its lease's end
# (database time); a claim is free when absent or when its `until` has passed
CLAIM = (
    "jsonb_build_object('claim', jsonb_build_object('token', CAST(:tok AS text), "
    "'until', to_jsonb(now() + make_interval(secs => :lease))))"
)
CLAIM_FREE = (
    "(accept_progress -> 'claim' IS NULL "
    "OR CAST(accept_progress -> 'claim' ->> 'until' AS timestamptz) < now())"
)
CLAIM_HELD = "accept_progress -> 'claim' ->> 'token' = CAST(:tok AS text)"


def start_accepting(
    session: Session,
    request_id: UUID,
    *,
    tenant_id: UUID,
    revision_no: int,
    accepted_by: UUID,
    token: UUID,
    lease_seconds: int,
) -> bool:
    """`drafted` -> `accepting` with the accepted revision, the accepter and the claim `token`;
    False when the request was not `drafted`."""
    changed = session.execute(
        text(
            # the claim fragment is a module constant; values are bound
            "UPDATE shifde.appgen_requests SET status = 'accepting', status_reason = NULL, "  # nosec B608
            "accepted_revision_no = :rev, accepted_by = :by, "
            f"accept_progress = accept_progress || {CLAIM}, updated_at = now() "
            "WHERE tenant_id = :t AND id = :id AND status = 'drafted' RETURNING id"
        ),
        {
            "t": tenant_id,
            "id": request_id,
            "rev": revision_no,
            "by": accepted_by,
            "tok": str(token),
            "lease": lease_seconds,
        },
    ).first()
    return changed is not None


def claim_accepting(
    session: Session,
    request_id: UUID,
    *,
    tenant_id: UUID,
    revision_no: int,
    token: UUID,
    lease_seconds: int,
) -> bool:
    """Take the claim of an `accepting` request of `revision_no` for `token`; False when another
    call holds an unexpired claim (or the request is no longer `accepting` that revision)."""
    changed = session.execute(
        text(
            # the claim fragments are module constants; values are bound
            f"UPDATE shifde.appgen_requests SET accept_progress = accept_progress || {CLAIM}, "  # nosec B608
            "updated_at = now() WHERE tenant_id = :t AND id = :id AND status = 'accepting' "
            f"AND accepted_revision_no = :rev AND {CLAIM_FREE} RETURNING id"
        ),
        {
            "t": tenant_id,
            "id": request_id,
            "rev": revision_no,
            "tok": str(token),
            "lease": lease_seconds,
        },
    ).first()
    return changed is not None


def record_progress(
    session: Session,
    request_id: UUID,
    *,
    tenant_id: UUID,
    key: str,
    value: Any,
    token: UUID,
    lease_seconds: int,
) -> bool:
    """Merge `{key: value}` into `accept_progress` and renew the claim while `accepting` and
    `token` holds the claim; False when it does not."""
    changed = session.execute(
        text(
            # the claim fragments are module constants; values are bound
            "UPDATE shifde.appgen_requests SET accept_progress = accept_progress || "  # nosec B608
            f"jsonb_build_object(CAST(:k AS text), CAST(:v AS jsonb)) || {CLAIM}, "
            "updated_at = now() WHERE tenant_id = :t AND id = :id AND status = 'accepting' "
            f"AND {CLAIM_HELD} RETURNING id"
        ),
        {
            "t": tenant_id,
            "id": request_id,
            "k": key,
            "v": json.dumps(value),
            "tok": str(token),
            "lease": lease_seconds,
        },
    ).first()
    return changed is not None


def accept_failed(
    session: Session, request_id: UUID, *, tenant_id: UUID, reason: str, token: UUID
) -> bool:
    """Record a failed accept step's `reason` and release the claim while `accepting` and `token`
    holds the claim; False when it does not."""
    changed = session.execute(
        text(
            # the claim fragment is a module constant; values are bound
            "UPDATE shifde.appgen_requests SET status_reason = :reason, "  # nosec B608
            "accept_progress = accept_progress - 'claim', updated_at = now() "
            f"WHERE tenant_id = :t AND id = :id AND status = 'accepting' AND {CLAIM_HELD} "
            "RETURNING id"
        ),
        {"t": tenant_id, "id": request_id, "reason": reason, "tok": str(token)},
    ).first()
    return changed is not None


def finish_accepting(
    session: Session, request_id: UUID, *, tenant_id: UUID, solution_version_id: UUID, token: UUID
) -> bool:
    """`accepting` -> `accepted` with the solution version, the claim released; False when it was
    not `accepting` or `token` does not hold the claim."""
    changed = session.execute(
        text(
            # the claim fragment is a module constant; values are bound
            "UPDATE shifde.appgen_requests SET status = 'accepted', status_reason = NULL, "  # nosec B608
            "solution_version_id = :v, accept_progress = accept_progress - 'claim', "
            "updated_at = now() "
            f"WHERE tenant_id = :t AND id = :id AND status = 'accepting' AND {CLAIM_HELD} "
            "RETURNING id"
        ),
        {"t": tenant_id, "id": request_id, "v": solution_version_id, "tok": str(token)},
    ).first()
    return changed is not None


def insert_draft(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.appgen_drafts (tenant_id, id, request_id, revision_no, source, "
            "draft, draft_hash, validation, created_by) VALUES (:tenant_id, :id, :request_id, "
            ":revision_no, :source, CAST(:draft AS jsonb), :draft_hash, "
            "CAST(:validation AS jsonb), :created_by)"
        ),
        {
            **values,
            "draft": json.dumps(values["draft"]),
            "validation": json.dumps(values["validation"]),
        },
    )


def list_drafts(session: Session, request_id: UUID, *, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {DRAFT_COLUMNS} FROM shifde.appgen_drafts "  # nosec B608
                "WHERE tenant_id = :t AND request_id = :r ORDER BY revision_no"
            ),
            {"t": tenant_id, "r": request_id},
        )
        .mappings()
        .all()
    )


def get_discovery(
    session: Session, discovery_id: UUID, *, tenant_id: UUID, connection_id: UUID
) -> Row | None:
    """The stored discovery a request names (of its connection, in its client)."""
    return (
        session.execute(
            text(
                "SELECT id, kind, payload FROM shifde.connection_discoveries "
                "WHERE tenant_id = :t AND id = :id AND connection_id = :c"
            ),
            {"t": tenant_id, "id": discovery_id, "c": connection_id},
        )
        .mappings()
        .first()
    )


# ------------------------------------------------------------------ jobs
def enqueue_job(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    job_id: UUID,
    dedupe_key: str,
    payload: Mapping[str, Any],
) -> None:
    """Queue the job in `tenant_id` (its dedupe key names its own id, so it never collides) and
    wake the workers."""
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


def get_job(session: Session, job_id: UUID, *, tenant_id: UUID, kind: str) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT id, kind, status, payload FROM shifde.jobs "
                "WHERE id = :id AND tenant_id = :t AND kind = :k"
            ),
            {"id": job_id, "t": tenant_id, "k": kind},
        )
        .mappings()
        .first()
    )
