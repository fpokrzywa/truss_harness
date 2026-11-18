"""`shifde-admin maintenance purge`: scrub the stored content of old, ended runs (F-11, Item 20).

Runs as the application role, so row-level security confines each client's pass: one transaction
lists every client under the platform-administrator context, then each client is processed in a
transaction of its own with only that client set. Only the two scrubs migration `runtime_0002`
permits are issued (artifact content -> `{"scrubbed": true}`, tool-call arguments -> NULL); nothing
is deleted. A client with a legal hold on any data class is skipped. A real run appends one
`maintenance.retention.purged` audit event per client through the normal audit path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.core.context import RequestContext
from shifde.core.db import Database, forget_tenant, platform_admin_context, set_tenant
from shifde.governance.repository import retention_rows
from shifde.governance.validation import RETENTION_DEFAULTS, RETENTION_FLOORS
from shifde.runtime import repository as runs

ACTION = "maintenance.retention.purged"
RUN_CLASSES = ("run_artifacts", "run_steps_content")

_CLIENTS = text("SELECT id, slug FROM shifde.tenants ORDER BY id")
# Terminal runs of the client that ended before the cutoff and whose root run has ended too (a
# parent reads a child's output after the child ends, as `runtime.settle.scrub_ended`).
_ELIGIBLE = text(
    "SELECT r.id FROM shifde.runs r JOIN shifde.runs root "
    "ON root.id = r.root_run_id AND root.tenant_id = :t "
    "WHERE r.tenant_id = :t AND r.status IN ('completed', 'failed', 'cancelled', 'timed_out') "
    "AND r.finished_at IS NOT NULL AND r.finished_at < :cutoff "
    "AND root.status IN ('completed', 'failed', 'cancelled', 'timed_out') ORDER BY r.id"
)


@dataclass(frozen=True)
class ClientPurge:
    slug: str
    legal_hold: bool
    effective_days: int
    cutoff: datetime
    runs: int = 0
    artifacts: int = 0
    tool_invocations: int = 0


def effective_days(older_than_days: int, stored: dict[str, int]) -> int:
    """The largest of `--older-than`, the client's resolved `retain_days` of both run data
    classes (stored value or default) and their floors."""
    days = [older_than_days]
    for data_class in RUN_CLASSES:
        days.append(stored.get(data_class, RETENTION_DEFAULTS[data_class]))
        days.append(RETENTION_FLOORS[data_class])
    return max(days)


def _client_pass(
    session: Session,
    tenant_id: UUID,
    slug: str,
    *,
    older_than_days: int,
    now: datetime,
    dry_run: bool,
    actor_label: str,
) -> ClientPurge:
    rows = retention_rows(session, tenant_id)
    legal_hold = any(r["legal_hold"] for r in rows)
    days = effective_days(older_than_days, {r["data_class"]: r["retain_days"] for r in rows})
    result = ClientPurge(slug, legal_hold, days, now - timedelta(days=days))
    if not legal_hold:
        scrubbed_runs = artifacts = invocations = 0
        eligible = session.execute(_ELIGIBLE, {"t": tenant_id, "cutoff": result.cutoff}).scalars()
        for run_id in list(eligible):
            a, i = runs.unscrubbed_counts(session, tenant_id, run_id)
            if a == 0 and i == 0:
                continue
            scrubbed_runs, artifacts, invocations = (
                scrubbed_runs + 1,
                artifacts + a,
                invocations + i,
            )
            if not dry_run:
                runs.scrub_run_content(session, tenant_id, run_id)
        result = ClientPurge(
            slug, legal_hold, days, result.cutoff, scrubbed_runs, artifacts, invocations
        )
    if not dry_run:
        audit.record(
            session,
            RequestContext(request_id="maintenance-purge"),
            action=ACTION,
            tenant_id=tenant_id,
            actor_label=actor_label,
            target_type="retention",
            details={
                "older_than_days": older_than_days,
                "effective_days": days,
                "cutoff": result.cutoff.isoformat(),
                "runs": result.runs,
                "artifacts": result.artifacts,
                "tool_invocations": result.tool_invocations,
                "legal_hold": legal_hold,
            },
        )
    return result


def purge(
    database: Database,
    *,
    older_than_days: int,
    now: datetime,
    dry_run: bool,
    actor_label: str,
) -> Iterator[ClientPurge]:
    """Purge every client in id order, yielding each one's result once its transaction has
    committed. A database error rolls the current client back and propagates; clients already
    yielded stay committed."""
    with database.session() as session:
        with platform_admin_context(session):
            clients = session.execute(_CLIENTS).all()
        session.commit()
        for tenant_id, slug in clients:
            try:
                set_tenant(session, tenant_id)
                result = _client_pass(
                    session,
                    tenant_id,
                    slug,
                    older_than_days=older_than_days,
                    now=now,
                    dry_run=dry_run,
                    actor_label=actor_label,
                )
                if dry_run:
                    session.rollback()
                else:
                    session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                forget_tenant(session)
            yield result
