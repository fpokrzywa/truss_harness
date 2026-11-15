"""Budget and quota evaluation (SETH 3ba0e06 p08): hard and soft rules, cost and token metrics,
held reservations, tenant scoping. Runs as the schema owner on real runs (tests/ledger/util.py);
the cross-client tests run the same queries as the application role."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.ledger import budgeting, enforcement
from shifde.ledger.enforcement import BudgetDenied, BudgetScope, ReserveDetail, evaluate
from tests.ledger.util import (
    add_budget,
    add_model,
    add_rate,
    add_run,
    add_tenant,
)


@pytest.fixture
def tenant(owner_session: Session) -> UUID:
    return add_tenant(owner_session)


def _scope(tenant_id: UUID, **kw: UUID | None) -> BudgetScope:
    return BudgetScope(
        tenant_id=tenant_id,
        workspace_id=kw.get("workspace_id"),
        project_id=kw.get("project_id"),
        agent_id=kw.get("agent_id"),
        root_run_id=kw.get("root_run_id"),
    )


def _spend(s: Session, tenant_id: UUID, tokens: int, key: str, **kw: object) -> None:
    ledger.record(
        s,
        {
            "tenant_id": tenant_id,
            "event_key": key,
            "event_type": "model_call",
            "input_tokens": tokens,
            "output_tokens": 0,
            **kw,
        },
    )


def test_a_hard_token_budget_denies_the_call_that_would_exceed_it(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    budget = add_budget(s, tenant, amount="1000")
    _spend(s, tenant, 900, "e1")
    ok = evaluate(s, _scope(tenant), 100, None)
    assert ok.applicable == [budget]
    with pytest.raises(BudgetDenied) as err:
        evaluate(s, _scope(tenant), 101, None)
    assert err.value.budget_id == budget


def test_a_soft_budget_never_denies(owner_session: Session, tenant: UUID) -> None:
    s = owner_session
    budget = add_budget(s, tenant, amount="10", enforcement="soft")
    _spend(s, tenant, 500, "e1")
    assert evaluate(s, _scope(tenant), 500, None).applicable == [budget]


def test_a_held_reservation_counts_until_it_expires(owner_session: Session, tenant: UUID) -> None:
    s = owner_session
    add_budget(s, tenant, amount="1000")
    run = add_run(s, tenant)

    def hold(amount: int, expires_in: timedelta, key: str) -> None:
        s.execute(
            text(
                "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, "
                "budget_ids, amount, status, expires_at) VALUES (:i, :t, :r, :k, '{}', :a, "
                "'held', now() + :e)"
            ),
            {"i": uuid7(), "t": tenant, "r": run, "k": key, "a": amount, "e": expires_in},
        )

    hold(800, timedelta(minutes=5), "live")
    with pytest.raises(BudgetDenied):
        evaluate(s, _scope(tenant, root_run_id=run), 300, None)
    evaluate(s, _scope(tenant, root_run_id=run), 200, None)
    hold(150, timedelta(seconds=-1), "stale")
    evaluate(s, _scope(tenant, root_run_id=run), 200, None)
    status = s.execute(
        text("SELECT status FROM shifde.budget_reservations WHERE step_key = 'stale'")
    ).scalar_one()
    assert status == "expired"


def test_a_cost_budget_compares_the_estimated_cost_of_the_call(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    model = add_model(s, tenant)
    add_rate(s, tenant, input_rate="1000", output_rate="2000")  # per 1M tokens
    add_budget(s, tenant, metric="cost", amount="0.50", currency="USD")
    fits = ReserveDetail(model_id=model, est_input_tokens=300, est_output_tokens=100)
    assert evaluate(s, _scope(tenant), 400, fits).est_cost == Decimal("0.5")
    too_big = ReserveDetail(model_id=model, est_input_tokens=300, est_output_tokens=101)
    with pytest.raises(BudgetDenied):
        evaluate(s, _scope(tenant), 401, too_big)


def test_an_unpriced_model_follows_the_tenants_unknown_cost_policy(
    owner_session: Session,
) -> None:
    s = owner_session
    allow = add_tenant(s)
    deny = add_tenant(s, settings='{"unknown_cost_policy": "deny"}')
    detail_for = {t: ReserveDetail(add_model(s, t), 10, 10) for t in (allow, deny)}
    for t in (allow, deny):
        add_budget(s, t, metric="cost", amount="1", currency="USD")
    evaluate(s, _scope(allow), 20, detail_for[allow])  # allowed, unmeasured
    with pytest.raises(BudgetDenied, match="unknown_cost_policy=deny"):
        evaluate(s, _scope(deny), 20, detail_for[deny])


def test_a_token_quota_is_a_hard_daily_rule(owner_session: Session, tenant: UUID) -> None:
    s = owner_session
    s.execute(
        text(
            "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, "
            "limit_value) VALUES (:i, :t, 'tenant', :t, 'tokens_per_day', 1000)"
        ),
        {"i": uuid7(), "t": tenant},
    )
    _spend(s, tenant, 950, "e1")
    assert evaluate(s, _scope(tenant), 50, None).has_quotas
    with pytest.raises(BudgetDenied, match="quota"):
        evaluate(s, _scope(tenant), 51, None)


def test_another_tenants_usage_budgets_and_holds_are_ignored(owner_session: Session) -> None:
    s = owner_session
    a, b = add_tenant(s), add_tenant(s)
    budget_b = add_budget(s, b, amount="10")  # B's budget does not apply to A
    _spend(s, b, 5000, "b-usage")
    budget_a = add_budget(s, a, amount="1000")
    _spend(s, a, 100, "a-usage")
    ev = evaluate(s, _scope(a), 900, None)
    assert ev.applicable == [budget_a]
    used, held = budgeting.used_and_held(
        s,
        a,
        None,
        {"scope_type": "tenant", "scope_id": a, "period": "daily", "currency": None},
        metric="tokens",
        since=budgeting.period_start("daily"),
    )
    assert (used, held) == (Decimal(100), Decimal(0))
    assert [x["id"] for x in enforcement.budgets_in_scope(s, _scope(a))] == [budget_a]
    assert [x["id"] for x in enforcement.budgets_in_scope(s, _scope(b))] == [budget_b]


def test_period_windows_and_effective_limits() -> None:
    now = utcnow()
    assert budgeting.period_start("day", now) == now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    monthly = budgeting.period_start("monthly", now)
    assert monthly is not None and monthly.day == 1
    assert budgeting.period_start("total") is None and budgeting.period_start("run") is None
    assert budgeting.period_name("month") == "monthly"
    row = {"amount": None, "limit_tokens": 7, "limit_runs": 3}
    assert budgeting.effective_limit(row, "tokens") == Decimal(7)
    assert budgeting.effective_limit(row, "runs") == Decimal(3)
    assert budgeting.effective_limit({**row, "amount": Decimal(9)}, "tokens") == Decimal(9)
