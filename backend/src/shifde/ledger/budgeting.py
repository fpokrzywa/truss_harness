"""Budget arithmetic shared by enforcement (runtime) and reporting (governance API): periods,
limits, settled usage from the ledger and held reservations. Pure SQL on the caller's session."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any  # Any: SQL rows
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow

PERIOD_ALIASES = {"day": "daily", "month": "monthly"}


def period_name(period: str) -> str:
    return PERIOD_ALIASES.get(period, period)


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    """Start of the UTC window (None = unbounded: `total` / `run`)."""
    now = now or utcnow()
    p = period_name(period)
    if p == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if p == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


USAGE_ENTITY = {
    "tenant": "TRUE",
    "workspace": "u.workspace_id = :scope_id",
    "project": "u.project_id = :scope_id",
    "agent": "u.agent_id = :scope_id",
    "pipeline": "u.pipeline_id = :scope_id",
    "principal": "u.principal_id = :scope_id",
}
RUN_ENTITY = {
    "tenant": "TRUE",
    "workspace": "r.workspace_id = :scope_id",
    "project": "r.project_id = :scope_id",
    "agent": "r.agent_id = :scope_id",
    "pipeline": "r.pipeline_id = :scope_id",
    "principal": "r.initiated_by_principal_id = :scope_id",
}
HELD_ENTITY = {  # reservations hang off the (possibly child) run that made the call
    **RUN_ENTITY,
    "pipeline": "rr.pipeline_id = :scope_id",  # the pipeline is the ROOT run
}


def _used_sql(
    tenant_id: UUID,
    root_run_id: UUID | None,
    b: Mapping[str, Any],  # Any: budget / quota row
    metric: str,
    since: datetime | None,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"t": tenant_id, "scope_id": b["scope_id"]}
    where = ["u.tenant_id = :t", USAGE_ENTITY[str(b["scope_type"])]]
    if period_name(str(b["period"])) == "run":
        where.append("u.root_run_id = :root")
        params["root"] = root_run_id
    elif since is not None:
        where.append("u.occurred_at >= :since")
        params["since"] = since
    if metric == "cost":
        select = "COALESCE(sum(u.cost_amount), 0)"
        where.append("u.currency = :cur")
        params["cur"] = b["currency"]
    else:
        select = "COALESCE(sum(u.input_tokens + u.output_tokens), 0)"
    return (
        # fixed fragments built in code; values are bound
        f"SELECT {select} FROM shifde.usage_events u WHERE {' AND '.join(where)}",  # nosec B608
        params,
    )


def _held_sql(
    tenant_id: UUID,
    root_run_id: UUID | None,
    b: Mapping[str, Any],  # Any: budget / quota row
    metric: str,
    since: datetime | None,
) -> tuple[str, dict[str, Any]]:
    """Held amount of the rule's window: run holds matched through their run's scope, plus
    direct-invocation holds (no run) that name the rule's id in `budget_ids` (never for a per-run
    budget). A hold counts while it is within its TTL or its provider call may have started (the
    marker): such a hold is never silently freed. `settled_unknown` is spent (its usage row)."""
    params: dict[str, Any] = {"t": tenant_id, "scope_id": b["scope_id"]}
    live = [
        "res.tenant_id = :t",
        "res.status = 'held'",
        "(res.expires_at > now() OR res.provider_request_at IS NOT NULL)",
    ]
    where = [*live, HELD_ENTITY[str(b["scope_type"])]]
    direct = [*live, "res.run_id IS NULL", "CAST(:rule_id AS uuid) = ANY (res.budget_ids)"]
    params["rule_id"] = b.get("id")
    if period_name(str(b["period"])) == "run":
        where.append("r.root_run_id = :root")
        params["root"] = root_run_id
        direct.append("FALSE")  # a per-run budget never counts a hold without a run
    elif since is not None:
        where.append("res.created_at >= :since")
        direct.append("res.created_at >= :since")
        params["since"] = since
    if metric == "cost":
        select = "COALESCE(sum(res.cost_amount), 0)"
        where.append("res.currency = :cur")
        direct.append("res.currency = :cur")
        params["cur"] = b["currency"]
    else:
        select = "COALESCE(sum(res.amount), 0)"
    return (
        # fixed fragments built in code; values are bound
        f"SELECT (SELECT {select} FROM shifde.budget_reservations res "  # nosec B608
        "JOIN shifde.runs r ON r.id = res.run_id AND r.tenant_id = res.tenant_id "
        "JOIN shifde.runs rr ON rr.id = r.root_run_id AND rr.tenant_id = res.tenant_id "
        f"WHERE {' AND '.join(where)}) + "
        f"(SELECT {select} FROM shifde.budget_reservations res "
        f"WHERE {' AND '.join(direct)})",
        params,
    )


def used_amount(
    session: Session,
    tenant_id: UUID,
    root_run_id: UUID | None,
    b: Mapping[str, Any],  # Any: budget / quota row
    *,
    metric: str,
    since: datetime | None,
) -> Decimal:
    """Settled usage of the budget's window from the ledger (tokens or known cost)."""
    sql, params = _used_sql(tenant_id, root_run_id, b, metric, since)
    return Decimal(session.execute(text(sql), params).scalar_one())


def held_amount(
    session: Session,
    tenant_id: UUID,
    root_run_id: UUID | None,
    b: Mapping[str, Any],  # Any: budget / quota row
    *,
    metric: str,
    since: datetime | None,
) -> Decimal:
    sql, params = _held_sql(tenant_id, root_run_id, b, metric, since)
    return Decimal(session.execute(text(sql), params).scalar_one())


def used_and_held(
    session: Session,
    tenant_id: UUID,
    root_run_id: UUID | None,
    b: Mapping[str, Any],  # Any: budget / quota row
    *,
    metric: str,
    since: datetime | None,
) -> tuple[Decimal, Decimal]:
    """Settled + held in ONE statement. Two statements under READ COMMITTED take two snapshots:
    a settle (usage event inserted, hold marked settled) committing in between would be counted
    by neither read and a hard limit could be overspent. One statement sees one snapshot, so the
    amount is either still held or already settled, never missing."""
    used_sql, params = _used_sql(tenant_id, root_run_id, b, metric, since)
    held_sql, held_params = _held_sql(tenant_id, root_run_id, b, metric, since)
    row = session.execute(
        text(f"SELECT ({used_sql}) AS used, ({held_sql}) AS held"), {**params, **held_params}
    ).one()
    return Decimal(row[0]), Decimal(row[1])


def effective_limit(b: Mapping[str, Any], metric: str) -> Decimal | None:  # Any: budget row
    """The limit in the budget's metric (P5 rows keep it in limit_tokens / limit_runs)."""
    if b["amount"] is not None:
        return Decimal(b["amount"])
    column = "limit_runs" if metric == "runs" else "limit_tokens"
    return None if b[column] is None else Decimal(b[column])
