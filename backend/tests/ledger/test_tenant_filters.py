"""Ledger lookups name their tenant (isolation review 1, item 6): they did not lean on
row-level security alone. These probes run as the schema owner (no RLS), so only the query's own
`tenant_id` condition keeps another tenant's rows out. Stand-in tables of later modules are
created in the test's transaction and rolled back (tests/ledger/util.py)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.ids import uuid7
from shifde.ledger import budgeting, enforcement, estimate
from tests.ledger.util import add_budget, add_model, add_rate, add_run, create_stand_ins


def _scope(tenant: UUID, project: UUID | None = None) -> enforcement.BudgetScope:
    return enforcement.BudgetScope(tenant, None, project, None, None)


def test_budgets_in_scope_ignores_another_tenants_budget_on_a_shared_scope_id(
    owner_session: Session,
) -> None:
    a, b, project = uuid7(), uuid7(), uuid7()
    other = add_budget(owner_session, b, scope_type="project", scope_id=project)
    own = add_budget(owner_session, a)
    found = [x["id"] for x in enforcement.budgets_in_scope(owner_session, _scope(a, project))]
    assert found == [own] and other not in found
    owner_session.rollback()


def test_quotas_in_scope_ignores_another_tenants_quota_on_a_shared_scope_id(
    owner_session: Session,
) -> None:
    a, b, project = uuid7(), uuid7(), uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, limit_value)"
            " VALUES (:i, :t, 'project', :p, 'tokens_per_day', 1)"
        ),
        {"i": uuid7(), "t": b, "p": project},
    )
    assert enforcement.quotas_in_scope(owner_session, _scope(a, project)) == []
    owner_session.rollback()


def test_a_model_of_another_tenant_is_not_priced(owner_session: Session) -> None:
    create_stand_ins(owner_session)
    a, b = uuid7(), uuid7()
    add_rate(owner_session, a, input_rate="1")
    model_b = add_model(owner_session, b)
    assert estimate.call_cost(owner_session, a, model_b, 10, 10) is None
    owner_session.rollback()


def test_an_event_does_not_inherit_attribution_from_another_tenants_run(
    owner_session: Session,
) -> None:
    create_stand_ins(owner_session)
    a, b, pipeline = uuid7(), uuid7(), uuid7()
    run_b = add_run(owner_session, b, pipeline_id=pipeline)
    ledger.record(
        owner_session,
        {"tenant_id": a, "event_key": "x", "event_type": "tool_call", "root_run_id": run_b},
    )
    got = owner_session.execute(
        text("SELECT pipeline_id FROM shifde.usage_events WHERE tenant_id = :t"), {"t": a}
    ).scalar_one()
    assert got is None
    owner_session.rollback()


def test_held_amount_does_not_count_a_reservation_on_another_tenants_run(
    owner_session: Session,
) -> None:
    create_stand_ins(owner_session)
    a, b = uuid7(), uuid7()
    run_b = add_run(owner_session, b)
    owner_session.execute(
        text(
            "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, budget_ids,"
            " amount, expires_at) VALUES (:i, :t, :r, 'k', '{}', 5, now() + interval '1 hour')"
        ),
        {"i": uuid7(), "t": a, "r": run_b},
    )
    budget = {"scope_type": "tenant", "scope_id": a, "period": "run", "currency": None}
    held = budgeting.held_amount(owner_session, a, run_b, budget, metric="tokens", since=None)
    assert held == 0
    owner_session.rollback()
