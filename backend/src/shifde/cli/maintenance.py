"""`shifde-admin maintenance purge` and `delete`: scrub the stored content of old, ended runs, or
delete them (F-11, Item 20).

Runs as the application role, so row-level security confines each client's pass: one transaction
lists every client under the platform-administrator context, then each client is processed in a
transaction of its own with only that client set. Only the two scrubs migration `runtime_0002`
permits are issued (artifact content -> `{"scrubbed": true}`, tool-call arguments -> NULL); nothing
is deleted. A client with a legal hold on any data class is skipped. A real run appends one
`maintenance.retention.purged` audit event per client through the normal audit path.

`delete` (B-2783) uses the same clients, cutoff and legal-hold rule and deletes whole run families
(the client's runs of one `root_run_id`) whose every run is eligible and none of which is pinned
(a held budget reservation, a pending approval, a queued or leased job, or a case run of a passed
evaluation of a release that is not withdrawn and that a delivery plan names). Migration
`runtime_0006` admits the deletes only for runs that ended before the cutoff the pass sets in
`shifde.retention_delete_cutoff`. Usage, policy-decision and audit rows are kept.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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
DELETE_ACTION = "maintenance.retention.deleted"
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


def _each_client[T](
    database: Database, client_pass: Callable[[Session, UUID, str], T], *, dry_run: bool
) -> Iterator[T]:
    """Run `client_pass` for every client in id order, yielding each one's result once its
    transaction has committed (rolled back on `dry_run`). A database error rolls the current
    client back and propagates; clients already yielded stay committed."""
    with database.session() as session:
        # The CLI's trust boundary is the `shifde_app` database credential: whoever holds it may
        # run this command. The platform-admin flag is set here without a PDP check and is used
        # only to list tenant ids and slugs; each client is then processed under its own tenant
        # context.
        with platform_admin_context(session):
            clients = session.execute(_CLIENTS).all()
        session.commit()
        for tenant_id, slug in clients:
            try:
                set_tenant(session, tenant_id)
                result = client_pass(session, tenant_id, slug)
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

    def client_pass(session: Session, tenant_id: UUID, slug: str) -> ClientPurge:
        return _client_pass(
            session,
            tenant_id,
            slug,
            older_than_days=older_than_days,
            now=now,
            dry_run=dry_run,
            actor_label=actor_label,
        )

    yield from _each_client(database, client_pass, dry_run=dry_run)


# ------------------------------------------------------------------ delete (B-2783)
# The issue's order, except run_steps before tool_invocations: `fk_run_steps__tool_invocation`
# refuses the other order and no key is deferrable. `runs` goes last.
DELETE_TABLES = (
    "run_artifacts",
    "run_events",
    "approvals",
    "run_steps",
    "tool_invocations",
    "jobs",
    "ontology_query_lineage",
    "budget_reservations",
)
# Every run of each family that has an eligible run (`_ELIGIBLE`'s rule): whether it is eligible
# and whether something pins it.
_FAMILY_RUNS = text(
    "WITH eligible AS (SELECT r.id FROM shifde.runs r JOIN shifde.runs root "
    "ON root.id = r.root_run_id AND root.tenant_id = :t "
    "WHERE r.tenant_id = :t AND r.status IN ('completed', 'failed', 'cancelled', 'timed_out') "
    "AND r.finished_at IS NOT NULL AND r.finished_at < :cutoff "
    "AND root.status IN ('completed', 'failed', 'cancelled', 'timed_out')) "
    "SELECT r.id, r.root_run_id, r.id IN (SELECT id FROM eligible) AS eligible, ("
    "EXISTS (SELECT 1 FROM shifde.budget_reservations b WHERE b.tenant_id = :t "
    "AND b.run_id = r.id AND b.status = 'held') "
    "OR EXISTS (SELECT 1 FROM shifde.approvals a WHERE a.tenant_id = :t "
    "AND a.run_id = r.id AND a.status = 'pending') "
    "OR EXISTS (SELECT 1 FROM shifde.jobs j WHERE j.tenant_id = :t "
    "AND j.run_id = r.id AND j.status IN ('queued', 'leased')) "
    "OR EXISTS (SELECT 1 FROM shifde.evaluation_runs e JOIN shifde.releases rel "
    "ON rel.tenant_id = :t AND rel.id = e.release_id "
    "WHERE e.tenant_id = :t AND e.status = 'passed' AND rel.status <> 'withdrawn' "
    "AND e.case_runs @> jsonb_build_array(jsonb_build_object('run_id', r.id::text)) "
    "AND EXISTS (SELECT 1 FROM shifde.delivery_plans p WHERE p.tenant_id = :t "
    "AND p.evaluation_run_id = e.id))) AS pinned "
    "FROM shifde.runs r WHERE r.tenant_id = :t AND r.root_run_id IN "
    "(SELECT x.root_run_id FROM shifde.runs x WHERE x.tenant_id = :t "
    "AND x.id IN (SELECT id FROM eligible)) ORDER BY r.id"
)
_SET_CUTOFF = text("SELECT set_config('shifde.retention_delete_cutoff', :c, true)")


@dataclass(frozen=True)
class ClientDelete:
    slug: str
    legal_hold: bool
    effective_days: int
    cutoff: datetime
    runs: int = 0
    runs_kept: int = 0
    counts: tuple[tuple[str, int], ...] = tuple((t, 0) for t in DELETE_TABLES)


def _deletable(session: Session, tenant_id: UUID, cutoff: datetime) -> tuple[list[UUID], int]:
    """(the runs of every family to delete, the eligible runs kept because their family is)."""
    families: dict[UUID, list[tuple[UUID, bool, bool]]] = {}
    rows = session.execute(_FAMILY_RUNS, {"t": tenant_id, "cutoff": cutoff}).all()
    for run_id, root, eligible, pinned in rows:
        families.setdefault(root, []).append((run_id, eligible, pinned))
    selected: list[UUID] = []
    kept = 0
    for members in families.values():
        if all(eligible and not pinned for _, eligible, pinned in members):
            selected += [run_id for run_id, _, _ in members]
        else:
            kept += sum(1 for _, eligible, _ in members if eligible)
    return selected, kept


def _delete_pass(
    session: Session,
    tenant_id: UUID,
    slug: str,
    *,
    older_than_days: int,
    now: datetime,
    dry_run: bool,
    actor_label: str,
) -> ClientDelete:
    rows = retention_rows(session, tenant_id)
    legal_hold = any(r["legal_hold"] for r in rows)
    days = effective_days(older_than_days, {r["data_class"]: r["retain_days"] for r in rows})
    result = ClientDelete(slug, legal_hold, days, now - timedelta(days=days))
    if not legal_hold:
        selected, kept = _deletable(session, tenant_id, result.cutoff)
        params = {"t": tenant_id, "ids": selected}
        if not dry_run:
            session.execute(_SET_CUTOFF, {"c": result.cutoff.isoformat()})
        counts: list[tuple[str, int]] = []
        for table in (*DELETE_TABLES, "runs"):
            column = "id" if table == "runs" else "run_id"
            # table and column come from the constants above
            where = f"FROM shifde.{table} WHERE tenant_id = :t AND {column} = ANY(:ids)"  # nosec B608
            if dry_run:
                n = session.execute(text(f"SELECT count(*) {where}"), params).scalar_one()
            else:
                n = session.execute(text(f"DELETE {where}"), params).rowcount  # type: ignore[attr-defined]
            counts.append((table, int(n)))
        _, deleted_runs = counts.pop()
        result = ClientDelete(
            slug, legal_hold, days, result.cutoff, deleted_runs, kept, tuple(counts)
        )
    if not dry_run:
        audit.record(
            session,
            RequestContext(request_id="maintenance-delete"),
            action=DELETE_ACTION,
            tenant_id=tenant_id,
            actor_label=actor_label,
            target_type="retention",
            details={
                "older_than_days": older_than_days,
                "effective_days": days,
                "cutoff": result.cutoff.isoformat(),
                "runs": result.runs,
                "runs_kept": result.runs_kept,
                **dict(result.counts),
                "legal_hold": legal_hold,
            },
        )
    return result


def delete(
    database: Database,
    *,
    older_than_days: int,
    now: datetime,
    dry_run: bool,
    actor_label: str,
) -> Iterator[ClientDelete]:
    """Delete every client's old ended run families in id order, yielding each one's result once
    its transaction has committed. A database error rolls the current client back and
    propagates; clients already yielded stay committed."""

    def client_pass(session: Session, tenant_id: UUID, slug: str) -> ClientDelete:
        return _delete_pass(
            session,
            tenant_id,
            slug,
            older_than_days=older_than_days,
            now=now,
            dry_run=dry_run,
            actor_label=actor_label,
        )

    yield from _each_client(database, client_pass, dry_run=dry_run)
