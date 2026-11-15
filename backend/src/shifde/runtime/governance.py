"""Governance hooks (SYSTEM_ARCHITECTURE 9.2): the interface the Execution Gate uses for budgets.

P5 shipped token / run-count budgets behind `GovernanceHooks`; P8 completes them behind the SAME
interface (no parallel mechanism). A budget or quota applies to a scope (tenant | workspace |
project | agent | pipeline | principal) and a period (`daily` / `monthly` = UTC calendar windows,
`total` = all time, `run` = one root run and its children; the P5 spellings `day` / `month` keep
working). Its metric is `tokens`, `cost` or (P5) `runs`.

Enforcement happens BEFORE work:
* `preflight` refuses a run that must not start (exhausted budget, run / concurrency quota);
* `reserve` holds an estimate for one model call across EVERY applicable budget in one transaction
  (advisory lock per budget, in id order, so two calls racing for the last of a budget serialize
  and exactly the first one fits) and is released or settled afterwards.

Reconciliation after the fact: usage comes from the append-only ledger (`usage_events`, written in
the same transaction as `settle`), so "settled" is exact by construction; `settle` stores the
actual tokens / cost on the reservation and fires each alert threshold once per period.

`hard` budgets deny; `soft` budgets only alert. `on_exceed` other than `deny`
(`queue_until_reset`, `require_approval`) is stored but enforced as `deny` (fail closed): honest
status in the API (`on_exceed_effective`). A model call whose price is unknown cannot be compared
with a COST budget: tenant setting `unknown_cost_policy` decides (`allow_unmeasured`, the default,
lets it run and records it as `unmeasured`; `deny` refuses it).

Port changes (DECISIONS B-2105): every statement names the client next to row-level security (the
reservation carries its client id; budget, reservation and run reads and writes filter on it and
joins match on it); the alert insert conflicts on `(tenant_id, budget_id, period_start, threshold)`
with the budget's own client (B-607); the reservation holds `Evaluation.hold_tokens`, never less
than the call's input + output ceiling (findings Item 9, B-602).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol  # Any: SQL rows
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.ledger import budgeting
from shifde.ledger.budgeting import RUN_ENTITY, effective_limit, period_name, period_start
from shifde.ledger.enforcement import (
    BudgetDenied,
    BudgetScope,
    ReserveDetail,
    budgets_in_scope,
    evaluate,
    lock,
    quotas_in_scope,
    scope_match,
    scope_params,
    used_and_held,
)

__all__ = [
    "BudgetDenied",
    "BudgetScope",
    "FiredAlert",
    "GovernanceHooks",
    "PolicyBudgetHooks",
    "Reservation",
    "ReserveDetail",
    "TokenBudgetHooks",
    "scope_match",
    "scope_params",
]

RESERVATION_TTL = timedelta(minutes=10)


@dataclass
class Reservation:
    id: UUID | None
    budget_ids: list[UUID] = field(default_factory=list)
    amount: int = 0
    cost: Decimal = Decimal(0)
    currency: str | None = None
    tenant_id: UUID | None = None  # the client of the held row; required when `id` is set


@dataclass(frozen=True)
class FiredAlert:
    budget_id: UUID
    scope_type: str
    scope_id: UUID
    metric: str
    threshold: Decimal
    utilization: Decimal


class GovernanceHooks(Protocol):
    def preflight(self, session: Session, scope: BudgetScope) -> None:
        """Raise `BudgetDenied` when a run must not start at all."""

    def reserve(
        self,
        session: Session,
        scope: BudgetScope,
        run_id: UUID,
        step_key: str,
        estimate: int,
        detail: ReserveDetail | None = None,
    ) -> Reservation:
        """Hold `estimate` tokens (and the estimated cost) for one model call (raise
        `BudgetDenied` when over a limit)."""

    def settle(
        self,
        session: Session,
        reservation: Reservation,
        actual_tokens: int,
        actual_cost: Decimal | None = None,
        currency: str | None = None,
    ) -> list[FiredAlert]:
        """Replace the hold by the provider-reported usage; returns the alerts newly fired."""

    def release(self, session: Session, reservation: Reservation) -> None:
        """Drop the hold (the call failed before consuming tokens)."""


class PolicyBudgetHooks:
    """Budgets (tokens / cost / runs) and quotas on the gate's hook interface."""

    # ------------------------------------------------------------------ pre-flight
    def preflight(self, session: Session, scope: BudgetScope) -> None:
        budgets = budgets_in_scope(session, scope)
        quotas = quotas_in_scope(session, scope)
        # serialize concurrent starters per rule until the run row is committed (same transaction)
        keys = [f"budget:{b['id']}" for b in budgets] + [f"quota:{q['id']}" for q in quotas]
        for key in sorted(keys):
            lock(session, key)
        for b in budgets:
            self._preflight_budget(session, scope, b)
        for q in quotas:
            self._preflight_quota(session, scope, q)

    def _preflight_budget(self, session: Session, scope: BudgetScope, b: dict[str, Any]) -> None:
        if b["enforcement"] == "soft":
            return
        metric = str(b["metric"])
        period = period_name(str(b["period"]))
        what = f"{b['scope_type']} {period}"
        since = period_start(str(b["period"]))
        if period != "run":
            # run-count limit: the metric itself, or the P5 `limit_runs` column of a mixed row
            runs_limit = effective_limit(b, "runs") if metric == "runs" else b["limit_runs"]
            if runs_limit is not None and self._count_runs(session, scope, b, since) >= runs_limit:
                raise BudgetDenied(f"the {what} run budget is exhausted", b["id"])
        limit = effective_limit(b, metric) if metric in ("tokens", "cost") else None
        if limit is None or period == "run":  # a per-run budget is enforced per model call
            return
        used, held = used_and_held(session, b, scope, metric=metric, since=since)
        if used + held >= limit:
            raise BudgetDenied(f"the {what} {metric} budget is exhausted", b["id"])

    @staticmethod
    def _count_runs(
        session: Session, scope: BudgetScope, rule: Mapping[str, Any], since: datetime | None
    ) -> int:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM shifde.runs r WHERE r.tenant_id = :t "
                    "AND r.parent_run_id IS NULL AND (CAST(:since AS timestamptz) IS NULL "
                    "OR r.created_at >= :since) "
                    "AND COALESCE(r.error_class, '') <> 'budget_denied' "
                    f"AND {RUN_ENTITY[str(rule['scope_type'])]}"
                ),
                {"t": scope.tenant_id, "since": since, "scope_id": rule["scope_id"]},
            ).scalar_one()
        )

    def _preflight_quota(self, session: Session, scope: BudgetScope, q: dict[str, Any]) -> None:
        limit = int(q["limit_value"])
        what = f"the {q['scope_type']} {q['metric']} quota"
        if q["metric"] == "runs_per_day":
            if self._count_runs(session, scope, q, period_start("daily")) >= limit:
                raise BudgetDenied(f"{what} is exhausted", q["id"])
        elif q["metric"] == "concurrent_runs":
            n = int(
                session.execute(
                    text(
                        "SELECT count(*) FROM shifde.runs r WHERE r.tenant_id = :t "
                        "AND r.parent_run_id IS NULL AND r.status IN "
                        "('pending', 'running', 'waiting_for_approval') "
                        f"AND {RUN_ENTITY[str(q['scope_type'])]}"
                    ),
                    {"t": scope.tenant_id, "scope_id": q["scope_id"]},
                ).scalar_one()
            )
            if n >= limit:
                raise BudgetDenied(f"{what} is exhausted", q["id"])
        elif q["metric"] == "tokens_per_day":
            rule = {"scope_type": q["scope_type"], "scope_id": q["scope_id"], "period": "daily"}
            since = period_start("daily")
            used, held = used_and_held(session, rule, scope, metric="tokens", since=since)
            if used + held >= limit:
                raise BudgetDenied(f"{what} is exhausted", q["id"])

    # ------------------------------------------------------------------ reserve
    def reserve(
        self,
        session: Session,
        scope: BudgetScope,
        run_id: UUID,
        step_key: str,
        estimate: int,
        detail: ReserveDetail | None = None,
    ) -> Reservation:
        ev = evaluate(session, scope, estimate, detail)
        applicable, est_cost, est_currency = ev.applicable, ev.est_cost, ev.est_currency
        quota_ids = [True] if ev.has_quotas else []
        # quotas are checked against the same holds; the reservation row carries budget ids only
        if not applicable and not quota_ids:
            return Reservation(None)
        cost_value = est_cost if est_cost is not None else Decimal(0)
        hold = ev.hold_tokens  # Item 9: never below the call's input + output ceiling
        res_id = session.execute(
            text(
                "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, "
                "budget_ids, amount, cost_amount, currency, expires_at) "
                "VALUES (:id, :t, :run, :k, :b, :a, :c, :cur, :exp) "
                "ON CONFLICT (tenant_id, run_id, step_key) DO UPDATE SET status = 'held', "
                "amount = :a, cost_amount = :c, currency = :cur, budget_ids = :b, "
                "expires_at = :exp RETURNING id"
            ),
            {
                "id": uuid7(),
                "t": scope.tenant_id,
                "run": run_id,
                "k": step_key,
                "b": applicable,
                "a": hold,
                "c": cost_value,
                "cur": est_currency,
                "exp": utcnow() + RESERVATION_TTL,
            },
        ).scalar_one()
        return Reservation(
            res_id, applicable, hold, cost_value, est_currency, tenant_id=scope.tenant_id
        )

    # ------------------------------------------------------------------ settle / release
    def settle(
        self,
        session: Session,
        reservation: Reservation,
        actual_tokens: int,
        actual_cost: Decimal | None = None,
        currency: str | None = None,
    ) -> list[FiredAlert]:
        if reservation.id is None:
            return []
        session.execute(
            text(
                "UPDATE shifde.budget_reservations SET status = 'settled', settled_amount = :a, "
                "settled_cost = :c WHERE id = :id AND tenant_id = :t AND status = 'held'"
            ),
            {"id": reservation.id, "t": _client(reservation), "a": actual_tokens, "c": actual_cost},
        )
        return self._alerts(session, reservation)

    def _alerts(self, session: Session, reservation: Reservation) -> list[FiredAlert]:
        if not reservation.budget_ids:
            return []
        tenant_id = _client(reservation)
        rows = (
            session.execute(
                text(
                    "SELECT b.id, b.tenant_id, b.scope_type, b.scope_id, b.period, b.metric, "
                    "b.limit_tokens, b.limit_runs, b.amount, b.currency, b.alert_thresholds "
                    "FROM shifde.budgets b WHERE b.id = ANY (:ids) AND b.tenant_id = :t "
                    "AND b.status = 'active'"
                ),
                {"ids": list(reservation.budget_ids), "t": tenant_id},
            )
            .mappings()
            .all()
        )
        fired: list[FiredAlert] = []
        now = utcnow()
        run = session.execute(
            text(
                "SELECT r.workspace_id, r.project_id, r.agent_id, rr.pipeline_id, "
                "r.initiated_by_principal_id, r.root_run_id FROM shifde.budget_reservations res "
                "JOIN shifde.runs r ON r.id = res.run_id AND r.tenant_id = res.tenant_id "
                "JOIN shifde.runs rr ON rr.id = r.root_run_id AND rr.tenant_id = r.tenant_id "
                "WHERE res.id = :id AND res.tenant_id = :t"
            ),
            {"id": reservation.id, "t": tenant_id},
        ).first()
        if run is None:
            return fired
        for row in rows:
            b = dict(row)
            metric = str(b["metric"])
            limit = effective_limit(b, metric)
            if limit is None or limit == 0 or metric not in ("tokens", "cost"):
                continue
            scope = BudgetScope(b["tenant_id"], run[0], run[1], run[2], run[5], run[3], run[4])
            since = period_start(str(b["period"]), now)
            used = budgeting.used_amount(
                session, scope.tenant_id, scope.root_run_id, b, metric=metric, since=since
            )
            utilization = (used / limit).quantize(Decimal("0.000001"))
            start = since or datetime(1970, 1, 1, tzinfo=now.tzinfo)
            for threshold in sorted(Decimal(t) for t in (b["alert_thresholds"] or [])):
                if utilization < threshold:
                    continue
                inserted = session.execute(
                    text(
                        "INSERT INTO shifde.budget_alerts (id, tenant_id, budget_id, "
                        "period_start, threshold, utilization) "
                        "VALUES (:id, :t, :b, :ps, :th, :u) "
                        "ON CONFLICT (tenant_id, budget_id, period_start, threshold) DO NOTHING "
                        "RETURNING id"
                    ),
                    {
                        "id": uuid7(),
                        "t": b["tenant_id"],
                        "b": b["id"],
                        "ps": start,
                        "th": threshold,
                        "u": utilization,
                    },
                ).first()
                if inserted is not None:
                    fired.append(
                        FiredAlert(
                            b["id"], b["scope_type"], b["scope_id"], metric, threshold, utilization
                        )
                    )
        return fired

    def release(self, session: Session, reservation: Reservation) -> None:
        if reservation.id is None:
            return
        session.execute(
            text(
                "UPDATE shifde.budget_reservations SET status = 'released' "
                "WHERE id = :id AND tenant_id = :t AND status = 'held'"
            ),
            {"id": reservation.id, "t": _client(reservation)},
        )


def _client(reservation: Reservation) -> UUID:
    """The client of a held reservation. `reserve` always sets it; a reservation without one
    cannot be settled or released (no statement runs on row-level security alone)."""
    if reservation.tenant_id is None:
        raise ValueError("a held reservation needs its client id")
    return reservation.tenant_id


TokenBudgetHooks = PolicyBudgetHooks  # P5 name (kept: the app, worker and P5 tests import it)
