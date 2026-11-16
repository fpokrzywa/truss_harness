"""R3 cost findings, ledger part: Item 9 (reservation below actual usage), Item 10 (the cost
estimate `limits.max_cost` needs), Item 11 (unreported usage is estimated and labelled, never 0;
a usage row survives a crash after the provider answered). Runtime wiring: carry_forward.md."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.adapters.base import Usage
from shifde.core.clock import utcnow
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.ledger import estimate
from shifde.ledger.enforcement import BudgetDenied, BudgetScope, ReserveDetail, evaluate
from tests._support.tenants import add_tenants
from tests.ledger.util import add_budget, add_model, add_rate, add_tenant


def _today() -> date:
    return utcnow().date()


def _scope(tenant_id: UUID) -> BudgetScope:
    return BudgetScope(tenant_id, None, None, None, None)


@pytest.fixture
def world(owner_session: Session) -> tuple[Session, UUID, UUID]:
    s = owner_session
    t = add_tenant(s)
    return s, t, add_model(s, t)


def _call(
    tenant: UUID, key: str, model: UUID | None = None, **kw: Any
) -> dict[str, Any]:  # Any: column values
    return {
        "tenant_id": tenant,
        "event_key": key,
        "event_type": "model_call",
        "model_id": model,
        "duration_ms": 12,
        **kw,
    }


# ----------------------------------------------------------------------- Item 9: the hold
def test_a_hold_below_the_calls_token_ceiling_cannot_overshoot_a_hard_budget(
    owner_session: Session,
) -> None:
    """Before: a caller holding 10 tokens for a call that may produce up to 950 passed a 500-token
    budget. The ledger now holds at least input + output ceiling."""
    s = owner_session
    t = add_tenant(s)
    add_budget(s, t, amount="500")
    detail = ReserveDetail(model_id=None, est_input_tokens=100, est_output_tokens=850)
    with pytest.raises(BudgetDenied):
        evaluate(s, _scope(t), 10, detail)


def test_the_evaluation_reports_the_hold_the_caller_must_store(owner_session: Session) -> None:
    s = owner_session
    t = add_tenant(s)
    add_budget(s, t, amount="5000")
    detail = ReserveDetail(model_id=None, est_input_tokens=100, est_output_tokens=850)
    assert evaluate(s, _scope(t), 10, detail).hold_tokens == 950
    assert evaluate(s, _scope(t), 2000, detail).hold_tokens == 2000  # never lowered
    assert evaluate(s, _scope(t), 10, None).hold_tokens == 10


def test_a_hold_needs_an_output_ceiling() -> None:
    assert estimate.call_hold_tokens(100, 400) == 500
    for missing in (None, 0, -5):
        with pytest.raises(estimate.MissingOutputCeiling):
            estimate.call_hold_tokens(100, missing)


def test_the_input_estimate_is_not_below_what_a_tokenizer_produces() -> None:
    english = "word " * 400  # about 400 tokens
    cjk = "漢字" * 300  # about one token per character
    emoji = "😀" * 100  # up to four byte-level tokens each
    code = '{"a":[1,2,3],"b":null}' * 50  # about one token per three characters
    for text_, floor in ((english, 400), (cjk, 600), (emoji, 100), (code, len(code) // 3)):
        assert estimate.estimate_text_tokens(text_) >= floor, text_[:8]
    messages = [{"role": "user", "content": english}, {"role": "system", "content": ""}]
    assert estimate.estimate_input_tokens(messages) >= 400 + 2 * estimate.MESSAGE_OVERHEAD_TOKENS
    assert estimate.estimate_text_tokens("") == 0


# ----------------------------------------------------------------------- Item 10: cost estimate
def test_the_worst_case_cost_of_a_call_is_priced_from_the_rate_in_force(
    world: tuple[Session, UUID, UUID],
) -> None:
    s, t, model = world
    assert estimate.call_cost(s, t, model, 1000, 500) is None  # no rate: unknown, not zero
    add_rate(s, t, input_rate="2", output_rate="4")
    cost = estimate.call_cost(s, t, model, 1_000_000, 500_000)
    assert cost == estimate.CostEstimate(Decimal(4), "USD")
    assert estimate.call_cost(s, t, None, 10, 10) is None
    # the budget check uses the same figure
    ev = evaluate(s, _scope(t), 10, ReserveDetail(model, 1_000_000, 500_000))
    assert (ev.est_cost, ev.est_currency) == (Decimal(4), "USD")


# ----------------------------------------------------------------------- Item 11: honesty
def test_unreported_usage_is_estimated_never_zero_and_a_real_zero_stays_zero() -> None:
    none = estimate.resolve_usage(Usage(), estimated_input=120, estimated_output=0)
    assert (none.input_tokens, none.output_tokens, none.estimated) == (120, 1, True)
    part = estimate.resolve_usage(Usage(input_tokens=90), estimated_input=120, estimated_output=40)
    assert (part.input_tokens, part.output_tokens, part.estimated) == (90, 40, True)
    assert part.estimated_fields == ("output_tokens",)
    real = estimate.resolve_usage(
        Usage(input_tokens=0, output_tokens=0), estimated_input=120, estimated_output=40
    )
    assert (real.input_tokens, real.output_tokens, real.estimated) == (0, 0, False)
    absent = estimate.resolve_usage(None, estimated_input=5, estimated_output=5)
    assert absent.estimated and absent.estimated_fields == ("input_tokens", "output_tokens")


def test_a_call_without_provider_usage_is_recorded_as_estimated_not_calculated(
    world: tuple[Session, UUID, UUID],
) -> None:
    s, t, model = world
    add_rate(s, t, input_rate="2", output_rate="4")
    res = ledger.record_model_call(
        s,
        _call(t, "k"),
        reported=Usage(),
        estimated_input=1_000_000,
        estimated_output=500_000,
    )
    assert res.inserted and res.costed.kind == "unmeasured"  # no model_id: nothing to price
    res = ledger.record_model_call(
        s,
        _call(t, "k2", model),
        reported=Usage(),
        estimated_input=1_000_000,
        estimated_output=500_000,
    )
    row = s.execute(
        text(
            "SELECT cost_kind, cost_amount, input_tokens, output_tokens, "
            "provider_reported_usage FROM shifde.usage_events WHERE event_key = 'k2'"
        )
    ).one()
    assert (row[0], row[1], row[2], row[3]) == ("estimated", Decimal(4), 1_000_000, 500_000)
    assert row[4]["basis"] == "estimated" and row[4]["input"] is None
    assert ledger.reconcile(s, t, _today(), _today()).consistent


@pytest.mark.parametrize("column", ["provider_key", "model_key"])
def test_the_provider_key_of_a_model_call_is_the_models_never_the_callers(
    world: tuple[Session, UUID, UUID], column: str
) -> None:
    """Runtime isolation review 2, finding 3: `shifde_ops_stats` groups every client's events by
    `provider_key` and `/api/metrics` exports it, so a caller cannot choose it."""
    s, t, model = world
    add_rate(s, t, input_rate="2", output_rate="4")
    with pytest.raises(ValueError, match="not by the caller"):
        ledger.record_model_call(
            s,
            _call(t, "forged", model, **{column: "chosen-by-client"}),
            reported=Usage(input_tokens=1, output_tokens=1),
            estimated_input=1,
            estimated_output=1,
        )
    ledger.record_model_call(
        s,
        _call(t, "own", model),
        reported=Usage(input_tokens=1, output_tokens=1),
        estimated_input=1,
        estimated_output=1,
    )
    keys = s.execute(
        text(
            "SELECT event_key, provider_key, model_key FROM shifde.usage_events WHERE tenant_id = :t"
        ),
        {"t": t},
    ).all()
    assert [tuple(k) for k in keys] == [("own", "openai", "m1")]


def test_reported_usage_is_calculated_and_labelled_reported(
    world: tuple[Session, UUID, UUID],
) -> None:
    s, t, model = world
    add_rate(s, t, input_rate="2", output_rate="4")
    ledger.record_model_call(
        s,
        _call(t, "k", model),
        reported=Usage(input_tokens=1_000_000, output_tokens=500_000),
        estimated_input=1,
        estimated_output=1,
    )
    row = s.execute(
        text(
            "SELECT cost_kind, cost_amount, input_tokens, provider_reported_usage "
            "FROM shifde.usage_events"
        )
    ).one()
    assert (row[0], row[1], row[2]) == ("calculated", Decimal(4), 1_000_000)
    assert row[3]["basis"] == "reported" and row[3]["input"] == 1_000_000


def test_estimated_tokens_without_a_rate_are_kept_and_the_cost_stays_unmeasured(
    world: tuple[Session, UUID, UUID],
) -> None:
    s, t, model = world
    ledger.record_model_call(
        s, _call(t, "k", model), reported=Usage(), estimated_input=70, estimated_output=30
    )
    row = s.execute(
        text(
            "SELECT cost_kind, cost_amount, input_tokens, output_tokens, "
            "provider_reported_usage->>'basis' FROM shifde.usage_events"
        )
    ).one()
    assert tuple(row) == ("unmeasured", None, 70, 30, "estimated")


def test_an_estimated_cost_counts_against_a_hard_cost_budget(
    world: tuple[Session, UUID, UUID],
) -> None:
    s, t, model = world
    add_rate(s, t, input_rate="1000", output_rate="1000")
    add_budget(s, t, metric="cost", amount="1", currency="USD")
    ledger.record_model_call(
        s, _call(t, "k", model), reported=Usage(), estimated_input=600_00, estimated_output=500_00
    )  # 110_000 tokens = 0.11
    with pytest.raises(BudgetDenied):
        evaluate(s, _scope(t), 1, ReserveDetail(model, 800_000, 100_000))


def test_a_succeeded_model_call_cannot_be_recorded_without_token_counts(
    owner_session: Session,
) -> None:
    """Before: a missing or None count was silently stored as 0."""
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    for bad in ({"output_tokens": 5}, {"input_tokens": None, "output_tokens": 5}):
        with pytest.raises(ledger.UsageNotReported):
            ledger.record(s, _call(t, f"k{len(bad)}", **bad))
    ledger.record(s, _call(t, "ok", input_tokens=0, output_tokens=0))  # a real zero is fine
    ledger.record(s, _call(t, "failed", status="failed"))  # nothing came back
    ledger.record(s, {"tenant_id": t, "event_key": "tool", "event_type": "tool_call"})


def test_a_provider_reported_cost_survives_unreported_tokens(
    world: tuple[Session, UUID, UUID],
) -> None:
    s, t, model = world
    ledger.record_model_call(
        s,
        _call(t, "k", model),
        reported=Usage(),
        estimated_input=10,
        estimated_output=10,
        provider_reported_cost=Decimal("0.25"),
        provider_reported_currency="USD",
    )
    kind = s.execute(text("SELECT cost_kind FROM shifde.usage_events")).scalar_one()
    assert kind == "provider_reported"


# ------------------------------------------------------------- Item 11: survives a crash
def test_a_usage_row_survives_a_crash_after_the_provider_answered(
    app_url: str, owner_db: Database
) -> None:
    t = uuid7()
    with owner_db.session() as owner:
        add_tenants(owner, t)
        owner.commit()
    key = f"step-{uuid7()}"
    db = Database(app_url, pool_size=3, max_overflow=0)  # the run's connection and the ledger's
    try:
        with db.session() as run_txn:
            set_tenant(run_txn, t)
            # the provider has answered; the row is written in its own transaction
            written = ledger.record_model_call_durably(
                db, t, _call(t, key), reported=Usage(), estimated_input=40, estimated_output=10
            )
            assert written.inserted
            run_txn.rollback()  # the run transaction dies before it commits
        with db.session() as s:
            set_tenant(s, t)
            assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
            kind, tokens = s.execute(
                text("SELECT cost_kind, input_tokens + output_tokens FROM shifde.usage_events")
            ).one()
            assert (kind, tokens) == ("unmeasured", 50)
            # the retry of the same step records the same event key: counted once
            again = ledger.record_model_call(
                s, _call(t, key), reported=Usage(), estimated_input=40, estimated_output=10
            )
            assert not again.inserted
            s.commit()
        with db.session() as s:
            set_tenant(s, t)
            count = s.execute(text("SELECT event_count FROM shifde.usage_rollups_daily"))
            assert count.scalar_one() == 1
            assert ledger.reconcile(s, t, _today(), _today()).consistent
    finally:
        db.dispose()


@pytest.mark.parametrize("as_owner", [False, True])
def test_the_durable_write_is_tenant_scoped_and_refuses_another_tenants_values(
    app_db: Database, owner_db: Database, as_owner: bool
) -> None:
    """Values naming client B under client A's durable write are refused before any session opens
    (B-2164), as `shifde_app` (where row-level security would also refuse them) and on the schema
    owner's database (where nothing else would): no row is written for either client."""
    a, b = uuid7(), uuid7()
    with owner_db.session() as owner:
        add_tenants(owner, a, b)
        owner.commit()
    with pytest.raises(ValueError, match="not the client of the durable write"):
        ledger.record_model_call_durably(
            owner_db if as_owner else app_db,
            a,
            _call(b, "k"),
            reported=Usage(),
            estimated_input=1,
            estimated_output=1,
        )
    with owner_db.session() as owner:
        count = owner.execute(
            text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id IN (:a, :b)"),
            {"a": a, "b": b},
        ).scalar_one()
    assert count == 0
    # A's own values are written (not vacuous)
    written = ledger.record_model_call_durably(
        app_db, a, _call(a, "k"), reported=Usage(), estimated_input=1, estimated_output=1
    )
    assert written.inserted
