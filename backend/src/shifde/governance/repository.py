"""SQL of the governance module (raw, parameterized; identifiers come from code constants).

Everything runs under the caller's RLS context (`app.tenant_id`); global rate cards have a NULL
tenant and are readable by every tenant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any  # Any: SQL parameter values / row values
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.ledger.usage import PRICEABLE, Visibility

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values
Params = dict[str, Any]  # Any: SQL parameters


def _j(value: Any) -> str:  # Any: JSON document
    return json.dumps(value, default=str)


# ------------------------------------------------------------------ rate cards
CARD_COLUMNS = "id, tenant_id, provider_key, model_key, name, created_by, created_at"
VERSION_COLUMNS = (
    "id, rate_card_id, tenant_id, version_no, effective_from, effective_to, currency, unit, "
    "input_rate, output_rate, cached_read_rate, cached_write_rate, other_rates, source, revision, "
    "created_by, created_at"
)


def insert_card(session: Session, v: Mapping[str, Any]) -> None:  # Any: column values
    session.execute(
        text(
            "INSERT INTO shifde.rate_cards (id, tenant_id, provider_key, model_key, name, "
            "created_by) VALUES (:id, :tenant_id, :provider_key, :model_key, :name, :created_by)"
        ),
        dict(v),
    )


# A tenant sees the platform's global cards (tenant_id NULL) and its own; the platform context
# (`tenant_id` None) sees only the global ones. RLS enforces the same; the filter is the second
# fence (B-1801).
CARD_VISIBLE = "(tenant_id = :t OR tenant_id IS NULL)"


def get_card(session: Session, tenant_id: UUID | None, card_id: UUID) -> Row | None:
    return (
        session.execute(
            text(f"SELECT {CARD_COLUMNS} FROM shifde.rate_cards WHERE id = :id AND {CARD_VISIBLE}"),
            {"id": card_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_cards(session: Session, tenant_id: UUID | None) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {CARD_COLUMNS} FROM shifde.rate_cards WHERE {CARD_VISIBLE} "
                "ORDER BY provider_key, model_key, (tenant_id IS NULL), id"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .all()
    )


def list_versions(session: Session, tenant_id: UUID | None, card_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {VERSION_COLUMNS} FROM shifde.rate_card_versions "
                f"WHERE rate_card_id = :c AND {CARD_VISIBLE} ORDER BY version_no DESC"
            ),
            {"c": card_id, "t": tenant_id},
        )
        .mappings()
        .all()
    )


def next_version_no(session: Session, tenant_id: UUID | None, card_id: UUID) -> int:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"ratecard:{card_id}"},
    )
    return int(
        session.execute(
            text(
                "SELECT COALESCE(max(version_no), 0) + 1 FROM shifde.rate_card_versions "
                f"WHERE rate_card_id = :c AND {CARD_VISIBLE}"
            ),
            {"c": card_id, "t": tenant_id},
        ).scalar_one()
    )


def insert_version(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    params = dict(v)
    params["other_rates"] = _j(params.get("other_rates") or {})
    return (
        session.execute(
            text(
                "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, version_no, "
                "effective_from, effective_to, currency, unit, input_rate, output_rate, "
                "cached_read_rate, cached_write_rate, other_rates, source, revision, created_by) "
                "VALUES (:id, :rate_card_id, :tenant_id, :version_no, :effective_from, "
                ":effective_to, :currency, :unit, :input_rate, :output_rate, :cached_read_rate, "
                ":cached_write_rate, CAST(:other_rates AS jsonb), :source, :revision, :created_by) "
                f"RETURNING {VERSION_COLUMNS}"
            ),
            params,
        )
        .mappings()
        .one()
    )


# ------------------------------------------------------------------ usage events
_EVENT_COLUMNS = (
    "SELECT u.id, u.event_key, u.occurred_at, u.event_type, u.status, u.run_id, u.root_run_id, "
    "u.step_id, {step_name} AS step_name, u.attempt, u.workspace_id, u.project_id, "
    "u.principal_id, u.agent_id, u.model_id, u.provider_key, u.model_key, u.is_test, "
    "u.input_tokens, u.output_tokens, u.cached_read_tokens, u.cached_write_tokens, u.call_count, "
    "u.duration_ms, u.cost_amount, u.currency, u.cost_kind, u.rate_card_version_id, "
    "u.rate_snapshot, u.corrects_event_id FROM shifde.usage_events u "
)


def event_select(session: Session) -> str:
    """The usage-event SELECT; the step name comes from the event's own client's `run_steps`."""
    return _EVENT_COLUMNS.format(step_name="rs.name") + (
        "LEFT JOIN shifde.run_steps rs ON rs.id = u.step_id AND rs.tenant_id = u.tenant_id "
    )


def list_events(
    session: Session,
    tenant_id: UUID,
    *,
    vis: Visibility,
    run_id: UUID | None,
    start: datetime | None,
    end: datetime | None,
    event_type: str | None,
    before: UUID | None,
    limit: int,
) -> list[Row]:
    params: Params = {"t": tenant_id, "limit": limit}
    where = ["u.tenant_id = :t", vis.clause(params, "u.")]
    if run_id is not None:
        where.append("(u.root_run_id = :run OR u.run_id = :run)")
        params["run"] = run_id
    if start is not None:
        where.append("u.occurred_at >= :start")
        params["start"] = start
    if end is not None:
        where.append("u.occurred_at < :end")
        params["end"] = end
    if event_type is not None:
        where.append("u.event_type = :et")
        params["et"] = event_type
    if before is not None:
        where.append("u.id < :before")
        params["before"] = before
    return list(
        session.execute(
            text(
                f"{event_select(session)}WHERE {' AND '.join(where)} "
                "ORDER BY u.id DESC LIMIT :limit"
            ),
            params,
        )
        .mappings()
        .all()
    )


def run_events(session: Session, tenant_id: UUID, run_id: UUID, vis: Visibility) -> list[Row]:
    params: Params = {"t": tenant_id, "run": run_id}
    return list(
        session.execute(
            text(
                f"{event_select(session)}WHERE u.tenant_id = :t AND (u.root_run_id = :run "
                f"OR u.run_id = :run) AND {vis.clause(params, 'u.')} "
                "ORDER BY u.occurred_at, u.id"
            ),
            params,
        )
        .mappings()
        .all()
    )


def export_events(
    session: Session,
    tenant_id: UUID,
    *,
    vis: Visibility,
    start: datetime,
    end: datetime,
    limit: int,
    workspace_id: UUID | None = None,
    project_id: UUID | None = None,
    cost_kind: str | None = None,
) -> list[Row]:
    """Filters are part of the WHERE (a Python-side filter after LIMIT would drop rows silently)."""
    params: Params = {"t": tenant_id, "start": start, "end": end, "limit": limit}
    extra = ""
    if workspace_id is not None:
        extra += " AND u.workspace_id = :ws"
        params["ws"] = workspace_id
    if project_id is not None:
        extra += " AND u.project_id = :pj"
        params["pj"] = project_id
    if cost_kind is not None:
        extra += " AND u.cost_kind = :ck"
        params["ck"] = cost_kind
    return list(
        session.execute(
            text(
                f"{event_select(session)}WHERE u.tenant_id = :t AND u.occurred_at >= :start "
                f"AND u.occurred_at < :end AND {vis.clause(params, 'u.')}{extra} "
                "ORDER BY u.occurred_at, u.id LIMIT :limit"
            ),
            params,
        )
        .mappings()
        .all()
    )


# ------------------------------------------------------------------ summary
DIM_COLUMNS = {
    "workspace": "workspace_id",
    "project": "project_id",
    "user": "principal_id",
    "agent": "agent_id",
    "prompt": "prompt_id",
    "model": "model_id",
    "pipeline": "pipeline_id",
    "workflow": "orchestration_id",
    "build": "build_ref",
}
_MEASURES = (
    "COALESCE(sum(input_tokens), 0) AS input_tokens, COALESCE(sum(output_tokens), 0) AS "
    "output_tokens, COALESCE(sum(cached_tokens), 0) AS cached_tokens, COALESCE(sum(calls), 0) AS "
    "calls, COALESCE(sum(duration_ms), 0) AS duration_ms, COALESCE(sum(cost_amount), 0) AS "
    "cost_amount, COALESCE(sum(event_count), 0) AS events, "
    "COALESCE(sum(event_count) FILTER (WHERE cost_kind = 'unmeasured' AND event_type IN "
    "('model_call', 'prompt_test')), 0) AS unmeasured_priceable"
)


def summary_rows(
    session: Session,
    tenant_id: UUID,
    group_by: str,
    from_day: date,
    to_day: date,
    *,
    vis: Visibility,
    workspace_id: UUID | None,
    project_id: UUID | None,
    cost_kind: str | None,
    limit: int,
) -> list[Row]:
    """Fine-grained rows (key x currency x cost kind x rate version) from the daily rollups, or
    from the events for `run` (no run dimension in the rollups)."""
    params: Params = {"t": tenant_id, "a": from_day, "b": to_day, "limit": limit}
    filters = [vis.clause(params)]
    if workspace_id is not None:
        filters.append("workspace_id = :ws")
        params["ws"] = workspace_id
    if project_id is not None:
        filters.append("project_id = :pj")
        params["pj"] = project_id
    if cost_kind is not None:
        filters.append("cost_kind = :ck")
        params["ck"] = cost_kind
    extra = " AND ".join(filters)
    if group_by == "run":
        # the most recent runs of the window (index range scan on occurred_at), then their events
        # by the root-run index: a window can hold millions of runs
        params["start"] = datetime(from_day.year, from_day.month, from_day.day, tzinfo=UTC)
        params["end"] = datetime(to_day.year, to_day.month, to_day.day, tzinfo=UTC) + timedelta(
            days=1
        )
        win = "tenant_id = :t AND occurred_at >= :start AND occurred_at < :end"
        sql = (
            "WITH recent AS (SELECT root_run_id FROM shifde.usage_events "
            f"WHERE {win} AND root_run_id IS NOT NULL AND {extra} "
            "ORDER BY occurred_at DESC LIMIT 50000), "
            f"ids AS (SELECT DISTINCT root_run_id FROM recent LIMIT {int(limit)}) "
            f"SELECT root_run_id::text AS key, currency, cost_kind, rate_card_version_id, "
            f"{_event_measures()} FROM shifde.usage_events WHERE tenant_id = :t "
            f"AND root_run_id = ANY (ARRAY(SELECT root_run_id FROM ids)) AND {extra} "
            "GROUP BY 1, 2, 3, 4"
        )
    else:
        col = DIM_COLUMNS[group_by]
        sql = (
            f"SELECT {col}::text AS key, currency, cost_kind, rate_card_version_id, {_MEASURES} "
            "FROM shifde.usage_rollups_daily WHERE tenant_id = :t AND day >= :a AND day <= :b "
            f"AND {col} IS NOT NULL AND {extra} GROUP BY 1, 2, 3, 4"
        )
    return list(session.execute(text(sql), params).mappings().all())


def totals_rows(
    session: Session,
    tenant_id: UUID,
    from_day: date,
    to_day: date,
    *,
    vis: Visibility,
    workspace_id: UUID | None,
    project_id: UUID | None,
    cost_kind: str | None,
) -> list[Row]:
    """The whole window (every event, whatever its dimensions) by currency / kind / version."""
    params: Params = {"t": tenant_id, "a": from_day, "b": to_day}
    filters = [vis.clause(params)]
    if workspace_id is not None:
        filters.append("workspace_id = :ws")
        params["ws"] = workspace_id
    if project_id is not None:
        filters.append("project_id = :pj")
        params["pj"] = project_id
    if cost_kind is not None:
        filters.append("cost_kind = :ck")
        params["ck"] = cost_kind
    return list(
        session.execute(
            text(
                f"SELECT 'all' AS key, currency, cost_kind, rate_card_version_id, {_MEASURES} "
                "FROM shifde.usage_rollups_daily WHERE tenant_id = :t AND day >= :a AND day <= :b "
                f"AND {' AND '.join(filters)} GROUP BY 1, 2, 3, 4"
            ),
            params,
        )
        .mappings()
        .all()
    )


def _event_measures() -> str:
    priceable = ", ".join(f"'{p}'" for p in PRICEABLE)
    return (
        "COALESCE(sum(input_tokens), 0) AS input_tokens, COALESCE(sum(output_tokens), 0) AS "
        "output_tokens, COALESCE(sum(cached_read_tokens + cached_write_tokens), 0) AS "
        "cached_tokens, COALESCE(sum(call_count), 0) AS calls, COALESCE(sum(duration_ms), 0) AS "
        "duration_ms, COALESCE(sum(cost_amount), 0) AS cost_amount, count(*) AS events, "
        "count(*) FILTER (WHERE cost_kind = 'unmeasured' AND event_type IN "
        f"({priceable})) AS unmeasured_priceable"
    )


def rate_versions(session: Session, tenant_id: UUID, ids: Sequence[UUID]) -> dict[UUID, int]:
    if not ids:
        return {}
    rows = session.execute(
        text(
            "SELECT id, version_no FROM shifde.rate_card_versions WHERE id = ANY (:ids) "
            "AND (tenant_id = :t OR tenant_id IS NULL)"
        ),
        {"ids": list(ids), "t": tenant_id},
    )
    return {r[0]: int(r[1]) for r in rows}


def labels(session: Session, tenant_id: UUID, group_by: str, keys: Sequence[str]) -> dict[str, str]:
    if not keys or group_by in ("build",):
        return {k: k for k in keys}
    if group_by == "run":
        return {k: f"run {k[:8]}" for k in keys}
    ids = [UUID(k) for k in keys]
    queries = {
        "workspace": (
            "SELECT id::text, name FROM shifde.workspaces WHERE id = ANY (:ids) AND tenant_id = :t"
        ),
        "project": (
            "SELECT id::text, name FROM shifde.projects WHERE id = ANY (:ids) AND tenant_id = :t"
        ),
        # a model row may be the platform's shared one (tenant_id NULL)
        "model": (
            "SELECT id::text, display_name FROM shifde.models WHERE id = ANY (:ids) "
            "AND (tenant_id = :t OR tenant_id IS NULL)"
        ),
        # users are platform-level (no tenant column): only the tenant's members' names are shown
        "user": (
            "SELECT u.id::text, u.display_name FROM shifde.users u WHERE u.id = ANY (:ids) "
            "AND EXISTS (SELECT 1 FROM shifde.memberships m WHERE m.user_id = u.id "
            "AND m.tenant_id = :t) "
            "UNION ALL SELECT id::text, name FROM shifde.service_accounts WHERE id = ANY (:ids) "
            "AND tenant_id = :t"
        ),
    }
    sql = queries.get(
        group_by,
        "SELECT id::text, COALESCE(NULLIF(name, ''), key) FROM shifde.artifacts "
        "WHERE id = ANY (:ids) AND tenant_id = :t",
    )
    return {r[0]: str(r[1]) for r in session.execute(text(sql), {"ids": ids, "t": tenant_id})}


# ------------------------------------------------------------------ budgets
BUDGET_COLUMNS = (
    "id, tenant_id, name, scope_type, scope_id, period, metric, limit_tokens, limit_runs, "
    "amount, currency, enforcement, on_exceed, alert_thresholds, status, row_version, "
    "created_by, created_at, updated_at"
)
QUOTA_COLUMNS = (
    "id, tenant_id, scope_type, scope_id, metric, limit_value, status, row_version, created_by, "
    "created_at, updated_at"
)


def insert_budget(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    return (
        session.execute(
            text(
                "INSERT INTO shifde.budgets (id, tenant_id, name, scope_type, scope_id, period, "
                "metric, limit_tokens, amount, currency, enforcement, on_exceed, "
                "alert_thresholds, status, created_by) VALUES (:id, :tenant_id, :name, "
                ":scope_type, :scope_id, :period, :metric, :limit_tokens, :amount, :currency, "
                ":enforcement, :on_exceed, :alert_thresholds, :status, :created_by) "
                f"RETURNING {BUDGET_COLUMNS}"
            ),
            dict(v),
        )
        .mappings()
        .one()
    )


def get_budget(session: Session, tenant_id: UUID, budget_id: UUID) -> Row | None:
    return (
        session.execute(
            text(f"SELECT {BUDGET_COLUMNS} FROM shifde.budgets WHERE id = :id AND tenant_id = :t"),
            {"id": budget_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_budgets(session: Session, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(f"SELECT {BUDGET_COLUMNS} FROM shifde.budgets WHERE tenant_id = :t ORDER BY id"),
            {"t": tenant_id},
        )
        .mappings()
        .all()
    )


BUDGET_UPDATABLE = frozenset(
    {
        "name",
        "period",
        "amount",
        "limit_tokens",
        "limit_runs",
        "currency",
        "enforcement",
        "on_exceed",
        "alert_thresholds",
        "status",
    }
)
QUOTA_UPDATABLE = frozenset({"limit_value", "status"})
POLICY_UPDATABLE = frozenset({"name", "definition", "status", "version"})


def update_row(
    session: Session,
    tenant_id: UUID,
    table: str,
    columns: str,
    updatable: frozenset[str],
    row_id: UUID,
    expected_version: int | None,
    values: Mapping[str, Any],  # Any: column values
    json_columns: frozenset[str] = frozenset(),
) -> Row | None:
    """Optimistic-locking UPDATE ... RETURNING over a code-owned column allowlist."""
    sets = ["row_version = row_version + 1", "updated_at = now()"]
    params: Params = {"id": row_id, "expected": expected_version, "t": tenant_id}
    for column, value in values.items():
        if column not in updatable:
            raise ValueError(f"column {column!r} is not updatable")
        if column in json_columns:
            sets.append(f"{column} = CAST(:{column} AS jsonb)")
            params[column] = _j(value)
        else:
            sets.append(f"{column} = :{column}")
            params[column] = value
    return (
        session.execute(
            text(
                f"UPDATE shifde.{table} SET {', '.join(sets)} WHERE id = :id "
                "AND tenant_id = :t "
                "AND (CAST(:expected AS int) IS NULL OR row_version = :expected) "
                f"RETURNING {columns}"
            ),
            params,
        )
        .mappings()
        .first()
    )


def budget_alerts(
    session: Session, tenant_id: UUID, budget_id: UUID, period_start: datetime
) -> list[float]:
    rows = session.execute(
        text(
            "SELECT threshold FROM shifde.budget_alerts WHERE budget_id = :b "
            "AND tenant_id = :t AND period_start = :ps ORDER BY threshold"
        ),
        {"b": budget_id, "t": tenant_id, "ps": period_start},
    )
    return [float(r[0]) for r in rows]


def stale_holds(session: Session, tenant_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.budget_reservations WHERE tenant_id = :t "
                "AND status = 'held' AND expires_at <= now()"
            ),
            {"t": tenant_id},
        ).scalar_one()
    )


# ------------------------------------------------------------------ quotas
def insert_quota(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    return (
        session.execute(
            text(
                "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, "
                "limit_value, created_by) VALUES (:id, :tenant_id, :scope_type, :scope_id, "
                f":metric, :limit_value, :created_by) RETURNING {QUOTA_COLUMNS}"
            ),
            dict(v),
        )
        .mappings()
        .one()
    )


def get_quota(session: Session, tenant_id: UUID, quota_id: UUID) -> Row | None:
    return (
        session.execute(
            text(f"SELECT {QUOTA_COLUMNS} FROM shifde.quotas WHERE id = :id AND tenant_id = :t"),
            {"id": quota_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_quotas(session: Session, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(f"SELECT {QUOTA_COLUMNS} FROM shifde.quotas WHERE tenant_id = :t ORDER BY id"),
            {"t": tenant_id},
        )
        .mappings()
        .all()
    )


# ------------------------------------------------------------------ scope resolution
def scope_workspace_project(
    session: Session, tenant_id: UUID, scope_type: str, scope_id: UUID
) -> tuple[UUID | None, UUID | None] | None:
    """(workspace_id, project_id) a scope lives in; (None, None) = tenant-level; None = unknown
    to this tenant (cross-tenant ids answer exactly like nonexistent ones)."""
    if scope_type == "workspace":
        row = session.execute(
            text("SELECT id FROM shifde.workspaces WHERE id = :i AND tenant_id = :t"),
            {"i": scope_id, "t": tenant_id},
        ).first()
        return (row[0], None) if row else None
    if scope_type == "project":
        row = session.execute(
            text("SELECT workspace_id, id FROM shifde.projects WHERE id = :i AND tenant_id = :t"),
            {"i": scope_id, "t": tenant_id},
        ).first()
        return (row[0], row[1]) if row else None
    if scope_type in ("agent", "pipeline"):
        kind = "agents" if scope_type == "agent" else "pipelines"
        row = session.execute(
            text(
                "SELECT workspace_id, project_id FROM shifde.artifacts "
                "WHERE id = :i AND kind = :k AND tenant_id = :t"
            ),
            {"i": scope_id, "k": kind, "t": tenant_id},
        ).first()
        return (row[0], row[1]) if row else None
    if scope_type == "principal":
        row = session.execute(
            text("SELECT user_id FROM shifde.memberships WHERE user_id = :i AND tenant_id = :t"),
            {"i": scope_id, "t": tenant_id},
        ).first()
        if row is None:
            row = session.execute(
                text("SELECT id FROM shifde.service_accounts WHERE id = :i AND tenant_id = :t"),
                {"i": scope_id, "t": tenant_id},
            ).first()
        return (None, None) if row else None
    return (None, None)


# ------------------------------------------------------------------ policies
POLICY_COLUMNS = (
    "id, tenant_id, kind, name, scope_type, scope_id, definition, status, version, row_version, "
    "created_by, created_at, updated_at"
)


def insert_policy(session: Session, v: Mapping[str, Any]) -> Row:  # Any: column values
    params = dict(v)
    params["definition"] = _j(params["definition"])
    return (
        session.execute(
            text(
                "INSERT INTO shifde.governance_policies (id, tenant_id, kind, name, scope_type, "
                "scope_id, definition, status, created_by) VALUES (:id, :tenant_id, :kind, :name, "
                ":scope_type, :scope_id, CAST(:definition AS jsonb), :status, :created_by) "
                f"RETURNING {POLICY_COLUMNS}"
            ),
            params,
        )
        .mappings()
        .one()
    )


def get_policy(session: Session, tenant_id: UUID, policy_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {POLICY_COLUMNS} FROM shifde.governance_policies "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": policy_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_policies(session: Session, tenant_id: UUID, kind: str | None) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {POLICY_COLUMNS} FROM shifde.governance_policies "
                "WHERE tenant_id = :t AND (CAST(:kind AS text) IS NULL OR kind = :kind) ORDER BY id"
            ),
            {"kind": kind, "t": tenant_id},
        )
        .mappings()
        .all()
    )


def policy_counts(session: Session, tenant_id: UUID) -> dict[str, int]:
    rows = session.execute(
        text(
            "SELECT kind, count(*) FROM shifde.governance_policies "
            "WHERE tenant_id = :t AND status = 'active' GROUP BY kind"
        ),
        {"t": tenant_id},
    )
    return {r[0]: int(r[1]) for r in rows}


# ------------------------------------------------------------------ decisions
def list_decisions(
    session: Session,
    tenant_id: UUID,
    *,
    run_id: UUID | None,
    decision: str | None,
    action: str | None,
    before: UUID | None,
    limit: int,
) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT d.id, d.at, d.decision, d.action, d.resource_type, d.resource_id, "
                "d.reason_codes, d.policy_ids, d.run_id, "
                "COALESCE(u.display_name, sa.name, d.principal_id::text) AS principal "
                "FROM shifde.policy_decisions d "
                "LEFT JOIN shifde.users u ON u.id = d.principal_id "
                "LEFT JOIN shifde.service_accounts sa ON sa.id = d.principal_id "
                "WHERE d.tenant_id = :t AND (CAST(:run AS uuid) IS NULL OR d.run_id = :run) "
                "AND (CAST(:dec AS text) IS NULL OR d.decision = :dec) "
                "AND (CAST(:act AS text) IS NULL OR d.action = :act) "
                "AND (CAST(:before AS uuid) IS NULL OR d.id < :before) "
                "ORDER BY d.id DESC LIMIT :limit"
            ),
            {
                "t": tenant_id,
                "run": run_id,
                "dec": decision,
                "act": action,
                "before": before,
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )


def denials_since(session: Session, tenant_id: UUID, since: datetime) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.policy_decisions WHERE tenant_id = :t AND at >= :s "
                "AND decision = 'deny' AND ('budget_denied' = ANY (reason_codes) "
                "OR cardinality(policy_ids) > 0)"
            ),
            {"t": tenant_id, "s": since},
        ).scalar_one()
    )


def pending_approvals(session: Session, tenant_id: UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shifde.approvals WHERE tenant_id = :t AND status = 'pending'"
            ),
            {"t": tenant_id},
        ).scalar_one()
    )


# ------------------------------------------------------------------ retention
def retention_rows(session: Session, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT data_class, retain_days, legal_hold FROM shifde.retention_policies "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .all()
    )


def upsert_retention(
    session: Session, tenant_id: UUID, data_class: str, days: int, hold: bool, actor: UUID
) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.retention_policies (tenant_id, data_class, retain_days, "
            "legal_hold, updated_by) VALUES (:t, :c, :d, :h, :a) "
            "ON CONFLICT (tenant_id, data_class) DO UPDATE SET retain_days = :d, "
            "legal_hold = :h, updated_by = :a, updated_at = now()"
        ),
        {"t": tenant_id, "c": data_class, "d": days, "h": hold, "a": actor},
    )


RETENTION_TABLES = {
    "run_artifacts": ("run_artifacts", "created_at", "kind <> 'intermediate'"),
    "run_steps_content": ("run_artifacts", "created_at", "kind = 'intermediate'"),
    "audit_events": ("audit_events", "occurred_at", "TRUE"),
    "usage_events": ("usage_events", "occurred_at", "TRUE"),
    "policy_decisions": ("policy_decisions", "at", "TRUE"),
}


def count_older_than(session: Session, tenant_id: UUID, data_class: str, cutoff: datetime) -> int:
    table, column, extra = RETENTION_TABLES[data_class]
    return int(
        session.execute(
            text(
                f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :t "
                f"AND {column} < :cutoff AND {extra}"
            ),
            {"t": tenant_id, "cutoff": cutoff},
        ).scalar_one()
    )


# ------------------------------------------------------------------ performance
def performance(
    session: Session,
    tenant_id: UUID,
    group_by: str,
    start: datetime,
    end: datetime,
    vis: Visibility,
) -> list[Row]:
    params: Params = {"t": tenant_id, "a": start, "b": end}
    quant = (
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY dur) AS p50, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY dur) AS p95, "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY dur) AS p99"
    )
    if group_by == "model":
        sql = (
            f"SELECT key, count(*) AS n, count(*) FILTER (WHERE failed) AS errors, {quant} FROM ("
            "SELECT s.model_id::text AS key, s.status = 'failed' AS failed, "
            "EXTRACT(EPOCH FROM (s.finished_at - s.started_at)) * 1000 AS dur "
            "FROM shifde.run_steps s JOIN shifde.runs r ON r.id = s.run_id "
            "WHERE s.tenant_id = :t AND s.kind = 'model_call' AND s.model_id IS NOT NULL "
            "AND s.finished_at >= :a AND s.finished_at < :b "
            f"AND {vis.clause(params, 'r.')}) q GROUP BY key"
        )
    else:
        col = "agent_id" if group_by == "agent" else "pipeline_id"
        sql = (
            f"SELECT key, count(*) AS n, count(*) FILTER (WHERE failed) AS errors, {quant} FROM ("
            f"SELECT r.{col}::text AS key, r.status IN ('failed', 'timed_out') AS failed, "
            "EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) * 1000 AS dur "
            f"FROM shifde.runs r WHERE r.tenant_id = :t AND r.{col} IS NOT NULL "
            "AND r.started_at IS NOT NULL AND r.finished_at >= :a AND r.finished_at < :b "
            "AND r.status <> 'cancelled' "
            f"AND {vis.clause(params, 'r.')}) q GROUP BY key"
        )
    return list(session.execute(text(sql), params).mappings().all())


def ops_stats(session: Session, window_seconds: int) -> dict[str, Any]:  # Any: JSON document
    value = session.execute(
        text("SELECT shifde.shifde_ops_stats(:w)"), {"w": window_seconds}
    ).scalar_one()
    return dict(value) if isinstance(value, dict) else dict(json.loads(value))


def workers(session: Session) -> list[Row]:
    return list(
        session.execute(
            text(
                "SELECT id, hostname, status, version, concurrency, heartbeat_at, last_reap_at "
                "FROM shifde.workers ORDER BY heartbeat_at DESC LIMIT 50"
            )
        )
        .mappings()
        .all()
    )


def new_id() -> UUID:
    return uuid7()
