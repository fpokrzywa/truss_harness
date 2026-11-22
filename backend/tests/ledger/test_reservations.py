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


def test_the_app_role_may_set_the_marker_and_delete_only_through_the_retention_guard(
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
    # DELETE since runtime_0006 (B-2783), refused by trg_budget_reservations_retention_delete
    # unless the retention delete set its cutoff (tests/cli/test_maintenance_delete.py)
    assert [g[0] for g in table] == ["DELETE", "INSERT", "SELECT"]


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


# ------------------------------------------------------------------ orphaned invocation holds
def _expire(s: Session, *holds: UUID) -> None:
    s.execute(
        text(
            "UPDATE shifde.budget_reservations SET expires_at = now() - interval '1 second' "
            "WHERE id = ANY (:h)"
        ),
        {"h": list(holds)},
    )


def _unknown_rows(s: Session, tenant: UUID) -> list[tuple[object, ...]]:
    rows = s.execute(
        text(
            "SELECT event_key, event_type, status, input_tokens, output_tokens, call_count, "
            "cost_kind, cost_amount, currency, workspace_id, project_id, agent_id, principal_id, "
            "model_id, run_id, provider_reported_usage FROM shifde.usage_events "
            "WHERE tenant_id = :t ORDER BY event_key"
        ),
        {"t": tenant},
    ).all()
    return [tuple(r) for r in rows]


def test_settling_one_unknown_invocation_uses_the_callers_attribution_once(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    budget = add_budget(s, tenant, amount="10000")
    model, ws, pj, principal = add_model(s, tenant), uuid7(), uuid7(), uuid7()
    hold = reservations.reserve_invocation(s, tenant, [budget], 120, Decimal(0), None)
    attribution = {"workspace_id": ws, "project_id": pj, "principal_id": principal,
                   "model_id": model, "is_test": True, "event_type": "prompt_test"}  # fmt: skip
    # not marked yet: nothing to settle
    assert reservations.settle_unknown_invocation(s, tenant, hold, attribution) is False
    reservations.mark_provider_request(s, tenant, hold)
    assert reservations.settle_unknown_invocation(s, tenant, uuid7(), attribution) is False
    assert reservations.settle_unknown_invocation(s, tenant, hold, attribution) is True
    assert reservations.settle_unknown_invocation(s, tenant, hold, attribution) is False
    assert _hold(s, hold) == ("settled_unknown", 120, 120, Decimal(0), True)
    unknown = {"estimated": True, "usage_unknown": True}
    assert _unknown_rows(s, tenant) == [
        (
            f"reservation_unknown:{hold}",
            "prompt_test",
            "failed",
            120,
            0,
            1,
            "unmeasured",
            None,
            None,
            ws,
            pj,
            None,
            principal,
            model,
            None,
            unknown,
        )  # fmt: skip
    ]
    rule = {"id": budget, "scope_type": "tenant", "scope_id": tenant, "period": "daily"}
    since = budgeting.period_start("daily")
    assert budgeting.used_and_held(s, tenant, None, rule, metric="tokens", since=since) == (120, 0)
    s.rollback()


def test_the_invocation_sweep_charges_a_marked_expired_hold_with_the_scope_of_its_rules(
    owner_session: Session, tenant: UUID
) -> None:
    s = owner_session
    ws, other_ws, pj, principal, agent = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    on_ws = add_budget(s, tenant, scope_type="workspace", scope_id=ws, amount="10000")
    on_other_ws = add_budget(s, tenant, scope_type="workspace", scope_id=other_ws, amount="10000")
    on_pj = add_budget(s, tenant, scope_type="project", scope_id=pj, amount="10000")
    on_tenant = add_budget(s, tenant, amount="10000")
    # named only by the unmarked hold (an agent budget: no run or usage row matches it)
    lone = add_budget(s, tenant, scope_type="agent", scope_id=agent, amount="10000")
    quota = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, limit_value)"
            " VALUES (:i, :t, 'principal', :p, 'tokens_per_day', 100000)"
        ),
        {"i": quota, "t": tenant, "p": principal},
    )
    named = [on_other_ws, on_ws, on_pj, on_tenant, quota]  # on_ws has the lower id: it wins
    marked = reservations.reserve_invocation(s, tenant, named, 300, Decimal("0.25"), "USD")
    unmarked = reservations.reserve_invocation(s, tenant, [lone], 50, Decimal(0), None)
    live = reservations.reserve_invocation(s, tenant, [on_tenant], 70, Decimal(0), None)
    run = add_run(s, tenant)
    run_hold = reservations.reserve_step(s, tenant, run, "m:1", [on_tenant], 40, Decimal(0), None)
    for h in (marked, live, run_hold):
        reservations.mark_provider_request(s, tenant, h)
    since = budgeting.period_start("daily")

    def counted(budget: UUID, scope_type: str = "tenant", scope_id: UUID = tenant) -> object:
        rule = {"id": budget, "scope_type": scope_type, "scope_id": scope_id, "period": "daily"}
        return budgeting.used_and_held(s, tenant, None, rule, metric="tokens", since=since)

    assert counted(lone, "agent", agent) == (0, 50)  # within its TTL the unmarked hold counts
    _expire(s, marked, unmarked, run_hold)
    assert reservations.settle_orphaned_invocations(s, tenant) == 1
    assert reservations.settle_orphaned_invocations(s, tenant) == 0
    assert _hold(s, marked) == ("settled_unknown", 300, 300, Decimal("0.25"), True)
    for h in (unmarked, live, run_hold):
        assert _hold(s, h)[0] == "held"
    unknown = {"estimated": True, "usage_unknown": True}
    assert _unknown_rows(s, tenant) == [
        (
            f"reservation_unknown:{marked}",
            "model_call",
            "failed",
            300,
            0,
            1,
            "estimated",
            Decimal("0.25"),
            "USD",
            ws,
            pj,
            None,
            principal,
            None,
            None,
            unknown,
        )  # fmt: skip
    ]
    # the settled amount keeps counting, as used, against every rule the hold counted against
    assert counted(on_ws, "workspace", ws) == (300, 0)
    assert counted(on_pj, "project", pj) == (300, 0)
    assert counted(quota, "principal", principal) == (300, 0)
    assert counted(on_tenant) == (300, 110)  # plus the live direct hold and the marked run hold
    # the unmarked hold is left to the expiry sweep of a budget check and no longer counts
    assert counted(lone, "agent", agent) == (0, 0)
    enforcement.evaluate(s, _scope(tenant), 1, ReserveDetail(est_output_tokens=1))
    assert _hold(s, unmarked)[0] == "expired"
    assert counted(lone, "agent", agent) == (0, 0)
    assert len(_unknown_rows(s, tenant)) == 1  # the unmarked hold wrote no usage row
    s.rollback()


@pytest.mark.parametrize("rule", [{}, {"id": None}], ids=["missing", "none"])
@pytest.mark.parametrize("period", ["daily", "run"])
def test_counting_holds_requires_the_rules_id(
    owner_session: Session, tenant: UUID, rule: dict[str, object], period: str
) -> None:
    b = {**rule, "scope_type": "tenant", "scope_id": tenant, "period": period}
    since = budgeting.period_start(period)
    with pytest.raises(ValueError, match="needs its id"):
        budgeting.used_and_held(owner_session, tenant, None, b, metric="tokens", since=since)
    with pytest.raises(ValueError, match="needs its id"):
        budgeting.held_amount(owner_session, tenant, None, b, metric="tokens", since=since)
    owner_session.rollback()


def test_the_ledger_package_reexports_the_reservations_module() -> None:
    assert ledger.reservations is reservations
