"""Cross-client isolation of the governance repository (plan recipe step 5, B-1801): with client
A's context set as `shifde_app`, client B's rate cards, versions, budgets, quotas, policies and
retention settings cannot be read, changed or referenced, through the repository functions and
through raw SQL, and every repository function also filters on the tenant id (checked with the
context of the wrong client and with no row-level security reach: the owner session).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from shifde.governance import repository as repo
from shifde.ledger.usage import ALL
from tests.governance.conftest import World
from tests.governance.runtime_rows import seed_runtime
from tests.ledger.util import add_budget, add_rate_card, add_version
from tests.tenancy.conftest import app_session


@dataclass(frozen=True)
class Seed:
    a: UUID
    b: UUID
    global_card: UUID
    card_a: UUID
    card_b: UUID
    version_b: UUID
    budget_a: UUID
    budget_b: UUID
    quota_b: UUID
    policy_a: UUID
    policy_b: UUID


def _quota(s: Session, t: UUID) -> UUID:
    qid = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.quotas (id, tenant_id, scope_type, scope_id, metric, limit_value)"
            " VALUES (:i, :t, 'tenant', :t, 'tokens_per_day', 10)"
        ),
        {"i": qid, "t": t},
    )
    return qid


def _policy(s: Session, t: UUID) -> UUID:
    pid = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.governance_policies (id, tenant_id, kind, name, definition) "
            "VALUES (:i, :t, 'lifecycle', 'p', '{}')"
        ),
        {"i": pid, "t": t},
    )
    s.execute(
        text(
            "INSERT INTO shifde.retention_policies (tenant_id, data_class, retain_days) "
            "VALUES (:t, 'audit_events', 400)"
        ),
        {"t": t},
    )
    return pid


@pytest.fixture
def seed(owner_db: Database, world: World) -> Seed:
    a, b = world.a.tenant, world.b.tenant
    with owner_db.session() as s:
        global_card = add_rate_card(s, None, "fake", "m-global")
        card_a = add_rate_card(s, a, "fake", "m-a")
        card_b = add_rate_card(s, b, "fake", "m-b")
        version_b = add_version(s, card_b, b, 1)
        out = Seed(
            a,
            b,
            global_card,
            card_a,
            card_b,
            version_b,
            add_budget(s, a),
            add_budget(s, b),
            _quota(s, b),
            _policy(s, a),
            _policy(s, b),
        )
        s.commit()
    return out


# ------------------------------------------------------------------ reads
def test_a_reads_only_its_own_and_the_global_rows(app_db: Database, seed: Seed) -> None:
    with app_session(app_db, seed.a) as s:
        cards = {r["id"] for r in repo.list_cards(s, seed.a)}
        assert cards == {seed.global_card, seed.card_a}
        assert repo.get_card(s, seed.a, seed.card_b) is None
        assert repo.get_card(s, seed.a, seed.global_card) is not None
        assert repo.list_versions(s, seed.a, seed.card_b) == []
        assert [r["id"] for r in repo.list_budgets(s, seed.a)] == [seed.budget_a]
        assert repo.get_budget(s, seed.a, seed.budget_b) is None
        assert repo.list_quotas(s, seed.a) == []
        assert repo.get_quota(s, seed.a, seed.quota_b) is None
        assert [r["id"] for r in repo.list_policies(s, seed.a, None)] == [seed.policy_a]
        assert repo.get_policy(s, seed.a, seed.policy_b) is None
        assert repo.policy_counts(s, seed.a) == {"lifecycle": 1}
        assert [r["data_class"] for r in repo.retention_rows(s, seed.a)] == ["audit_events"]
        assert repo.rate_versions(s, seed.a, [seed.version_b]) == {}
        s.rollback()


def test_the_platform_context_lists_only_the_global_cards(app_db: Database, seed: Seed) -> None:
    with app_session(app_db, None) as s, platform_admin_context(s):
        assert {r["id"] for r in repo.list_cards(s, None)} == {seed.global_card}
        assert repo.get_card(s, None, seed.card_a) is None
        s.rollback()


def test_the_tenant_filter_holds_without_row_level_security(owner_db: Database, seed: Seed) -> None:
    """The schema owner is not subject to row-level security: only the explicit tenant id keeps
    A's calls away from B's rows."""
    with owner_db.session() as s:
        assert repo.get_card(s, seed.a, seed.card_b) is None
        assert repo.list_versions(s, seed.a, seed.card_b) == []
        assert repo.next_version_no(s, seed.a, seed.card_b) == 1
        assert {r["id"] for r in repo.list_cards(s, seed.a)} == {seed.global_card, seed.card_a}
        assert repo.get_budget(s, seed.a, seed.budget_b) is None
        assert [r["id"] for r in repo.list_budgets(s, seed.a)] == [seed.budget_a]
        assert repo.get_quota(s, seed.a, seed.quota_b) is None
        assert repo.list_quotas(s, seed.a) == []
        assert repo.get_policy(s, seed.a, seed.policy_b) is None
        assert [r["id"] for r in repo.list_policies(s, seed.a, None)] == [seed.policy_a]
        assert repo.policy_counts(s, seed.b) == {"lifecycle": 1}
        assert repo.retention_rows(s, seed.a)[0]["retain_days"] == 400
        assert repo.rate_versions(s, seed.a, [seed.version_b]) == {}
        assert repo.budget_alerts(s, seed.a, seed.budget_b, utcnow()) == []
        s.rollback()


# ------------------------------------------------------------------ writes
def test_a_cannot_update_bs_rows_through_the_repository(
    app_db: Database, owner_db: Database, seed: Seed
) -> None:
    with app_session(app_db, seed.a) as s:
        row = repo.update_row(
            s, seed.a, "budgets", repo.BUDGET_COLUMNS, repo.BUDGET_UPDATABLE,
            seed.budget_b, None, {"name": "taken"},
        )  # fmt: skip
        assert row is None
        s.rollback()
    # the same call under A's id but B's context: the explicit filter still refuses
    with app_session(app_db, seed.b) as s:
        row = repo.update_row(
            s, seed.a, "budgets", repo.BUDGET_COLUMNS, repo.BUDGET_UPDATABLE,
            seed.budget_b, None, {"name": "taken"},
        )  # fmt: skip
        assert row is None
        s.rollback()
    with owner_db.session() as o:
        assert (
            o.execute(
                text("SELECT name FROM shifde.budgets WHERE id = :i"), {"i": seed.budget_b}
            ).scalar_one()
            != "taken"
        )


def test_a_cannot_insert_rows_for_b(app_db: Database, seed: Seed) -> None:
    with app_session(app_db, seed.a) as s:
        for insert, values in (
            (
                repo.insert_card,
                {
                    "id": uuid7(), "tenant_id": seed.b, "provider_key": "p", "model_key": "m",
                    "name": "n", "created_by": None,
                },
            ),
            (
                repo.insert_policy,
                {
                    "id": uuid7(), "tenant_id": seed.b, "kind": "lifecycle", "name": "n",
                    "scope_type": None, "scope_id": None, "definition": {}, "status": "active",
                    "created_by": None,
                },
            ),
        ):  # fmt: skip
            with pytest.raises(DBAPIError) as err:
                insert(s, values)
            assert getattr(err.value.orig, "sqlstate", None) == "42501"
            s.rollback()
        with pytest.raises(DBAPIError) as err:
            repo.upsert_retention(s, seed.b, "usage_events", 30, False, uuid7())
        assert getattr(err.value.orig, "sqlstate", None) == "42501"
        s.rollback()


def test_a_cannot_version_bs_card(app_db: Database, seed: Seed) -> None:
    """A version naming B's card is refused by the card's composite key even when A writes its
    own tenant id (the card is invisible to A)."""
    values = {
        "id": uuid7(), "rate_card_id": seed.card_b, "tenant_id": seed.a, "version_no": 5,
        "effective_from": "2000-01-01T00:00:00Z", "effective_to": None, "currency": "XTS",
        "unit": "per_call", "input_rate": 1, "output_rate": 1, "cached_read_rate": None,
        "cached_write_rate": None, "other_rates": {}, "source": "s", "revision": "r",
        "created_by": None,
    }  # fmt: skip
    with app_session(app_db, seed.a) as s:
        with pytest.raises(DBAPIError):
            repo.insert_version(s, values)
        s.rollback()


# ------------------------------------------------------------------ raw SQL as shifde_app
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM shifde.governance_policies WHERE id = :b_policy",
        "SELECT count(*) FROM shifde.retention_policies WHERE tenant_id = :b",
        "UPDATE shifde.governance_policies SET name = 'x' WHERE id = :b_policy",
        "UPDATE shifde.governance_policies SET name = 'x' WHERE tenant_id = :b",
        "UPDATE shifde.retention_policies SET retain_days = 1 WHERE tenant_id = :b",
        "UPDATE shifde.quotas SET limit_value = 1 WHERE tenant_id = :b",
        "UPDATE shifde.budgets SET amount = 1 WHERE tenant_id = :b",
    ],
)
def test_raw_sql_as_the_app_role_sees_and_changes_nothing_of_b(
    app_db: Database, seed: Seed, sql: str
) -> None:
    with app_session(app_db, seed.a) as s:
        r = s.execute(text(sql), {"b": seed.b, "b_policy": seed.policy_b})
        if sql.startswith("SELECT"):
            assert r.scalar_one() == 0
        else:
            assert r.rowcount == 0  # type: ignore[attr-defined]  # mypy: CursorResult has it
        s.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO shifde.governance_policies (id, tenant_id, kind) "
        "VALUES (gen_random_uuid(), :b, 'lifecycle')",
        "INSERT INTO shifde.retention_policies (tenant_id, data_class, retain_days) "
        "VALUES (:b, 'usage_events', 5)",
        "INSERT INTO shifde.governance_policies (id, tenant_id, kind) "
        "VALUES (gen_random_uuid(), NULL, 'lifecycle')",
    ],
)
def test_raw_sql_as_the_app_role_cannot_write_for_b_or_for_nobody(
    app_db: Database, seed: Seed, sql: str
) -> None:
    with app_session(app_db, seed.a) as s:
        with pytest.raises(DBAPIError) as err:
            s.execute(text(sql), {"b": seed.b})
        assert getattr(err.value.orig, "sqlstate", None) in ("42501", "23502")
        s.rollback()


def test_without_a_client_context_no_governance_row_is_visible(
    app_db: Database, seed: Seed
) -> None:
    with app_session(app_db, None) as s:
        for table in ("governance_policies", "retention_policies", "budgets", "quotas"):
            assert s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one() == 0
        s.rollback()


# ------------------------------------------------------------------ runtime reads (B-2105)
def _performance(s: Session, tenant: UUID, group_by: str) -> list[tuple[str, int]]:
    end = utcnow() + timedelta(minutes=1)
    rows = repo.performance(s, tenant, group_by, end - timedelta(days=7), end, ALL)
    return [(str(r["key"]), int(r["n"])) for r in rows]


def test_the_runtime_reads_filter_on_the_tenant_without_row_level_security(
    owner_db: Database, world: World
) -> None:
    """The reads B-2105 turned on, as the schema owner over real runs of both clients: only each
    query's own tenant condition keeps B's runs, steps, approvals, artifacts and holds out of A's
    answer (isolation review 1, F1)."""
    a, b, model = world.a, world.b, uuid7()
    with owner_db.session() as s:
        ra = seed_runtime(s, a.tenant, a.ws, a.project, n=1, model=model)
        rb = seed_runtime(s, b.tenant, b.ws, b.project, n=2, model=model)
        assert (repo.pending_approvals(s, a.tenant), repo.pending_approvals(s, b.tenant)) == (1, 2)
        cutoff = utcnow() - timedelta(days=90)
        for data_class in ("run_artifacts", "run_steps_content"):
            assert repo.count_older_than(s, a.tenant, data_class, cutoff) == 1, data_class
            assert repo.count_older_than(s, b.tenant, data_class, cutoff) == 2, data_class
        assert (repo.stale_holds(s, a.tenant), repo.stale_holds(s, b.tenant)) == (1, 2)
        for group_by, key_a, key_b in (
            ("agent", ra.agent, rb.agent),
            ("model", model, model),
            ("pipeline", ra.pipeline, rb.pipeline),
        ):
            assert _performance(s, a.tenant, group_by) == [(str(key_a), 1)], group_by
            assert _performance(s, b.tenant, group_by) == [(str(key_b), 2)], group_by
        for key, step in (("own", ra.steps[0]), ("theirs", rb.steps[0])):
            s.execute(
                text(
                    "INSERT INTO shifde.usage_events (id, tenant_id, event_key, event_type, "
                    "step_id) VALUES (:i, :t, :k, 'tool_call', :s)"
                ),
                {"i": uuid7(), "t": a.tenant, "k": key, "s": step},
            )
        names = dict(
            s.execute(
                text(f"SELECT q.event_key, q.step_name FROM ({repo.event_select(s)}"
                     "WHERE u.tenant_id = :t) q"),
                {"t": a.tenant},
            ).all()
        )  # fmt: skip
        assert names == {"own": ra.step_name, "theirs": None}
        s.rollback()


def test_performance_joins_a_step_to_a_run_of_its_own_client_only(
    owner_db: Database, world: World
) -> None:
    """F2: the model grouping joins `runs` on (id, tenant_id). The composite key
    `fk_run_steps__run` already refuses a step of A naming B's run; with it dropped in this
    rolled-back transaction, such a step still counts nowhere for A."""
    a, b, model = world.a, world.b, uuid7()
    with owner_db.session() as s:
        seed_runtime(s, a.tenant, a.ws, a.project, n=1, model=model)
        rb = seed_runtime(s, b.tenant, b.ws, b.project, n=2, model=model)
        s.execute(text("ALTER TABLE shifde.run_steps DROP CONSTRAINT fk_run_steps__run"))
        now = utcnow()
        s.execute(
            text(
                "INSERT INTO shifde.run_steps (id, tenant_id, run_id, seq, kind, name, status, "
                "model_id, started_at, finished_at) VALUES (:i, :t, :r, 9, 'model_call', 'x', "
                "'succeeded', :m, :s, :f)"
            ),
            {"i": uuid7(), "t": a.tenant, "r": rb.runs[0], "m": model,
             "s": now - timedelta(hours=2), "f": now - timedelta(hours=1)},
        )  # fmt: skip
        assert _performance(s, a.tenant, "model") == [(str(model), 1)]
        assert _performance(s, b.tenant, "model") == [(str(model), 2)]
        s.rollback()
