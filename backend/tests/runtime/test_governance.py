"""`runtime.governance` (RT-1): the reservation holds the ledger's `hold_tokens` (findings Item 9,
B-602) and every statement names the reservation's client (B-2105)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from shifde.core.db import Database
from shifde.runtime.governance import BudgetScope, PolicyBudgetHooks, Reservation, ReserveDetail
from tests.ledger.util import add_budget
from tests.runtime.conftest import Client, Two


def _scope(c: Client) -> BudgetScope:
    return BudgetScope(c.tenant, c.workspace, c.project, None, c.run)


def test_the_hold_covers_the_calls_input_and_output_ceiling(owner_db: Database, two: Two) -> None:
    """Before: a caller estimating 10 tokens for a call that may use 100 + 400 held 10."""
    a = two.a
    with owner_db.session() as s:
        add_budget(s, a.tenant, amount="1000")
        detail = ReserveDetail(est_input_tokens=100, est_output_tokens=400)
        res = PolicyBudgetHooks().reserve(s, _scope(a), a.run, "step-1", 10, detail)
        stored = s.execute(
            text("SELECT amount FROM shifde.budget_reservations WHERE id = :i"), {"i": res.id}
        ).scalar_one()
        s.rollback()
    assert res.amount == 500 and stored == 500 and res.tenant_id == a.tenant


def test_settle_and_release_touch_only_the_reservations_own_client(
    owner_db: Database, two: Two
) -> None:
    """As the schema owner (no row-level security) a reservation id under another client's id
    changes nothing; with its own client it settles."""
    a, b = two.a, two.b
    hooks = PolicyBudgetHooks()
    with owner_db.session() as s:
        add_budget(s, a.tenant, amount="1000")
        res = hooks.reserve(s, _scope(a), a.run, "step-1", 10, ReserveDetail(est_output_tokens=5))
        forged = Reservation(res.id, res.budget_ids, res.amount, tenant_id=b.tenant)
        assert hooks.settle(s, forged, 3, Decimal(0)) == []
        hooks.release(s, forged)
        status = text("SELECT status FROM shifde.budget_reservations WHERE id = :i")
        assert s.execute(status, {"i": res.id}).scalar_one() == "held"
        hooks.settle(s, res, 3, Decimal(0))
        assert s.execute(status, {"i": res.id}).scalar_one() == "settled"
        with pytest.raises(ValueError, match="client id"):
            hooks.release(s, Reservation(res.id))
        s.rollback()


def test_an_alert_fires_once_per_threshold_for_the_budgets_own_client(
    owner_db: Database, two: Two
) -> None:
    a = two.a
    hooks = PolicyBudgetHooks()
    with owner_db.session() as s:
        budget = add_budget(s, a.tenant, amount="100")
        s.execute(
            text("UPDATE shifde.budgets SET alert_thresholds = '{0.5}' WHERE id = :b"),
            {"b": budget},
        )
        s.execute(
            text(
                "INSERT INTO shifde.usage_events (id, tenant_id, event_key, event_type, "
                "input_tokens, root_run_id) VALUES (gen_random_uuid(), :t, 'e', 'tool_call', 60, "
                ":r)"
            ),
            {"t": a.tenant, "r": a.run},
        )
        res = hooks.reserve(s, _scope(a), a.run, "s1", 1, ReserveDetail(est_output_tokens=1))
        first = hooks.settle(s, res, 1, Decimal(0))
        res2 = hooks.reserve(s, _scope(a), a.run, "s2", 1, ReserveDetail(est_output_tokens=1))
        second = hooks.settle(s, res2, 1, Decimal(0))
        rows = (
            s.execute(
                text("SELECT tenant_id FROM shifde.budget_alerts WHERE budget_id = :b"),
                {"b": budget},
            )
            .scalars()
            .all()
        )
        s.rollback()
    assert [f.threshold for f in first] == [Decimal("0.5")] and second == []
    assert rows == [a.tenant]
