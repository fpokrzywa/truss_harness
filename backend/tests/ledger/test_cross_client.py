"""Cross-client isolation of the ledger: with tenant A's context set as `shifde_app`, tenant B's
usage, rollups, budgets, reservations, alerts, quotas and rate cards are invisible and
unwritable, and every ledger service query is tenant-scoped (also without row-level security).

The ledger has no routes; its service functions are the surface other modules call."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.clock import utcnow
from shifde.core.db import Database, set_platform_admin, set_tenant
from shifde.core.ids import uuid7
from shifde.ledger import budgeting, enforcement, pricing
from tests._support.tenants import add_tenants
from tests.ledger.util import add_budget, add_rate, add_rate_card, add_run, add_version

TABLES = (
    "usage_events",
    "usage_rollups_daily",
    "budgets",
    "budget_reservations",
    "budget_alerts",
    "quotas",
    "rate_cards",
    "rate_card_versions",
)


@dataclass
class World:
    a: UUID
    b: UUID
    budget_a: UUID
    budget_b: UUID
    global_version: UUID
    version_a: UUID
    version_b: UUID


def _today() -> date:
    return utcnow().date()


def _seed(s: Session, t: UUID, tag: str) -> UUID:
    ledger.record(
        s,
        {
            "tenant_id": t,
            "event_key": f"{tag}-event",
            "event_type": "model_call",
            "input_tokens": 100 if tag == "a" else 7000,
            "output_tokens": 0,
        },
    )
    budget = add_budget(s, t, amount="1000")
    s.execute(
        text(
            "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, budget_ids, "
            "amount, expires_at) VALUES (:i, :t, :r, 'k', '{}', 1, now() + interval '1 hour')"
        ),
        {"i": uuid7(), "t": t, "r": add_run(s, t)},  # a run of t (fk_budget_reservations__run)
    )
    s.execute(
        text(
            "INSERT INTO shifde.budget_alerts (id, tenant_id, budget_id, period_start, threshold, "
            "utilization) VALUES (:i, :t, :b, now(), 0.5, 0.5)"
        ),
        {"i": uuid7(), "t": t, "b": budget},
    )
    s.execute(
        text(
            "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, limit_value)"
            " VALUES (:i, :t, 'tenant', :t, 'tokens_per_day', 10)"
        ),
        {"i": uuid7(), "t": t},
    )
    return budget


@pytest.fixture
def world(owner_db: Database) -> Iterator[World]:
    a, b = uuid7(), uuid7()
    with owner_db.session() as s:
        add_tenants(s, a, b)
        global_version = add_rate(s, None, input_rate="1")
        version_a = add_rate(s, a, input_rate="5")
        version_b = add_rate(s, b, input_rate="9")
        budget_a, budget_b = _seed(s, a, "a"), _seed(s, b, "b")
        s.commit()
    yield World(a, b, budget_a, budget_b, global_version, version_a, version_b)


def _ids(s: Session, table: str) -> set[UUID]:
    return {r[0] for r in s.execute(text(f"SELECT id FROM shifde.{table}"))}


def _as(db: Database, tenant: UUID | None) -> Session:
    s = db.session()
    if tenant is not None:
        set_tenant(s, tenant)
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    return s


def test_tenant_b_rows_are_invisible_to_tenant_a(app_db: Database, world: World) -> None:
    with _as(app_db, world.a) as s:
        for table in TABLES:
            tenants = {r[0] for r in s.execute(text(f"SELECT tenant_id FROM shifde.{table}"))}
            assert world.b not in tenants, table
        assert _ids(s, "budgets") == {world.budget_a}
        assert _ids(s, "rate_card_versions") == {world.global_version, world.version_a}
        assert len(_ids(s, "usage_events")) == 1
        s.rollback()
    with _as(app_db, world.b) as s:
        assert _ids(s, "budgets") == {world.budget_b}
        assert _ids(s, "rate_card_versions") == {world.global_version, world.version_b}
        s.rollback()


def test_without_a_tenant_context_nothing_is_visible_except_the_global_rate_cards(
    app_db: Database, world: World
) -> None:
    with _as(app_db, None) as s:
        for table in TABLES:
            n = s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one()
            # only the one global rate card and its version are readable
            assert n == (1 if table in ("rate_cards", "rate_card_versions") else 0), table
        s.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO shifde.budgets (id, tenant_id, scope_type, scope_id, period, amount) "
        "VALUES (gen_random_uuid(), :other, 'tenant', :other, 'daily', 1)",
        "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, limit_value) "
        "VALUES (gen_random_uuid(), :other, 'tenant', :other, 'tokens_per_day', 1)",
        "INSERT INTO shifde.usage_events (id, tenant_id, event_key, event_type) "
        "VALUES (gen_random_uuid(), :other, 'x', 'tool_call')",
        "INSERT INTO shifde.usage_rollups_daily (id, tenant_id, day, dim_key, event_type, "
        "cost_kind) VALUES (gen_random_uuid(), :other, current_date, 'x', 'tool_call', "
        "'unmeasured')",
        "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, budget_ids, "
        "amount, expires_at) VALUES (gen_random_uuid(), :other, gen_random_uuid(), 'k', '{}', 1, "
        "now())",
        "INSERT INTO shifde.rate_cards (id, tenant_id, provider_key, model_key, name) "
        "VALUES (gen_random_uuid(), :other, 'p', 'm', 'n')",
        # a global card needs the platform administrator context
        "INSERT INTO shifde.rate_cards (id, tenant_id, provider_key, model_key, name) "
        "VALUES (gen_random_uuid(), NULL, 'p', 'm', 'n')",
        # review 1, item 5
        "INSERT INTO shifde.budget_alerts (id, tenant_id, budget_id, period_start, threshold, "
        "utilization) VALUES (gen_random_uuid(), :other, gen_random_uuid(), now(), 0.5, 0.5)",
        "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, version_no, "
        "effective_from, currency, unit, input_rate, output_rate, source, revision) VALUES "
        "(gen_random_uuid(), gen_random_uuid(), :other, 1, now(), 'USD', 'per_call', 1, 1, 's', "
        "'r')",
        "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, version_no, "
        "effective_from, currency, unit, input_rate, output_rate, source, revision) VALUES "
        "(gen_random_uuid(), gen_random_uuid(), NULL, 1, now(), 'USD', 'per_call', 1, 1, 's', "
        "'r')",
    ],
)
def test_tenant_a_cannot_write_rows_for_tenant_b_or_global_rows(
    app_db: Database, world: World, sql: str
) -> None:
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            s.execute(text(sql), {"other": world.b})
        assert getattr(err.value.orig, "sqlstate", None) == "42501"  # row-level security
        s.rollback()


def test_tenant_a_cannot_change_tenant_bs_rows(app_db: Database, world: World) -> None:
    with _as(app_db, world.a) as s:
        for sql in (
            "UPDATE shifde.budgets SET amount = 1 WHERE id = :b",
            "UPDATE shifde.budgets SET amount = 1 WHERE tenant_id = :t",
            "UPDATE shifde.usage_rollups_daily SET input_tokens = 0 WHERE tenant_id = :t",
            "UPDATE shifde.budget_reservations SET status = 'released' WHERE tenant_id = :t",
            "UPDATE shifde.quotas SET limit_value = 0 WHERE tenant_id = :t",
        ):
            done = s.execute(text(sql), {"b": world.budget_b, "t": world.b})
            assert done.rowcount == 0, sql  # B's rows are not even visible to the UPDATE
        s.rollback()
    with _as(app_db, world.b) as s:
        amount = s.execute(
            text("SELECT amount FROM shifde.budgets WHERE id = :b"), {"b": world.budget_b}
        ).scalar_one()
        assert amount == Decimal(1000)
        s.rollback()


def test_the_ledger_cannot_record_an_event_for_another_tenant(
    app_db: Database, world: World
) -> None:
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError):
            ledger.record(
                s,
                {
                    "tenant_id": world.b,
                    "event_key": "forged",
                    "event_type": "tool_call",
                },
            )
        s.rollback()


def test_ledger_service_queries_for_another_tenant_return_nothing_under_rls(
    app_db: Database, world: World
) -> None:
    scope_b = enforcement.BudgetScope(world.b, None, None, None, None)
    with _as(app_db, world.a) as s:
        rec = ledger.reconcile(s, world.b, _today(), _today())
        assert (rec.events_sum, rec.rollup_sum, rec.consistent) == (0, 0, True)
        assert enforcement.budgets_in_scope(s, scope_b) == []
        assert enforcement.quotas_in_scope(s, scope_b) == []
        spent = budgeting.used_amount(
            s,
            world.b,
            None,
            {"scope_type": "tenant", "scope_id": world.b, "period": "daily", "currency": None},
            metric="tokens",
            since=budgeting.period_start("daily"),
        )
        assert spent == 0
        # B's own override is not reachable: asking as B from A's context yields the global rate
        seen = pricing.resolve(s, world.b, "openai", "m1", utcnow())
        assert seen is not None and seen.id == world.global_version
        s.rollback()


def test_ledger_service_queries_filter_by_tenant_even_without_rls(
    owner_db: Database, world: World
) -> None:
    """The schema owner bypasses RLS, so these prove the queries themselves are tenant-scoped."""
    with owner_db.session() as s:
        rec = ledger.reconcile(s, world.a, _today(), _today())
        assert rec.events_sum == 100 and rec.consistent
        assert ledger.reconcile(s, world.a, _today(), _today(), deep=False).events_sum == 100
        assert [b["id"] for b in enforcement.budgets_in_scope(s, _scope(world.a))] == [
            world.budget_a
        ]
        assert [q["scope_id"] for q in enforcement.quotas_in_scope(s, _scope(world.b))] == [world.b]
        used = budgeting.used_amount(
            s,
            world.a,
            None,
            {"scope_type": "tenant", "scope_id": world.a, "period": "daily", "currency": None},
            metric="tokens",
            since=budgeting.period_start("daily"),
        )
        assert used == 100  # B's 7000 tokens are not counted
        own = pricing.resolve(s, world.a, "openai", "m1", utcnow())
        assert own is not None and own.id == world.version_a
        assert ledger.correct(s, world.a, uuid7(), reason="not an event") is None
        s.rollback()


def _scope(tenant_id: UUID) -> enforcement.BudgetScope:
    return enforcement.BudgetScope(tenant_id, None, None, None, None)


def test_a_platform_administrator_may_write_a_global_card_but_a_tenant_may_not(
    app_db: Database,
) -> None:
    with _as(app_db, uuid7()) as s:
        set_platform_admin(s, True)
        card = add_rate_card(s, None, model_key="global-only")
        add_version(s, card, None, 1)
        s.rollback()
    with _as(app_db, uuid7()) as s, pytest.raises(DBAPIError):
        add_rate_card(s, None, model_key="not-allowed")


# ---- isolation review 1, items 1 and 5: the SECURITY DEFINER version guard trusts nothing the
# caller names. Before the fix a version naming another tenant's card answered 23P01 ("starts in
# the past" / "overlaps"), an oracle on that card's version ranges, and took its advisory lock.
def _card_of(db: Database, version: UUID) -> UUID:
    with db.session() as s:
        return s.execute(
            text("SELECT rate_card_id FROM shifde.rate_card_versions WHERE id = :v"), {"v": version}
        ).scalar_one()


@pytest.mark.parametrize("backdated", [True, False])
def test_a_version_on_another_tenants_card_is_refused_with_42501(
    app_db: Database, owner_db: Database, world: World, backdated: bool
) -> None:
    card_b = _card_of(owner_db, world.version_b)
    start = utcnow() - timedelta(days=1) if backdated else utcnow() + timedelta(days=1)
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            add_version(s, card_b, world.b, 2, frm=start)
        assert getattr(err.value.orig, "sqlstate", None) == "42501"
        s.rollback()


def test_a_version_naming_another_tenants_card_under_own_tenant_leaks_nothing(
    app_db: Database, owner_db: Database, world: World
) -> None:
    """With its own tenant id the guard may look the card up, but the answer for B's card is the
    one for a card that does not exist."""
    codes = set()
    for card in (_card_of(owner_db, world.version_b), uuid7()):
        with _as(app_db, world.a) as s:
            with pytest.raises(DBAPIError) as err:
                add_version(s, card, world.a, 2, frm=utcnow() - timedelta(days=1))
            codes.add(getattr(err.value.orig, "sqlstate", None))
            s.rollback()
    assert len(codes) == 1, codes


def test_a_tenant_cannot_close_a_global_version_through_the_guard(
    app_db: Database, owner_db: Database, world: World
) -> None:
    card = _card_of(owner_db, world.global_version)
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            add_version(s, card, None, 2, frm=utcnow() + timedelta(days=1))
        assert getattr(err.value.orig, "sqlstate", None) == "42501"
        s.rollback()


# ---- governance isolation review 1, item 1 (ledger_0002, B-611): with no client context
# `NEW.tenant_id = current_tenant_id()` was NULL, so the guard's first check was skipped and the
# definer locked B's card, read it, closed B's open-ended version and ran the overlap check
# before WITH CHECK refused: three different errors (B's card ids and ranges leak) and a lock
# that delayed B's own writes. A raw insert, not `add_version`: its `_seed_context` would give a
# session without a context the row's own tenant.
_PROBE = text(
    "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, version_no, "
    "effective_from, currency, unit, input_rate, output_rate, source, revision) "
    "VALUES (:i, :c, :t, 2, :f, 'USD', 'per_1m_tokens', 1, 1, 'probe', 'r1')"
)


def _versions(db: Database) -> list[tuple[object, ...]]:
    with db.session() as s:
        rows = s.execute(
            text(
                "SELECT id, rate_card_id, tenant_id, version_no, effective_from, effective_to "
                "FROM shifde.rate_card_versions ORDER BY id"
            )
        )
        return [tuple(r) for r in rows]


def _refusal(err: pytest.ExceptionInfo[DBAPIError]) -> tuple[object, object]:
    orig = err.value.orig
    return getattr(orig, "sqlstate", None), getattr(
        getattr(orig, "diag", None), "message_primary", None
    )


@pytest.mark.parametrize("context", ["none", "tenant_a"])
@pytest.mark.parametrize("row_tenant", ["b", "global"])
def test_a_version_outside_the_callers_context_gets_one_answer_whatever_the_card(
    app_db: Database, owner_db: Database, world: World, context: str, row_tenant: str
) -> None:
    """B's real card (a start in the past, which overlapped; a start in the future, which RLS
    refused after the guard closed B's version) and an unknown card id answer the same refusal,
    with no client context and in A's; likewise a global row without the platform administrator
    context. Nothing of B's (or the global card's) changes."""
    tenant = world.b if row_tenant == "b" else None
    card = _card_of(owner_db, world.version_b if row_tenant == "b" else world.global_version)
    before = _versions(owner_db)
    answers = set()
    for card_id in (card, uuid7()):
        for start in (utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)):
            with _as(app_db, world.a if context == "tenant_a" else None) as s:
                with pytest.raises(DBAPIError) as err:
                    s.execute(_PROBE, {"i": uuid7(), "c": card_id, "t": tenant, "f": start})
                answers.add(_refusal(err))
                s.rollback()
    assert answers == {("42501", "rate card version is outside the caller's tenant")}
    assert _versions(owner_db) == before


@pytest.mark.parametrize("context", ["none", "tenant_a"])
def test_a_refused_version_does_not_queue_on_another_tenants_card_lock(
    app_db: Database, app_url: str, owner_db: Database, world: World, context: str
) -> None:
    """B adds a version to its own card and keeps its transaction open, so B holds the card's
    guard lock and its open-ended version row. A probe naming B's card, with a short lock
    timeout, gets the generic refusal at once: before the fix the guard queued on B's lock and
    answered 55P03 (an oracle: the card exists and B is writing it), and a guard that got the
    lock held it, and B's version row, while it read and closed B's version. (A refused
    statement's locks go when its transaction aborts, so B's lock is the one to probe.) B's
    write then goes on unaffected."""
    card_b = _card_of(owner_db, world.version_b)
    later = utcnow() + timedelta(days=1)
    writer_db = Database(app_url, pool_size=1, max_overflow=0)  # B's own, second connection
    try:
        with _as(writer_db, world.b) as writer:
            add_version(writer, card_b, world.b, 2, frm=later)  # B's guard holds the lock now
            with _as(app_db, world.a if context == "tenant_a" else None) as prober:
                prober.execute(text("SET LOCAL lock_timeout = '500ms'"))
                with pytest.raises(DBAPIError) as err:
                    prober.execute(_PROBE, {"i": uuid7(), "c": card_b, "t": world.b, "f": later})
                assert _refusal(err) == (
                    "42501",
                    "rate card version is outside the caller's tenant",
                )
                prober.rollback()
            closed = writer.execute(
                text("SELECT effective_to FROM shifde.rate_card_versions WHERE id = :v"),
                {"v": world.version_b},
            ).scalar_one()
            assert closed == later  # B's own guard closed B's version; B's transaction is live
            writer.rollback()
    finally:
        writer_db.dispose()


# ---- isolation review 1, item 2 (blocking): constraints bypass RLS, so a budget_alerts row of
# tenant A naming tenant B's budget id used to be accepted (or collide with B's slot and, with
# governance's ON CONFLICT DO NOTHING, silently stop B's alert).
@pytest.mark.parametrize("threshold", ["0.5", "0.9"])  # B's existing slot and a free one
def test_an_alert_cannot_name_another_tenants_budget(
    app_db: Database, world: World, threshold: str
) -> None:
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            s.execute(
                text(
                    "INSERT INTO shifde.budget_alerts (id, tenant_id, budget_id, period_start, "
                    "threshold, utilization) VALUES (:i, :t, :b, now(), :th, 0.5)"
                ),
                {"i": uuid7(), "t": world.a, "b": world.budget_b, "th": Decimal(threshold)},
            )
        # the same answer as for a budget id that does not exist: no oracle on B's slots
        assert getattr(err.value.orig, "sqlstate", None) == "23503"
        s.rollback()


def test_a_budget_id_is_unique_within_its_tenant_for_the_alert_foreign_key(
    owner_db: Database, world: World
) -> None:
    with owner_db.session() as s:
        (n,) = s.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conrelid = 'shifde.budgets'::regclass "
                "AND contype = 'u' AND pg_get_constraintdef(oid) = 'UNIQUE (tenant_id, id)'"
            )
        ).one()
        assert n == 1


# ---- isolation review 1, item 3: an event's rate_card_version_id is a global version or one of
# the event's own tenant; the foreign key alone accepted (and so confirmed) another tenant's.
def _event_with_version(s: Session, tenant: UUID, version: UUID) -> None:
    s.execute(
        text(
            "INSERT INTO shifde.usage_events (id, tenant_id, event_key, event_type, "
            "rate_card_version_id) VALUES (:i, :t, :k, 'tool_call', :v)"
        ),
        {"i": uuid7(), "t": tenant, "k": f"rv-{uuid7()}", "v": version},
    )


def test_an_event_cannot_name_another_tenants_rate_card_version(
    app_db: Database, world: World
) -> None:
    with _as(app_db, world.a) as s:
        with pytest.raises(DBAPIError) as err:
            _event_with_version(s, world.a, world.version_b)
        assert getattr(err.value.orig, "sqlstate", None) == "23503"
        s.rollback()
        with pytest.raises(DBAPIError) as err2:  # the answer for a version that does not exist
            _event_with_version(s, world.a, uuid7())
        assert getattr(err2.value.orig, "sqlstate", None) == "23503"
        s.rollback()


def test_an_event_may_name_a_global_or_its_own_tenants_rate_card_version(
    app_db: Database, world: World
) -> None:
    with _as(app_db, world.a) as s:
        _event_with_version(s, world.a, world.global_version)
        _event_with_version(s, world.a, world.version_a)
        s.rollback()


def test_the_owner_cannot_attach_another_tenants_version_either(
    owner_db: Database, world: World
) -> None:
    """The check names the tenant explicitly: it does not lean on row-level security."""
    with owner_db.session() as s:
        with pytest.raises(DBAPIError) as err:
            _event_with_version(s, world.a, world.version_b)
        assert getattr(err.value.orig, "sqlstate", None) == "23503"
        s.rollback()
