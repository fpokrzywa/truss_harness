"""Cross-client isolation of `ledger.reservations` (issue #82, isolation-critical): with client A's
context as `shifde_app`, and on the schema owner (no row-level security: the statements' own
client predicates are all there is), A's orphan settle, reserve, mark, settle and release never
change client B's holds or write usage in B, even when given B's run id or hold id; B's
direct-invocation holds are invisible under A's context and never count in A's budgets."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.ledger import budgeting, reservations
from tests._support.tenants import add_tenants
from tests.ledger.util import add_budget, add_run

MODES = ["app-role", "schema-owner"]


@dataclass(frozen=True)
class Side:
    tenant: UUID
    run: UUID
    budget: UUID
    step_hold: UUID  # held, marked, of `run`
    direct_hold: UUID  # held, marked, of a direct invocation


def _side(o: Session, tenant: UUID) -> Side:
    run = add_run(o, tenant)
    budget = add_budget(o, tenant, amount="100000")
    step = reservations.reserve_step(o, tenant, run, "m:1", [budget], 40, Decimal(0), None)
    direct = reservations.reserve_invocation(o, tenant, [budget], 60, Decimal(0), None)
    for hold in (step, direct):
        reservations.mark_provider_request(o, tenant, hold)
    return Side(tenant, run, budget, step, direct)


@pytest.fixture
def two(owner_db: Database) -> Iterator[tuple[Side, Side]]:
    a, b = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, a, b)
        pair = _side(o, a), _side(o, b)
        o.commit()
    yield pair


def _state(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        holds = o.execute(
            text(
                "SELECT id, status, amount, settled_amount, provider_request_at, expires_at "
                "FROM shifde.budget_reservations WHERE tenant_id = :t ORDER BY id"
            ),
            {"t": tenant},
        ).all()
        usage = o.execute(
            text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
    return tuple(map(tuple, holds)), usage


@pytest.fixture(params=MODES)
def as_a(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Iterator[Any]:  # Any: session factory
    def session(tenant: UUID) -> Session:
        if request.param == "schema-owner":
            return owner_db.session()
        s = app_db.session()
        set_tenant(s, tenant)
        assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        return s

    yield session


def test_a_never_changes_bs_holds_or_writes_usage_in_b(
    two: tuple[Side, Side], as_a: Any, owner_db: Database
) -> None:
    a, b = two
    before = _state(owner_db, b.tenant)
    with as_a(a.tenant) as s:
        # B's run id under A's client id: no hold of B is found or changed
        assert reservations.settle_orphaned(s, a.tenant, b.run) == 0
        for hold in (b.step_hold, b.direct_hold):
            reservations.mark_provider_request(s, a.tenant, hold)
            reservations.settle(s, a.tenant, hold, 0, Decimal(0))
            reservations.release(s, a.tenant, hold)
        reservations.expire_unstarted(s, a.tenant)
        s.commit()
    assert _state(owner_db, b.tenant) == before
    with as_a(a.tenant) as s:  # a hold for B's run under A's client is refused by the database
        with pytest.raises(DBAPIError):
            reservations.reserve_step(s, a.tenant, b.run, "x:1", [], 1, Decimal(0), None)
        s.rollback()
    assert _state(owner_db, b.tenant) == before
    with as_a(a.tenant) as s:  # not vacuous: A's own run is settled the same way
        assert reservations.settle_orphaned(s, a.tenant, a.run) == 1
        s.commit()
    assert _state(owner_db, a.tenant)[1] == 1
    assert _state(owner_db, b.tenant) == before


def test_under_as_context_bs_holds_are_invisible_and_bs_client_id_reaches_nothing(
    two: tuple[Side, Side], app_db: Database, owner_db: Database
) -> None:
    a, b = two
    before = _state(owner_db, b.tenant)
    with app_db.session() as s:
        set_tenant(s, a.tenant)
        direct = s.execute(
            text("SELECT id FROM shifde.budget_reservations WHERE run_id IS NULL")
        ).all()
        assert [r[0] for r in direct] == [a.direct_hold]
        # even naming B's client, row-level security hides B's rows from A's context
        assert reservations.settle_orphaned(s, b.tenant, b.run) == 0
        reservations.release(s, b.tenant, b.direct_hold)
        reservations.expire_unstarted(s, b.tenant)
        # B's budget id never finds B's direct hold (60) for A: only A's own run hold (40)
        rule = {"id": b.budget, "scope_type": "tenant", "scope_id": a.tenant, "period": "daily"}
        since = budgeting.period_start("daily")
        held = budgeting.used_and_held(s, a.tenant, None, rule, metric="tokens", since=since)
        assert held == (0, 40)
        s.commit()
    assert _state(owner_db, b.tenant) == before
