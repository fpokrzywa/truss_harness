"""`ledger.reservations` and the ledger_0003 schema (issue #82, design 12.1 and 12.2).

A hold carries a provider-request marker (`provider_request_at`); a marked hold never expires
after its TTL and is charged as `settled_unknown` with one unknown-usage row when its run's
worker is lost, an unmarked one expires or is released as before. A direct invocation's hold has
no run (`invocation_id` instead) and counts against the rules named in its `budget_ids`. Runs as
the schema owner on real runs (tests/ledger/util.py)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.ledger import budgeting, enforcement, reservations
from shifde.ledger.enforcement import BudgetDenied, BudgetScope, ReserveDetail
from tests.ledger.util import add_budget, add_model, add_run, add_tenant

INSERT = (
    "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, invocation_id, step_key, "
    "budget_ids, amount, status, expires_at) VALUES (:i, :t, :r, :inv, 'k', '{}', 1, :st, "
    "now() + interval '1 hour')"
)


@pytest.fixture
def tenant(owner_session: Session) -> UUID:
    return add_tenant(owner_session)


def _scope(tenant_id: UUID) -> BudgetScope:
    return BudgetScope(tenant_id, None, None, None, None)


def _hold(s: Session, hold: UUID) -> tuple[object, ...]:
    row = s.execute(
        text(
            "SELECT status, amount, settled_amount, settled_cost, provider_request_at IS NOT NULL "
            "FROM shifde.budget_reservations WHERE id = :i"
        ),
        {"i": hold},
    ).one()
    return tuple(row)


def _insert(s: Session, tenant: UUID, **values: object) -> None:
    s.execute(
        text(INSERT),
        {"i": uuid7(), "t": tenant, "r": None, "inv": None, "st": "held", **values},
    )


# ------------------------------------------------------------------ ledger_0003 constraints
@pytest.mark.parametrize("owner", ["both", "neither"])
def test_a_hold_names_exactly_one_of_a_run_and_an_invocation(
    owner_session: Session, tenant: UUID, owner: str
) -> None:
    run = add_run(owner_session, tenant)
    values = {"r": run, "inv": uuid7()} if owner == "both" else {}
    with pytest.raises(IntegrityError, match="ck_budget_reservations__owner"):
        _insert(owner_session, tenant, **values)
    owner_session.rollback()


def test_an_invocation_holds_once_per_client_and_settled_unknown_is_a_status(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    invocation, run = uuid7(), add_run(s, tenant)
    _insert(s, tenant, inv=invocation, st="settled_unknown")
    _insert(s, tenant, r=run)
    with (
        pytest.raises(IntegrityError, match="uq_budget_reservations__invocation"),
        s.begin_nested(),
    ):
        _insert(s, tenant, inv=invocation)
    with pytest.raises(IntegrityError, match="uq_budget_reservations__run_step"), s.begin_nested():
        _insert(s, tenant, r=run)
    _insert(s, tenant, inv=uuid7())  # another invocation of the same client is its own hold
    s.rollback()


def test_the_app_role_may_set_the_marker_and_still_has_no_delete(
    owner_session: Session,
) -> None:
    grants = owner_session.execute(
        text(
            "SELECT privilege_type FROM information_schema.column_privileges "
            "WHERE table_schema = 'shifde' AND table_name = 'budget_reservations' "
            "AND column_name = 'provider_request_at' AND grantee = 'shifde_app'"
        )
    ).all()
    assert "UPDATE" in {g[0] for g in grants}
    table = owner_session.execute(
        text(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'shifde' AND table_name = 'budget_reservations' "
            "AND grantee = 'shifde_app' ORDER BY privilege_type"
        )
    ).all()
    assert [g[0] for g in table] == ["INSERT", "SELECT"]  # still no DELETE


# ------------------------------------------------------------------ expiry and counting
def test_an_unmarked_hold_past_its_ttl_expires_and_stops_counting_a_marked_one_does_not(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    budget = add_budget(s, tenant, amount="1000")
    run = add_run(s, tenant)
    unmarked = reservations.reserve_step(s, tenant, run, "a:1", [budget], 300, Decimal(0), None)
    marked = reservations.reserve_step(s, tenant, run, "b:1", [budget], 200, Decimal(0), None)
    reservations.mark_provider_request(s, tenant, marked)
    s.execute(
        text("UPDATE shifde.budget_reservations SET expires_at = now() - interval '1 second'")
    )
    rule = {"id": budget, "scope_type": "tenant", "scope_id": tenant, "period": "daily"}
    since = budgeting.period_start("daily")
    # before the sweep the stale unmarked hold no longer counts; the marked one still does
    assert budgeting.used_and_held(s, tenant, None, rule, metric="tokens", since=since) == (0, 200)
    enforcement.evaluate(s, _scope(tenant), 1, ReserveDetail(est_output_tokens=1))
    assert _hold(s, unmarked)[0] == "expired"
    assert _hold(s, marked)[:1] == ("held",)
    assert budgeting.used_and_held(s, tenant, None, rule, metric="tokens", since=since) == (0, 200)
    s.rollback()


def test_a_direct_invocation_hold_counts_against_the_rules_it_names_only(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    named = add_budget(s, tenant, amount="1000")
    other = add_budget(s, tenant, amount="1000")
    per_run = add_budget(s, tenant, amount="1000", period="run")
    hold = reservations.reserve_invocation(s, tenant, [named, per_run], 400, Decimal(0), None)
    since = budgeting.period_start("daily")

    def held(budget: UUID, period: str = "daily") -> Decimal:
        rule = {"id": budget, "scope_type": "tenant", "scope_id": tenant, "period": period}
        return budgeting.used_and_held(
            s, tenant, uuid7(), rule, metric="tokens", since=None if period == "run" else since
        )[1]

    assert (held(named), held(other), held(per_run, "run")) == (400, 0, 0)
    row = s.execute(
        text(
            "SELECT run_id, invocation_id IS NOT NULL, step_key FROM shifde.budget_reservations "
            "WHERE id = :i"
        ),
        {"i": hold},
    ).one()
    assert tuple(row) == (None, True, reservations.INVOCATION_STEP_KEY)
    # outside the rule's window it no longer counts
    s.execute(
        text("UPDATE shifde.budget_reservations SET created_at = :c WHERE id = :i"),
        {"c": utcnow() - timedelta(days=2), "i": hold},
    )
    assert held(named) == 0
    s.rollback()


def test_a_token_quota_counts_a_direct_invocation_hold_that_names_it(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    quota = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, limit_value)"
            " VALUES (:i, :t, 'tenant', :t, 'tokens_per_day', 100)"
        ),
        {"i": quota, "t": tenant},
    )
    ev = enforcement.evaluate(s, _scope(tenant), 10, ReserveDetail(est_output_tokens=10))
    assert ev.quota_ids == [quota] and ev.applicable == []
    hold = reservations.reserve_invocation(s, tenant, ev.quota_ids, 95, Decimal(0), None)
    reservations.mark_provider_request(s, tenant, hold)
    with pytest.raises(BudgetDenied, match="quota would be exceeded"):
        enforcement.evaluate(s, _scope(tenant), 10, ReserveDetail(est_output_tokens=10))
    s.rollback()


# ------------------------------------------------------------------ settle orphaned holds
def test_settling_orphans_charges_a_marked_hold_once_and_releases_an_unmarked_one(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    budget = add_budget(s, tenant, amount="10000")
    model = add_model(s, tenant)
    run = add_run(s, tenant)
    s.execute(
        text(
            "INSERT INTO shifde.run_steps (id, tenant_id, run_id, seq, attempt, kind, name, "
            "model_id) VALUES (:i, :t, :r, 1, 2, 'model_call', 'model:0', :m)"
        ),
        {"i": uuid7(), "t": tenant, "r": run, "m": model},
    )
    marked = reservations.reserve_step(
        s, tenant, run, "model:0:2", [budget], 700, Decimal("0.5"), "USD"
    )
    reservations.mark_provider_request(s, tenant, marked)
    unmarked = reservations.reserve_step(
        s, tenant, run, "model:1:1", [budget], 50, Decimal(0), None
    )
    assert reservations.settle_orphaned(s, tenant, run) == 2
    assert reservations.settle_orphaned(s, tenant, run) == 0
    assert _hold(s, marked) == ("settled_unknown", 700, 700, Decimal("0.5"), True)
    assert _hold(s, unmarked)[0] == "released"
    rows = s.execute(
        text(
            "SELECT event_key, status, input_tokens, output_tokens, cost_kind, cost_amount, "
            "currency, model_id, run_id, root_run_id, provider_reported_usage "
            "FROM shifde.usage_events WHERE tenant_id = :t"
        ),
        {"t": tenant},
    ).all()
    assert [tuple(r) for r in rows] == [
        (
            f"reservation_unknown:{marked}",
            "failed",
            700,
            0,
            "estimated",
            Decimal("0.5"),
            "USD",
            model,
            run,
            run,
            {"estimated": True, "usage_unknown": True},
        )
    ]
    # spent once, as used, never also as held
    rule = {"id": budget, "scope_type": "tenant", "scope_id": tenant, "period": "daily"}
    since = budgeting.period_start("daily")
    assert budgeting.used_and_held(s, tenant, None, rule, metric="tokens", since=since) == (700, 0)
    # a late settle of the real answer changes nothing (settle matches `held` only)
    reservations.settle(s, tenant, marked, 3, Decimal(0))
    assert _hold(s, marked)[0] == "settled_unknown"
    s.rollback()


def test_an_orphan_without_a_currency_or_a_step_is_unmeasured_without_a_model(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    run = add_run(s, tenant)
    hold = reservations.reserve_step(s, tenant, run, "gone:1", [], 9, Decimal(0), None)
    reservations.mark_provider_request(s, tenant, hold)
    reservations.settle_orphaned(s, tenant, run)
    row = s.execute(
        text(
            "SELECT cost_kind, cost_amount, model_id, step_id, input_tokens FROM "
            "shifde.usage_events WHERE event_key = :k"
        ),
        {"k": f"reservation_unknown:{hold}"},
    ).one()
    assert tuple(row) == ("unmeasured", None, None, None, 9)
    s.rollback()


def test_the_ledger_package_reexports_the_reservations_module() -> None:
    assert ledger.reservations is reservations
