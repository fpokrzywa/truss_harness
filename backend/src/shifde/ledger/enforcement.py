"""Budget / quota evaluation shared by the run gate and direct model invocations; both then hold
the call's tokens through `ledger.reservations` while the rule locks are held.

Every applicable rule is locked (advisory transaction lock per rule, in id order) and compared in
the rule's metric; `used + held` is read in ONE statement (one snapshot) so a settle committing
concurrently is counted exactly once, never by neither read (see `budgeting.used_and_held`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any  # Any: SQL rows / parameters
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.ledger import budgeting, reservations
from shifde.ledger import estimate as call_estimate
from shifde.ledger.budgeting import effective_limit, period_name, period_start


class BudgetDenied(Exception):
    def __init__(self, message: str, budget_id: UUID | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.budget_id = budget_id


@dataclass(frozen=True)
class BudgetScope:
    tenant_id: UUID
    workspace_id: UUID | None
    project_id: UUID | None
    agent_id: UUID | None
    root_run_id: UUID | None
    pipeline_id: UUID | None = None
    principal_id: UUID | None = None


@dataclass(frozen=True)
class ReserveDetail:
    """What the caller knows about the call being checked (used to estimate its cost)."""

    model_id: UUID | None = None
    est_input_tokens: int = 0
    est_output_tokens: int = 0


@dataclass
class Evaluation:
    applicable: list[UUID]  # budget ids (not quotas) the call counts against
    has_quotas: bool
    est_cost: Decimal | None
    est_currency: str | None
    # Tokens the caller must hold for the call: never below the call's input + output ceiling
    # (Item 9: a smaller hold let a hard budget overshoot). Store this, not the caller's figure.
    hold_tokens: int = 0
    quota_ids: list[UUID] = field(default_factory=list)  # token quotas the call was checked against


def scope_match(alias: str) -> str:
    """SQL predicate: rows of `alias` (scope_type, scope_id) that cover the run's scope chain."""
    return (
        f"(({alias}.scope_type = 'tenant' AND {alias}.scope_id = :tenant) "
        f"OR ({alias}.scope_type = 'workspace' AND {alias}.scope_id = :ws) "
        f"OR ({alias}.scope_type = 'project' AND {alias}.scope_id = :proj) "
        f"OR ({alias}.scope_type = 'agent' AND {alias}.scope_id = :agent) "
        f"OR ({alias}.scope_type = 'pipeline' AND {alias}.scope_id = :pipeline) "
        f"OR ({alias}.scope_type = 'principal' AND {alias}.scope_id = :principal))"
    )


def scope_params(scope: BudgetScope) -> dict[str, Any]:  # Any: SQL parameters
    return {
        "tenant": scope.tenant_id,
        "ws": scope.workspace_id,
        "proj": scope.project_id,
        "agent": scope.agent_id,
        "pipeline": scope.pipeline_id,
        "principal": scope.principal_id,
    }


def lock(session: Session, key: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


def used_and_held(
    session: Session,
    b: Any,  # Any: budget / quota row
    scope: BudgetScope,
    *,
    metric: str,
    since: datetime | None,
) -> tuple[Decimal, Decimal]:
    return budgeting.used_and_held(
        session, scope.tenant_id, scope.root_run_id, b, metric=metric, since=since
    )


def budgets_in_scope(session: Session, scope: BudgetScope) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            # scope_match takes a constant alias; values are bound
            "SELECT b.id, b.tenant_id, b.scope_type, b.scope_id, b.period, b.metric, "  # nosec B608
            "b.limit_tokens, b.limit_runs, b.amount, b.currency, b.enforcement, b.on_exceed, "
            "b.alert_thresholds FROM shifde.budgets b "
            f"WHERE b.tenant_id = :tenant AND b.status = 'active' AND {scope_match('b')} "
            "ORDER BY b.id"
        ),
        scope_params(scope),
    ).mappings()
    out = []
    for r in rows:
        d = dict(r)
        if (
            d["metric"] == "tokens"
            and d["limit_tokens"] is None
            and d["amount"] is None
            and d["limit_runs"] is not None
        ):  # a P5 row with only a run limit is a run-count budget
            d["metric"] = "runs"
        out.append(d)
    return out


def quotas_in_scope(session: Session, scope: BudgetScope) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            # scope_match takes a constant alias; values are bound
            "SELECT q.id, q.scope_type, q.scope_id, q.metric, q.limit_value "  # nosec B608
            f"FROM shifde.quotas q WHERE q.tenant_id = :tenant AND q.status = 'active' "
            f"AND {scope_match('q')} "
            "ORDER BY q.id"
        ),
        scope_params(scope),
    ).mappings()
    return [dict(r) for r in rows]


def unknown_cost_policy(session: Session, tenant_id: UUID) -> str:
    row = session.execute(
        text("SELECT settings->>'unknown_cost_policy' FROM shifde.tenants WHERE id = :t"),
        {"t": tenant_id},
    ).first()
    return "deny" if row and row[0] == "deny" else "allow_unmeasured"


def estimate_cost(
    session: Session, scope: BudgetScope, detail: ReserveDetail | None
) -> tuple[Decimal | None, str | None]:
    if detail is None or detail.model_id is None:
        return None, None
    cost = call_estimate.call_cost(
        session, scope.tenant_id, detail.model_id, detail.est_input_tokens, detail.est_output_tokens
    )
    return (None, None) if cost is None else (cost.amount, cost.currency)


def evaluate(
    session: Session, scope: BudgetScope, estimate: int, detail: ReserveDetail | None
) -> Evaluation:
    """Lock and compare every token / cost budget and token quota covering `scope` with the
    estimated call; raise `BudgetDenied` when a hard rule would be exceeded."""
    # holds that outlived their TTL and whose provider call never started stop counting (see
    # held_amount) and are marked expired; a hold whose call may have started keeps counting
    reservations.expire_unstarted(session, scope.tenant_id)
    rules: list[dict[str, Any]] = [
        b
        for b in budgets_in_scope(session, scope)
        if b["metric"] in ("tokens", "cost") and effective_limit(b, str(b["metric"])) is not None
    ]
    quota_ids: set[UUID] = set()
    for q in quotas_in_scope(session, scope):
        if q["metric"] == "tokens_per_day":
            quota_ids.add(q["id"])
            rules.append(
                {
                    "id": q["id"],
                    "scope_type": q["scope_type"],
                    "scope_id": q["scope_id"],
                    "metric": "tokens",
                    "period": "daily",
                    "amount": q["limit_value"],
                    "limit_tokens": None,
                    "limit_runs": None,
                    "currency": None,
                    "enforcement": "hard",
                }
            )
    rules.sort(key=lambda r: str(r["id"]))
    est_cost, est_currency = estimate_cost(session, scope, detail)
    ceiling = 0 if detail is None else detail.est_input_tokens + detail.est_output_tokens
    hold = max(estimate, ceiling)
    applicable: list[UUID] = []
    for b in rules:
        is_quota = b["id"] in quota_ids
        lock(session, f"{'quota' if is_quota else 'budget'}:{b['id']}")
        metric = str(b["metric"])
        limit = effective_limit(b, metric)
        assert limit is not None
        since = period_start(str(b["period"]))
        hard = b["enforcement"] != "soft"
        what = f"the {b['scope_type']} {period_name(str(b['period']))}"
        if metric == "cost":
            if est_cost is None or est_currency != b["currency"]:
                # no usable price for this budget's currency
                if hard and unknown_cost_policy(session, scope.tenant_id) == "deny":
                    raise BudgetDenied(
                        f"{what} cost budget cannot be checked: the model has no effective "
                        "rate (unknown_cost_policy=deny)",
                        b["id"],
                    )
                continue
            request = est_cost
        else:
            request = Decimal(hold)
        used, held = used_and_held(session, b, scope, metric=metric, since=since)
        if hard and used + held + request > limit:
            unit = "cost" if metric == "cost" else "token"
            raise BudgetDenied(
                f"{what} {unit} {'quota' if is_quota else 'budget'} would be exceeded "
                f"({used + held} of {limit} used or reserved, {request} requested)",
                b["id"],
            )
        if not is_quota:
            applicable.append(b["id"])
    return Evaluation(
        applicable, bool(quota_ids), est_cost, est_currency, hold, sorted(quota_ids, key=str)
    )
