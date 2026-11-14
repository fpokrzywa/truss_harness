"""The usage ledger: append-only events plus their write-through daily rollups (FR-COST-001..007).

`record` is the single writer. One call = one idempotent event (`event_key` is unique per tenant:
a replay of the same step attempt inserts nothing and changes no rollup, so a retry is counted
once, and every real retry attempt has its own key). When the insert happens, the matching
`usage_rollups_daily` row is upserted in the SAME transaction, so rollups equal the sum of events
by construction; `reconcile` proves it (and detects a tampered or drifted rollup) and `rebuild`
repairs it from the events.

Corrections are compensating rows (`corrects_event_id`, negative deltas), never updates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any  # Any: column values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.ledger import estimate, pricing

# `estimated`: priced from token counts the platform estimated because the provider reported none
# (Item 11); `calculated` is only for counts the provider reported.
COST_KINDS = ("provider_reported", "calculated", "estimated", "estimated_infra", "unmeasured")
# Only these event types are PRICEABLE by a rate card (model usage). Other events (tool calls,
# ontology queries, pipeline nodes) carry no cost; they are not counted as "unmeasured".
PRICEABLE = ("model_call", "prompt_test")

EVENT_COLUMNS = (
    "event_key", "event_type", "status", "workspace_id", "project_id", "principal_id", "run_id",
    "root_run_id", "step_id", "attempt", "agent_id", "agent_version_id", "prompt_version_id",
    "model_id", "is_test", "input_tokens", "output_tokens", "cached_read_tokens", "call_count",
    "duration_ms", "tenant_id", "pipeline_version_id", "node_id", "provider_key", "model_key",
    "cached_write_tokens", "prompt_id", "pipeline_id", "orchestration_id", "build_ref",
    "rate_card_version_id", "currency", "cost_amount", "cost_kind", "corrects_event_id",
    "occurred_at",
)  # fmt: skip
DIMS = (
    "workspace_id", "project_id", "principal_id", "agent_id", "prompt_id", "model_id",
    "pipeline_id", "orchestration_id", "build_ref", "rate_card_version_id",
)  # fmt: skip


class UsageNotReported(ValueError):
    """A succeeded model call was offered without token counts. The ledger never stores a missing
    count as 0: estimate it (`estimate.resolve_usage`, `record_model_call`) and label it so."""


def j(value: Any) -> str:  # Any: JSON document
    return json.dumps(value, default=str)


def dim_key(v: Mapping[Any, Any]) -> str:  # Any: dimension values / RowMapping
    parts = [str(v.get(d) or "") for d in (*DIMS, "event_type", "cost_kind", "currency")]
    return hashlib.sha256(
        "|".join(parts).encode()
    ).hexdigest()  # same expression as the p08 backfill


def utc_day(at: datetime) -> date:
    return at.astimezone(UTC).date()


@dataclass(frozen=True)
class Visibility:
    """Which rows a caller may see: everything in the tenant, or specific workspaces / projects."""

    all: bool = True
    workspace_ids: tuple[UUID, ...] = ()
    project_ids: tuple[UUID, ...] = ()

    def clause(self, params: dict[str, Any], alias: str = "") -> str:  # Any: SQL parameters
        if self.all:
            return "TRUE"
        params["vis_ws"] = list(self.workspace_ids)
        params["vis_pj"] = list(self.project_ids)
        return (
            f"({alias}workspace_id = ANY (CAST(:vis_ws AS uuid[])) "
            f"OR {alias}project_id = ANY (CAST(:vis_pj AS uuid[])))"
        )


ALL = Visibility()


@dataclass
class EventResult:
    inserted: bool
    event_id: UUID
    costed: pricing.Costed = field(default=pricing.UNMEASURED)


def _fill_attribution(session: Session, v: dict[str, Any]) -> None:  # Any: column values
    """Derive the artifact-level dimensions (prompt / pipeline / workflow) from the run tree."""
    if v.get("prompt_id") is None and v.get("prompt_version_id") is not None:
        row = session.execute(
            text(
                "SELECT artifact_id FROM shifde.artifact_versions WHERE id = :id AND tenant_id = :t"
            ),
            {"id": v["prompt_version_id"], "t": v["tenant_id"]},
        ).first()
        v["prompt_id"] = row[0] if row else None
    root = v.get("root_run_id")
    if root is not None and (v.get("pipeline_id") is None or v.get("orchestration_id") is None):
        row = session.execute(
            text(
                "SELECT pipeline_id, orchestration_id FROM shifde.runs "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": root, "t": v["tenant_id"]},
        ).first()
        if row:
            v["pipeline_id"] = v.get("pipeline_id") or row[0]
            v["orchestration_id"] = v.get("orchestration_id") or row[1]


def price_model_usage(
    session: Session,
    tenant_id: UUID,
    model_id: UUID | None,
    usage: pricing.Usage,
    *,
    at: datetime | None = None,
    provider_reported_cost: Decimal | None = None,
    provider_reported_currency: str | None = None,
    estimated: bool = False,
) -> tuple[pricing.Costed, str | None, str | None]:
    """(cost, provider_key, model_key) of one model call. A provider-reported amount wins; the
    calculated rate (if any) is then still kept as the snapshot for comparison. `estimated`: the
    token counts are the platform's estimate (priced as kind `estimated`, not `calculated`)."""
    keys = pricing.model_keys(session, tenant_id, model_id) if model_id is not None else None
    version = (
        pricing.resolve(session, tenant_id, keys[0], keys[1], at or utcnow()) if keys else None
    )
    calculated = pricing.price(version, usage, estimated=estimated)
    if provider_reported_cost is not None:
        currency = provider_reported_currency or calculated.currency
        if currency is None:
            raise ValueError("a provider-reported cost needs a currency")
        costed = pricing.Costed(
            "provider_reported",
            provider_reported_cost.quantize(pricing.QUANT),
            currency,
            calculated.rate_card_version_id,
            calculated.snapshot,
        )
        return costed, keys[0] if keys else None, keys[1] if keys else None
    return calculated, keys[0] if keys else None, keys[1] if keys else None


def record(session: Session, values: Mapping[str, Any]) -> EventResult:  # Any: column values
    """Insert one usage event (+ rollup). `values` may carry `costed` (a `pricing.Costed`)."""
    v: dict[str, Any] = dict(values)  # Any: column values
    costed: pricing.Costed = v.pop("costed", None) or pricing.UNMEASURED
    reported = v.pop("provider_reported_usage", {})
    v["cost_kind"] = costed.kind
    v["cost_amount"] = costed.amount
    v["currency"] = costed.currency
    v["rate_card_version_id"] = costed.rate_card_version_id
    v.setdefault("occurred_at", utcnow())
    if (
        v.get("event_type") in PRICEABLE
        and v.get("status", "succeeded") == "succeeded"
        and (v.get("input_tokens") is None or v.get("output_tokens") is None)
    ):
        raise UsageNotReported(
            "a succeeded model call needs input and output token counts; estimate what the "
            "provider did not report (ledger.record_model_call) instead of storing 0"
        )
    for numeric in ("input_tokens", "output_tokens", "cached_read_tokens", "cached_write_tokens"):
        v[numeric] = v.get(numeric) or 0
    if v.get("call_count") is None:
        v["call_count"] = 1
    v.setdefault("status", "succeeded")
    v.setdefault("is_test", False)
    _fill_attribution(session, v)
    event_id = uuid7()
    params = {c: v.get(c) for c in EVENT_COLUMNS}
    params["id"] = event_id
    params["reported"] = j(reported)
    params["snapshot"] = j(costed.snapshot) if costed.snapshot is not None else None
    row = session.execute(
        text(
            f"INSERT INTO shifde.usage_events (id, {', '.join(EVENT_COLUMNS)}, "
            "provider_reported_usage, rate_snapshot) "
            f"VALUES (:id, {', '.join(':' + c for c in EVENT_COLUMNS)}, "
            "CAST(:reported AS jsonb), CAST(:snapshot AS jsonb)) "
            "ON CONFLICT (tenant_id, event_key) DO NOTHING RETURNING id"
        ),
        params,
    ).first()
    if row is None:
        return EventResult(False, event_id, costed)
    _bump_rollup(session, v)
    return EventResult(True, event_id, costed)


def record_model_call(
    session: Session,
    values: Mapping[str, Any],  # Any: column values
    *,
    reported: estimate.ReportedUsage | None,
    estimated_input: int,
    estimated_output: int,
    provider_reported_cost: Decimal | None = None,
    provider_reported_currency: str | None = None,
    at: datetime | None = None,
) -> EventResult:
    """Record one model call whose provider usage may be missing (Item 11). Counts the provider
    reported are stored as reported; each one it did not report is stored as the caller's
    estimate (never 0) and the event is priced as `estimated`, not `calculated`. What the provider
    said is kept in `provider_reported_usage`. `values` carries the event columns except the token
    counts, cost and `provider_reported_usage`."""
    resolved = estimate.resolve_usage(
        reported, estimated_input=estimated_input, estimated_output=estimated_output
    )
    v: dict[str, Any] = dict(values)  # Any: column values
    usage = pricing.Usage(
        input_tokens=resolved.input_tokens,
        output_tokens=resolved.output_tokens,
        calls=1 if v.get("status", "succeeded") == "succeeded" else 0,
        duration_ms=v.get("duration_ms"),
    )
    costed, provider_key, model_key = price_model_usage(
        session,
        v["tenant_id"],
        v.get("model_id"),
        usage,
        at=at,
        provider_reported_cost=provider_reported_cost,
        provider_reported_currency=provider_reported_currency,
        estimated=resolved.estimated,
    )
    v["input_tokens"] = resolved.input_tokens
    v["output_tokens"] = resolved.output_tokens
    v["costed"] = costed
    v["provider_reported_usage"] = resolved.as_json()
    if provider_key is not None:
        v.setdefault("provider_key", provider_key)
        v.setdefault("model_key", model_key)
    return record(session, v)


def record_model_call_durably(
    db: Database,
    tenant_id: UUID,
    values: Mapping[str, Any],  # Any: column values
    *,
    reported: estimate.ReportedUsage | None,
    estimated_input: int,
    estimated_output: int,
    provider_reported_cost: Decimal | None = None,
    provider_reported_currency: str | None = None,
) -> EventResult:
    """`record_model_call` in its own short transaction, committed before returning: call it the
    moment the provider has answered, so the usage row exists even when the caller's transaction
    never commits (a crash, a lost worker). The caller's later `record` of the same `event_key`
    inserts nothing and changes no rollup, so the call is counted once.

    Call it while the caller's own transaction holds no uncommitted ledger writes (the rollup row
    of the same dimensions would be locked by it): a lock wait beyond five seconds fails instead
    of hanging."""
    with db.session() as s:
        set_tenant(s, tenant_id)
        s.execute(text("SET LOCAL lock_timeout = '5s'"))
        result = record_model_call(
            s,
            values,
            reported=reported,
            estimated_input=estimated_input,
            estimated_output=estimated_output,
            provider_reported_cost=provider_reported_cost,
            provider_reported_currency=provider_reported_currency,
        )
        s.commit()
        return result


def _bump_rollup(session: Session, v: Mapping[str, Any]) -> None:  # Any: column values
    cached = int(v.get("cached_read_tokens") or 0) + int(v.get("cached_write_tokens") or 0)
    amount = v.get("cost_amount")
    session.execute(
        text(
            "INSERT INTO shifde.usage_rollups_daily (id, tenant_id, day, dim_key, workspace_id, "
            "project_id, principal_id, agent_id, prompt_id, model_id, pipeline_id, "
            "orchestration_id, build_ref, rate_card_version_id, event_type, cost_kind, currency, "
            "input_tokens, output_tokens, cached_tokens, calls, duration_ms, cost_amount, "
            "event_count) "
            "VALUES (:id, :tenant_id, :day, :dim_key, :workspace_id, :project_id, :principal_id, "
            ":agent_id, :prompt_id, :model_id, :pipeline_id, :orchestration_id, :build_ref, "
            ":rate_card_version_id, :event_type, :cost_kind, :currency, :input_tokens, "
            ":output_tokens, :cached, :calls, "
            ":duration_ms, :cost_amount, 1) "
            "ON CONFLICT (tenant_id, day, dim_key) DO UPDATE SET "
            "input_tokens = usage_rollups_daily.input_tokens + EXCLUDED.input_tokens, "
            "output_tokens = usage_rollups_daily.output_tokens + EXCLUDED.output_tokens, "
            "cached_tokens = usage_rollups_daily.cached_tokens + EXCLUDED.cached_tokens, "
            "calls = usage_rollups_daily.calls + EXCLUDED.calls, "
            "duration_ms = usage_rollups_daily.duration_ms + EXCLUDED.duration_ms, "
            "cost_amount = usage_rollups_daily.cost_amount + EXCLUDED.cost_amount, "
            "event_count = usage_rollups_daily.event_count + 1, updated_at = now()"
        ),
        {
            "id": uuid7(),
            "tenant_id": v["tenant_id"],
            "day": utc_day(v["occurred_at"]),
            "dim_key": dim_key(v),
            **{d: v.get(d) for d in DIMS},
            "event_type": v["event_type"],
            "cost_kind": v["cost_kind"],
            "currency": v.get("currency"),
            "input_tokens": int(v.get("input_tokens") or 0),
            "output_tokens": int(v.get("output_tokens") or 0),
            "cached": cached,
            "calls": int(v.get("call_count") or 0),
            "duration_ms": int(v.get("duration_ms") or 0),
            "cost_amount": amount if amount is not None else Decimal(0),
        },
    )


# ---------------------------------------------------------------------------- corrections
def correct(
    session: Session, tenant_id: UUID, event_id: UUID, *, reason: str
) -> EventResult | None:
    """Append the compensating row that cancels `event_id` (negative deltas). Idempotent: a second
    correction of the same event inserts nothing."""
    row = (
        session.execute(
            text(
                "SELECT * FROM shifde.usage_events WHERE id = :id AND tenant_id = :t"
                " AND corrects_event_id IS NULL"
            ),
            {"id": event_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    v = dict(row)
    for k in ("input_tokens", "output_tokens", "cached_read_tokens", "cached_write_tokens"):
        v[k] = -int(v[k] or 0)
    v["call_count"] = -int(v["call_count"] or 0)
    v["duration_ms"] = -int(v["duration_ms"] or 0)
    costed = pricing.UNMEASURED
    if v["cost_amount"] is not None:
        costed = pricing.Costed(
            v["cost_kind"],
            -Decimal(v["cost_amount"]),
            v["currency"],
            v["rate_card_version_id"],
            v["rate_snapshot"],
        )
    v["costed"] = costed
    v["event_key"] = f"correction:{event_id}"
    v["corrects_event_id"] = event_id
    v["provider_reported_usage"] = {"correction_reason": reason[:200]}
    v["occurred_at"] = utcnow()
    for drop in ("id", "cost_kind", "cost_amount", "currency", "rate_card_version_id"):
        v.pop(drop, None)
    v.pop("rate_snapshot", None)
    return record(session, v)


# ---------------------------------------------------------------------------- reconciliation
@dataclass
class Reconciliation:
    events_sum: int
    rollup_sum: int
    consistent: bool
    mismatched: int
    detail: list[dict[str, Any]] = field(default_factory=list)  # Any: diagnostic rows
    level: str = "full"  # `totals` (per day and cost kind) or `full` (per dimension row)


def _window(from_day: date, to_day: date) -> tuple[datetime, datetime]:
    start = datetime(from_day.year, from_day.month, from_day.day, tzinfo=UTC)
    end = datetime(to_day.year, to_day.month, to_day.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


_MEASURES = ("input_tokens", "output_tokens", "cached_tokens", "calls", "duration_ms", "events")


def _event_groups(
    session: Session, tenant_id: UUID, from_day: date, to_day: date, vis: Visibility = ALL
) -> dict[tuple[date, str], dict[str, Any]]:  # Any: measures
    start, end = _window(from_day, to_day)
    params: dict[str, Any] = {"t": tenant_id, "a": start, "b": end}  # Any: SQL parameters
    rows = (
        session.execute(
            text(
                "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, "
                + ", ".join(DIMS)
                + ", event_type, cost_kind, currency, "
                "sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, "
                "sum(cached_read_tokens + cached_write_tokens) AS cached_tokens, "
                "sum(call_count) AS calls, COALESCE(sum(duration_ms), 0) AS duration_ms, "
                "COALESCE(sum(cost_amount), 0) AS cost_amount, count(*) AS events "
                "FROM shifde.usage_events WHERE tenant_id = :t AND occurred_at >= :a "
                f"AND occurred_at < :b AND {vis.clause(params)} GROUP BY 1, "
                + ", ".join(DIMS)
                + ", event_type, cost_kind, currency"
            ),
            params,
        )
        .mappings()
        .all()
    )
    out: dict[tuple[date, str], dict[str, Any]] = {}
    for r in rows:
        out[(r["day"], dim_key(r))] = dict(r)
    return out


def _rollup_groups(
    session: Session, tenant_id: UUID, from_day: date, to_day: date, vis: Visibility = ALL
) -> dict[tuple[date, str], dict[str, Any]]:  # Any: measures
    params: dict[str, Any] = {"t": tenant_id, "a": from_day, "b": to_day}  # Any: SQL parameters
    rows = (
        session.execute(
            text(
                "SELECT day, dim_key, input_tokens, output_tokens, cached_tokens, calls, "
                "duration_ms, cost_amount, event_count AS events FROM shifde.usage_rollups_daily "
                f"WHERE tenant_id = :t AND day >= :a AND day <= :b AND {vis.clause(params)}"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return {(r["day"], r["dim_key"]): dict(r) for r in rows}


def _day_totals(
    session: Session, tenant_id: UUID, from_day: date, to_day: date, vis: Visibility
) -> tuple[dict[tuple[date, str], dict[str, Any]], dict[tuple[date, str], dict[str, Any]]]:  # Any
    """Control totals per (UTC day, cost kind) from the events and from the rollups. Cheap enough
    for every dashboard request (one pass over the window, a handful of groups)."""
    start, end = _window(from_day, to_day)
    p1: dict[str, Any] = {"t": tenant_id, "a": start, "b": end}  # Any: SQL parameters
    events = session.execute(
        text(
            "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, cost_kind AS dim_key, "
            "sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, "
            "sum(cached_read_tokens + cached_write_tokens) AS cached_tokens, "
            "sum(call_count) AS calls, COALESCE(sum(duration_ms), 0) AS duration_ms, "
            "COALESCE(sum(cost_amount), 0) AS cost_amount, count(*) AS events "
            "FROM shifde.usage_events WHERE tenant_id = :t AND occurred_at >= :a "
            f"AND occurred_at < :b AND {vis.clause(p1)} GROUP BY 1, 2"
        ),
        p1,
    ).mappings()
    p2: dict[str, Any] = {"t": tenant_id, "a": from_day, "b": to_day}  # Any: SQL parameters
    rolls = session.execute(
        text(
            "SELECT day, cost_kind AS dim_key, sum(input_tokens) AS input_tokens, "
            "sum(output_tokens) AS output_tokens, sum(cached_tokens) AS cached_tokens, "
            "sum(calls) AS calls, sum(duration_ms) AS duration_ms, sum(cost_amount) AS "
            "cost_amount, sum(event_count) AS events FROM shifde.usage_rollups_daily "
            "WHERE tenant_id = :t AND day >= :a AND day <= :b "
            f"AND {vis.clause(p2)} GROUP BY 1, 2"
        ),
        p2,
    ).mappings()
    return (
        {(r["day"], r["dim_key"]): dict(r) for r in events},
        {(r["day"], r["dim_key"]): dict(r) for r in rolls},
    )


def reconcile(
    session: Session,
    tenant_id: UUID,
    from_day: date,
    to_day: date,
    vis: Visibility = ALL,
    *,
    deep: bool = True,
) -> Reconciliation:
    """Compare the ledger with its rollups over whole UTC days. `deep=True` compares every
    (day, dimension row); `deep=False` compares control totals per (day, cost kind), which is what
    the dashboards use (the deep pass groups every event by eleven columns and is meant for
    audits, `rebuild` and tests)."""
    if deep:
        events = _event_groups(session, tenant_id, from_day, to_day, vis)
        rolls = _rollup_groups(session, tenant_id, from_day, to_day, vis)
    else:
        events, rolls = _day_totals(session, tenant_id, from_day, to_day, vis)
    detail: list[dict[str, Any]] = []  # Any: diagnostic rows
    for key in sorted(set(events) | set(rolls), key=lambda k: (k[0], k[1])):
        e, r = events.get(key), rolls.get(key)
        measures = (*_MEASURES, "cost_amount")
        left = {m: (Decimal(e[m]) if e else Decimal(0)) for m in measures}
        right = {m: (Decimal(r[m]) if r else Decimal(0)) for m in measures}
        if left != right:
            detail.append(
                {
                    "day": key[0].isoformat(),
                    "dim_key": key[1],
                    "events": {m: str(x) for m, x in left.items()},
                    "rollup": {m: str(x) for m, x in right.items()},
                }
            )
    ev_tokens = sum(int(e["input_tokens"] + e["output_tokens"]) for e in events.values())
    ru_tokens = sum(int(r["input_tokens"] + r["output_tokens"]) for r in rolls.values())
    return Reconciliation(
        ev_tokens, ru_tokens, not detail, len(detail), detail[:20], "full" if deep else "totals"
    )


def rebuild(session: Session, tenant_id: UUID, from_day: date, to_day: date) -> Reconciliation:
    """Recompute the rollup rows of the window from the events (set, not add) and zero the rows
    that no event backs any more. Returns the reconciliation AFTER the rebuild."""
    events = _event_groups(session, tenant_id, from_day, to_day)
    now = utcnow()
    for (day, key), e in events.items():
        session.execute(
            text(
                "INSERT INTO shifde.usage_rollups_daily (id, tenant_id, day, dim_key, "
                "workspace_id, project_id, principal_id, agent_id, prompt_id, model_id, "
                "pipeline_id, orchestration_id, build_ref, rate_card_version_id, event_type, "
                "cost_kind, currency, "
                "input_tokens, output_tokens, cached_tokens, calls, duration_ms, cost_amount, "
                "event_count, rebuilt_at) VALUES (:id, :tenant_id, :day, :dim_key, :workspace_id, "
                ":project_id, :principal_id, :agent_id, :prompt_id, :model_id, :pipeline_id, "
                ":orchestration_id, :build_ref, :rate_card_version_id, :event_type, "
                ":cost_kind, :currency, "
                ":input_tokens, :output_tokens, :cached_tokens, :calls, :duration_ms, "
                ":cost_amount, :events, :now) "
                "ON CONFLICT (tenant_id, day, dim_key) DO UPDATE SET "
                "input_tokens = EXCLUDED.input_tokens, output_tokens = EXCLUDED.output_tokens, "
                "cached_tokens = EXCLUDED.cached_tokens, calls = EXCLUDED.calls, "
                "duration_ms = EXCLUDED.duration_ms, cost_amount = EXCLUDED.cost_amount, "
                "event_count = EXCLUDED.event_count, rebuilt_at = :now, updated_at = :now"
            ),
            {"id": uuid7(), "tenant_id": tenant_id, "day": day, "dim_key": key, "now": now, **e},
        )
    stale = [k for k in _rollup_groups(session, tenant_id, from_day, to_day) if k not in events]
    for day, key in stale:
        session.execute(
            text(
                "UPDATE shifde.usage_rollups_daily SET input_tokens = 0, output_tokens = 0, "
                "cached_tokens = 0, calls = 0, duration_ms = 0, cost_amount = 0, event_count = 0, "
                "rebuilt_at = :now, updated_at = :now "
                "WHERE tenant_id = :t AND day = :d AND dim_key = :k"
            ),
            {"now": now, "t": tenant_id, "d": day, "k": key},
        )
    return reconcile(session, tenant_id, from_day, to_day)
