"""Budget holds survive a crash (issue #82, design 7.1 and 12.2).

A run's model attempt holds its tokens and sets the provider-request marker (as `call_model`
does) and then the worker dies: no settle, no release. The hold does not expire after its TTL and
keeps counting; when an orphan handler finds the run (the reaper's audit pass, a recovering
advance, `settle_terminal`), it becomes `settled_unknown` at the full held amount with one usage
row marked as unknown usage, counted once as used and no longer as held. A hold without the
marker is released by the same handlers and writes no usage. `settle_ended_run` (runtime_0004)
leaves a marked hold `held` for the handler.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ledger import budgeting, enforcement, reservations
from shifde.runtime import repository as repo
from shifde.runtime import state
from shifde.runtime.governance import BudgetScope, PolicyBudgetHooks, ReserveDetail
from shifde.runtime.settle import settle_terminal
from shifde.runtime.worker import Worker
from tests.build.service_support import REGISTRY
from tests.conftest import CLEAN_TABLES
from tests.ledger.util import add_budget, add_model
from tests.runtime.conftest import app_session
from tests.runtime.test_lifecycle import C, advance, fake, two

__all__ = ["advance", "fake", "two"]  # fixtures shared with the RT-11 tests

AMOUNT = 600
for _table in ("budgets", "budget_reservations", "usage_events", "usage_rollups_daily"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


def _worker(settings: Settings) -> Worker:
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    return Worker(settings, worker_id="w-reaper", runtime=runtime, hooks=PolicyBudgetHooks(),
                  concurrency=1, lease_seconds=2, poll_ms=50, reap_seconds=3600.0)  # fmt: skip


def _attempt(o: Session, c: C, budget: UUID, *, marked: bool, name: str = "model:0") -> UUID:
    """`c`'s run holds AMOUNT for attempt 1 of model step `name` and (when `marked`) records that
    the provider call may have started, with its `running` step row, as `call_model` does; then
    the worker dies. Returns the hold's id."""
    scope = BudgetScope(c.tenant, None, None, None, c.run)
    detail = ReserveDetail(est_input_tokens=AMOUNT - 100, est_output_tokens=100)
    res = PolicyBudgetHooks().reserve(o, scope, c.run, f"{name}:1", AMOUNT, detail)
    assert res.id is not None and res.budget_ids == [budget]
    if marked:
        repo.insert_step(o, {"tenant_id": c.tenant, "run_id": c.run, "seq": 7, "attempt": 1,
                             "kind": "model_call", "name": name, "status": "running",
                             "model_id": add_model(o, c.tenant)})  # fmt: skip
        reservations.mark_provider_request(o, c.tenant, res.id)
    return res.id


def _lose_lease(o: Session, c: C, *, exhausted: bool = False) -> None:
    """`c`'s run is `running` under its job, whose lease has expired (the worker died)."""
    o.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'w-dead', attempts = "
            "CASE WHEN :x THEN max_attempts ELSE 1 END, "
            "lease_expires_at = now() - interval '1 second' WHERE id = :j"
        ),
        {"j": c.job, "x": exhausted},
    )
    o.execute(
        text(
            "UPDATE shifde.runs SET status = 'running', lease_token = :t, lease_job_id = :j, "
            "state_version = state_version + 1, attempt_count = 1, started_at = now() "
            "WHERE id = :r"
        ),
        {"t": uuid7(), "j": c.job, "r": c.run},
    )


def _hold(o: Session, hold: UUID) -> tuple[Any, ...]:  # Any: row values
    o.rollback()
    row = o.execute(
        text("SELECT status, settled_amount FROM shifde.budget_reservations WHERE id = :i"),
        {"i": hold},
    ).one()
    return tuple(row)


def _unknown(o: Session, hold: UUID) -> list[tuple[Any, ...]]:  # Any: row values
    o.rollback()
    rows = o.execute(
        text(
            "SELECT input_tokens + output_tokens, status, provider_reported_usage FROM "
            "shifde.usage_events WHERE event_key = :k"
        ),
        {"k": f"reservation_unknown:{hold}"},
    ).all()
    return [tuple(r) for r in rows]


def _usage_count(o: Session, tenant: UUID) -> int:
    o.rollback()
    return int(
        o.execute(
            text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
    )


def _used_and_held(app_db: Database, c: C, budget: UUID) -> tuple[Decimal, Decimal]:
    rule = {"id": budget, "scope_type": "tenant", "scope_id": c.tenant, "period": "daily"}
    with app_session(app_db, c.tenant) as s:
        return budgeting.used_and_held(
            s, c.tenant, None, rule, metric="tokens", since=budgeting.period_start("daily")
        )


def _expire_and_evaluate(app_db: Database, owner_db: Database, c: C) -> None:
    """Every hold is past its TTL, then a budget check runs its expiry sweep (as `shifde_app`)."""
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.budget_reservations SET expires_at = now() - interval '1 second' "
                "WHERE tenant_id = :t"
            ),
            {"t": c.tenant},
        )
        o.commit()
    with app_session(app_db, c.tenant) as s:
        enforcement.evaluate(
            s, BudgetScope(c.tenant, None, None, None, None), 1, ReserveDetail(est_output_tokens=1)
        )
        s.commit()


# ------------------------------------------------------------------ the crash test
def test_a_marked_hold_survives_a_crash_and_its_ttl_until_the_reaper_charges_it_in_full(
    two: tuple[C, C], app_db: Database, owner_db: Database, settings: Settings
) -> None:
    a, b = two
    with owner_db.session() as o:
        budget = add_budget(o, a.tenant, amount="100000")
        hold = _attempt(o, a, budget, marked=True)
        _lose_lease(o, a)
        o.commit()
    _expire_and_evaluate(app_db, owner_db, a)
    with owner_db.session() as o:
        assert _hold(o, hold) == ("held", None)  # past its TTL, never expired
    assert _used_and_held(app_db, a, budget) == (0, AMOUNT)
    w = _worker(settings)
    try:
        w._reap()  # the lease expired with attempts left: requeued, its holds charged
    finally:
        w.db.dispose()
    with owner_db.session() as o:
        assert _hold(o, hold) == ("settled_unknown", AMOUNT)
        assert _unknown(o, hold) == [(AMOUNT, "failed", {"estimated": True, "usage_unknown": True})]
        assert _usage_count(o, a.tenant) == 1
        assert _usage_count(o, b.tenant) == 0
    assert _used_and_held(app_db, a, budget) == (AMOUNT, 0)  # now used, no longer held


def test_without_the_marker_the_reaper_releases_the_hold_and_writes_no_usage(
    two: tuple[C, C], owner_db: Database, settings: Settings
) -> None:
    a, _ = two
    with owner_db.session() as o:
        budget = add_budget(o, a.tenant, amount="100000")
        hold = _attempt(o, a, budget, marked=False)
        _lose_lease(o, a)
        o.commit()
    w = _worker(settings)
    try:
        w._reap()
    finally:
        w.db.dispose()
    with owner_db.session() as o:
        assert _hold(o, hold) == ("released", None)
        assert _usage_count(o, a.tenant) == 0


def test_an_unmarked_hold_past_its_ttl_is_expired_and_no_longer_counts(
    two: tuple[C, C], app_db: Database, owner_db: Database
) -> None:
    a, _ = two
    with owner_db.session() as o:
        budget = add_budget(o, a.tenant, amount="100000")
        hold = _attempt(o, a, budget, marked=False)
        o.commit()
    assert _used_and_held(app_db, a, budget) == (0, AMOUNT)
    _expire_and_evaluate(app_db, owner_db, a)
    with owner_db.session() as o:
        assert _hold(o, hold) == ("expired", None)
    assert _used_and_held(app_db, a, budget) == (0, 0)


def test_a_recovering_advance_charges_a_marked_hold_and_releases_an_unmarked_one(
    two: tuple[C, C], advance: Any, fake: Any, owner_db: Database
) -> None:
    """The run is `running` under the job this worker holds (its previous attempt abandoned it):
    the advance recovers it to pending, settling its orphaned holds in that transaction."""
    a, b = two
    with owner_db.session() as o:
        budget = add_budget(o, a.tenant, amount="100000")
        marked = _attempt(o, a, budget, marked=True)
        unmarked = _attempt(o, a, budget, marked=False, name="model:1")
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'running', lease_token = :t, state_version = 2, "
                "attempt_count = 1 WHERE id = :r"
            ),
            {"t": uuid7(), "r": a.run},
        )
        o.commit()
    assert advance(a.tenant, a.run, a.job) == "done"
    with owner_db.session() as o:
        assert _hold(o, marked) == ("settled_unknown", AMOUNT)
        assert _hold(o, unmarked) == ("released", None)
        assert len(_unknown(o, marked)) == 1 and _unknown(o, unmarked) == []
        assert _usage_count(o, b.tenant) == 0


# ------------------------------------------------------------------ the end of a run
def test_settle_terminal_charges_a_marked_hold_of_the_ended_run_once(
    two: tuple[C, C], owner_db: Database, settings: Settings
) -> None:
    a, _ = two
    with owner_db.session() as o:
        budget = add_budget(o, a.tenant, amount="100000")
        hold = _attempt(o, a, budget, marked=True)
        row = repo.transition(o, a.tenant, a.run, state.PENDING, state.TIMED_OUT,
                              finished_at=utcnow(),
                              error_message="the run deadline passed")  # fmt: skip
        assert row is not None
        settle_terminal(o, row, state.TIMED_OUT, settings)
        settle_terminal(o, row, state.TIMED_OUT, settings)  # twice: one usage row
        o.commit()
        assert _hold(o, hold) == ("settled_unknown", AMOUNT)
        assert len(_unknown(o, hold)) == 1


@pytest.mark.parametrize("marked", [True, False])
def test_a_run_the_reaper_fails_keeps_a_marked_hold_held_until_its_audit_pass_charges_it(
    two: tuple[C, C], app_db: Database, owner_db: Database, settings: Settings, marked: bool
) -> None:
    """Attempts exhausted: `shifde_reap_expired_leases` fails the run (`worker_lost`) and its
    `settle_ended_run` releases only an unmarked hold; `_audit_reaped` converts a marked one."""
    a, _ = two
    with owner_db.session() as o:
        budget = add_budget(o, a.tenant, amount="100000")
        hold = _attempt(o, a, budget, marked=marked)
        _lose_lease(o, a, exhausted=True)
        o.commit()
    with app_session(app_db, None) as s:  # the reaper's database pass, no client context
        reaped = s.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        s.commit()
    assert [tuple(r) for r in reaped] == [(a.tenant, a.run, "worker_lost")]
    with owner_db.session() as o:
        assert _hold(o, hold) == (("held", None) if marked else ("released", None))
    w = _worker(settings)
    try:
        w._audit_reaped(a.tenant, a.run, "worker_lost")
        w._audit_reaped(a.tenant, a.run, "worker_lost")  # twice: one usage row
    finally:
        w.db.dispose()
    with owner_db.session() as o:
        if marked:
            assert _hold(o, hold) == ("settled_unknown", AMOUNT)
            assert len(_unknown(o, hold)) == 1
        else:
            assert _hold(o, hold) == ("released", None)
            assert _usage_count(o, a.tenant) == 0


def test_the_reapers_audit_pass_naming_bs_run_with_as_client_charges_nothing(
    two: tuple[C, C], owner_db: Database, settings: Settings
) -> None:
    a, b = two
    with owner_db.session() as o:
        budget = add_budget(o, b.tenant, amount="100000")
        hold = _attempt(o, b, budget, marked=True)
        o.commit()
    w = _worker(settings)
    try:
        w._audit_reaped(a.tenant, b.run, "worker_lost")
    finally:
        w.db.dispose()
    with owner_db.session() as o:
        assert _hold(o, hold) == ("held", None)
        assert _usage_count(o, b.tenant) == 0 and _usage_count(o, a.tenant) == 0
