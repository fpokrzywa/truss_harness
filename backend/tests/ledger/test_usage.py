"""The usage ledger: idempotent events, write-through rollups, corrections, reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.ledger import pricing
from tests.ledger.util import add_model, add_rate, add_tenant, create_stand_ins


def _event(tenant: UUID, key: str, **kw: Any) -> dict[str, Any]:  # Any: column values
    return {
        "tenant_id": tenant,
        "event_key": key,
        "event_type": "model_call",
        "input_tokens": 100,
        "output_tokens": 50,
        **kw,
    }


def _today() -> date:
    return utcnow().date()


def test_a_replayed_event_inserts_nothing_and_changes_no_rollup(owner_session: Session) -> None:
    s, t = owner_session, uuid7()
    first = ledger.record(s, _event(t, "step-1"))
    again = ledger.record(s, _event(t, "step-1"))
    assert first.inserted and not again.inserted
    rolled = s.execute(
        text("SELECT event_count, input_tokens, calls FROM shifde.usage_rollups_daily")
    ).one()
    assert tuple(rolled) == (1, 100, 1)
    assert ledger.reconcile(s, t, _today(), _today()).consistent


def test_the_same_key_in_another_tenant_is_a_separate_event(owner_session: Session) -> None:
    s, a, b = owner_session, uuid7(), uuid7()
    assert ledger.record(s, _event(a, "k")).inserted
    assert ledger.record(s, _event(a, "k2")).inserted
    assert ledger.record(s, _event(b, "k")).inserted
    totals = {
        r[0]: r[1]
        for r in s.execute(
            text("SELECT tenant_id, sum(input_tokens) FROM shifde.usage_events GROUP BY 1")
        )
    }
    assert totals == {a: 200, b: 100}


def test_a_correction_is_a_compensating_row_that_nets_out_and_is_idempotent(
    owner_session: Session,
) -> None:
    s, t = owner_session, uuid7()
    ev = ledger.record(s, _event(t, "step-1"))
    fix = ledger.correct(s, t, ev.event_id, reason="duplicate charge")
    assert fix is not None and fix.inserted
    again = ledger.correct(s, t, ev.event_id, reason="again")
    assert again is not None and not again.inserted
    assert s.execute(text("SELECT count(*) FROM shifde.usage_events")).scalar_one() == 2
    net = s.execute(
        text("SELECT sum(input_tokens), sum(call_count) FROM shifde.usage_events")
    ).one()
    assert tuple(net) == (0, 0)
    assert ledger.reconcile(s, t, _today(), _today()).consistent
    assert ledger.correct(s, uuid7(), ev.event_id, reason="other tenant") is None


def test_a_tampered_rollup_is_detected_in_both_modes_and_rebuilt(owner_session: Session) -> None:
    s, t = owner_session, uuid7()
    ledger.record(s, _event(t, "k1"))
    ledger.record(s, _event(t, "k2"))
    s.execute(text("UPDATE shifde.usage_rollups_daily SET input_tokens = input_tokens + 7"))
    for deep in (True, False):
        rec = ledger.reconcile(s, t, _today(), _today(), deep=deep)
        assert not rec.consistent and rec.mismatched == 1
    fixed = ledger.rebuild(s, t, _today(), _today())
    assert fixed.consistent
    rolled = s.execute(text("SELECT input_tokens FROM shifde.usage_rollups_daily")).scalar_one()
    assert rolled == 200


def test_reconciliation_counts_only_the_tenants_own_events(owner_session: Session) -> None:
    s, a, b = owner_session, uuid7(), uuid7()
    ledger.record(s, _event(a, "k"))
    ledger.record(s, _event(b, "k", input_tokens=5000))
    rec = ledger.reconcile(s, a, _today(), _today())
    assert rec.consistent and rec.events_sum == 150
    assert ledger.reconcile(s, a, _today(), _today(), deep=False).events_sum == 150


def test_events_are_append_only(owner_session: Session) -> None:
    s, t = owner_session, uuid7()
    ledger.record(s, _event(t, "k"))
    for sql in (
        "UPDATE shifde.usage_events SET input_tokens = 0",
        "DELETE FROM shifde.usage_events",
    ):
        with pytest.raises(DBAPIError), s.begin_nested():
            s.execute(text(sql))


def test_an_unpriced_model_is_unmeasured_with_no_amount_never_zero(
    owner_session: Session,
) -> None:
    s = owner_session
    create_stand_ins(s)
    t = add_tenant(s)
    model = add_model(s, t)
    costed, provider_key, model_key = ledger.price_model_usage(
        s, t, model, pricing.Usage(input_tokens=10, output_tokens=10)
    )
    assert costed.kind == "unmeasured" and costed.amount is None
    assert (provider_key, model_key) == ("openai", "m1")
    ledger.record(s, _event(t, "k", costed=costed))
    row = s.execute(text("SELECT cost_kind, cost_amount, currency FROM shifde.usage_events")).one()
    assert tuple(row) == ("unmeasured", None, None)


def test_a_priced_call_keeps_the_rate_snapshot(owner_session: Session) -> None:
    s = owner_session
    create_stand_ins(s)
    t = add_tenant(s)
    model = add_model(s, t)
    version = add_rate(s, t, input_rate="2", output_rate="4")
    costed, pk, mk = ledger.price_model_usage(
        s, t, model, pricing.Usage(input_tokens=1_000_000, output_tokens=500_000)
    )
    assert costed.kind == "calculated" and costed.amount == Decimal(4)
    assert costed.rate_card_version_id == version
    ledger.record(s, _event(t, "k", model_id=model, provider_key=pk, model_key=mk, costed=costed))
    row = s.execute(
        text("SELECT cost_amount, rate_snapshot->>'input_rate' FROM shifde.usage_events")
    ).one()
    assert (row[0], row[1]) == (Decimal(4), "2.0000000000")
    assert ledger.reconcile(s, t, _today(), _today()).consistent


def test_a_provider_reported_cost_wins_over_the_calculated_one(owner_session: Session) -> None:
    s = owner_session
    create_stand_ins(s)
    t = add_tenant(s)
    model = add_model(s, t)
    add_rate(s, t)
    costed, _, _ = ledger.price_model_usage(
        s,
        t,
        model,
        pricing.Usage(input_tokens=10, output_tokens=10),
        provider_reported_cost=Decimal("0.5"),
        provider_reported_currency="EUR",
    )
    assert (costed.kind, costed.amount, costed.currency) == (
        "provider_reported",
        Decimal("0.5"),
        "EUR",
    )
    assert costed.snapshot is not None  # the calculated rate is kept for comparison


def test_a_cost_kind_and_an_amount_must_agree(owner_session: Session) -> None:
    s, t = owner_session, uuid7()
    bad = pricing.Costed("calculated", None, None)
    with pytest.raises(DBAPIError), s.begin_nested():
        ledger.record(s, _event(t, "k", costed=bad))


def test_the_visibility_clause_filters_by_workspace_and_project() -> None:
    vis = ledger.Visibility(all=False, workspace_ids=(uuid7(),))
    params: dict[str, Any] = {}
    assert "workspace_id = ANY" in vis.clause(params, "u.")
    assert set(params) == {"vis_ws", "vis_pj"}
    assert ledger.ALL.clause({}) == "TRUE"
