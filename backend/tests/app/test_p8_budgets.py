"""P8 budgets and quotas through runs and the worker, moved from SETH 3ba0e06
`tests/test_p8_budgets.py`; assertions unchanged, imports rewritten to `tests._support.*`.

Not here: `test_budget_administration_validates_audits_and_uses_optimistic_locking`,
`test_budget_admin_requires_the_budget_permissions_and_is_tenant_scoped` and
`test_a_client_may_echo_the_whole_budget_back_but_not_change_its_scope_or_metric` were moved to
`tests/governance/test_p8_budgets_usage.py` (B-1813).
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime.governance import BudgetDenied, BudgetScope, PolicyBudgetHooks, Reservation
from tests._support.fullapp import World
from tests._support.p2util import login, tenant_id
from tests._support.p3util import ADMIN, A
from tests._support.p4util import set_tenant_setting, tenant_scope
from tests._support.p5util import start_run, thread_worker, wait_run
from tests._support.p8util import agent_scope, budget, events_of, priced_stack, project_scope


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


def _decisions(owner: Session, run_id: str) -> list[Any]:
    return _rows(
        owner,
        "SELECT decision, reason_codes, policy_ids FROM shifde.policy_decisions "
        "WHERE run_id = :r AND decision = 'deny'",
        r=run_id,
    )


# ------------------------------------------------------------------ enforcement before execution
def test_a_cost_budget_stops_a_run_before_it_starts_and_the_denial_is_a_recorded_decision(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    b = budget(client, h, project_scope(owner_session), metric="cost", amount=0)
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    assert run["status"] == "failed" and run["error_class"] == "budget_denied"
    assert _rows(owner_session, "SELECT count(*) FROM shifde.jobs")[0][0] == 0
    (decision,) = _decisions(owner_session, run["id"])
    assert decision[1] == ["budget_denied"] and [str(i) for i in decision[2]] == [b["id"]]
    assert _rows(owner_session, "SELECT count(*) FROM shifde.usage_events")[0][0] == 0


def test_a_cost_budget_denies_a_model_call_mid_run_and_reconciles_after(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    settings = settings.model_copy(update={"runtime_output_reserve_tokens": 10})
    first = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        assert wait_run(client, h, first["id"])["status"] == "completed"
    spent = float(events_of(owner_session, first["id"])[0]["cost_amount"])
    # room for what was spent plus half of it: the next run starts, its first call does not fit
    b = budget(client, h, project_scope(owner_session), metric="cost", amount=round(spent * 1.5, 6))
    second = start_run(client, h, stack.agent["id"], "what is the weather again")
    assert second["status"] == "pending"  # the pre-flight check passed
    with thread_worker(settings):
        done = wait_run(client, h, second["id"])
    assert done["status"] == "failed" and done["error_class"] == "budget_denied", done
    assert done["usage"]["model_calls"] == 0
    (decision,) = _decisions(owner_session, second["id"])
    assert [str(i) for i in decision[2]] == [b["id"]]
    status = client.get(f"{A}/budgets/{b['id']}/status", headers=h).json()
    assert status["settled"] == pytest.approx(spent) and status["reserved"] == 0
    assert status["remaining"] == pytest.approx(round(spent * 1.5, 6) - spent)
    assert status["utilization"] == pytest.approx(spent / round(spent * 1.5, 6))
    assert "denied" in done["error_message"] or "exceeded" in done["error_message"]


def test_a_token_budget_by_pipeline_principal_and_agent_scope_applies(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    uid = _rows(owner_session, "SELECT id FROM shifde.users WHERE primary_email = :e", e=ADMIN)[0][
        0
    ]
    budget(client, h, {"type": "principal", "id": str(uid)}, amount=0)
    denied = start_run(client, h, stack.agent["id"], "hello")
    assert denied["error_class"] == "budget_denied"
    other = budget(client, h, agent_scope(stack.agent["id"]), amount=10)
    assert (
        other["scope"] == agent_scope(stack.agent["id"]) and other["on_exceed_effective"] == "deny"
    )


def test_a_mid_run_token_budget_with_the_p5_columns_still_denies(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """P5 rows (day / month periods, limit_tokens, no metric) keep working unchanged."""
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    pid = project_scope(owner_session)["id"]
    owner_session.execute(
        text(
            "INSERT INTO shifde.budgets (id, tenant_id, scope_type, scope_id, period, limit_tokens) "
            "VALUES (gen_random_uuid(), :t, 'project', :p, 'day', 5)"
        ),
        {"t": tenant_id(owner_session, "demo-a"), "p": pid},
    )
    owner_session.commit()
    listed = client.get(f"{A}/budgets", headers=h).json()["items"]
    assert (
        listed[0]["period"] == "daily"
        and listed[0]["amount"] == 5
        and listed[0]["metric"] == "tokens"
    )
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "failed" and done["error_class"] == "budget_denied"


# ------------------------------------------------------------------ race
def test_two_calls_racing_for_the_last_of_a_budget_exactly_one_gets_it(
    client: TestClient, world: World, owner_session: Session, app_url: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    tid = tenant_id(owner_session, "demo-a")
    budget(client, h, tenant_scope(owner_session), amount=100)
    pair = [
        UUID(start_run(client, h, stack.agent["id"], f"go {i}", key=f"race-{i}")["id"])
        for i in range(2)
    ]
    run_row = owner_session.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": pair[0]}
    ).one()
    pool = Database(app_url, pool_size=4)
    hooks = PolicyBudgetHooks()

    def attempt(
        run_id: UUID, key: str, gate: threading.Barrier, outcomes: list[Reservation | None]
    ) -> None:
        scope = BudgetScope(tid, run_row[0], run_row[1], UUID(stack.agent["id"]), run_id)
        with pool.session() as s:
            db.set_tenant(s, tid)
            gate.wait()
            try:
                res = hooks.reserve(s, scope, run_id, key, 60)
                s.commit()
                outcomes.append(res)
            except BudgetDenied:
                s.rollback()
                outcomes.append(None)

    try:
        for round_no in range(5):
            gate = threading.Barrier(2)
            outcomes: list[Reservation | None] = []
            threads = [
                threading.Thread(target=attempt, args=(r, f"k{round_no}", gate, outcomes))
                for r in pair
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            winners = [o for o in outcomes if o is not None]
            assert len(outcomes) == 2 and len(winners) == 1, f"round {round_no}: {outcomes}"
            with pool.session() as s:  # free the hold for the next round
                db.set_tenant(s, tid)
                hooks.release(s, winners[0])
                s.commit()
    finally:
        pool.dispose()
    held = _rows(
        owner_session, "SELECT count(*) FROM shifde.budget_reservations WHERE status = 'held'"
    )
    assert held[0][0] == 0


def test_run_starters_are_serialized_per_rule_until_the_run_row_is_committed(
    client: TestClient, world: World, owner_session: Session, app_url: str
) -> None:
    """The pre-flight check and the run insert share one transaction; the per-rule advisory lock
    makes a second starter wait for the first, so both can never count the same free slot."""
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    tid = tenant_id(owner_session, "demo-a")
    r = client.post(
        f"{A}/quotas",
        json={"scope": tenant_scope(owner_session), "metric": "runs_per_day", "limit": 1},
        headers=h,
    )
    assert r.status_code == 201
    ws, pj = owner_session.execute(
        text(
            "SELECT workspace_id, id FROM shifde.projects WHERE slug = 'sample' AND tenant_id = :t"
        ),
        {"t": tid},
    ).one()
    scope = BudgetScope(tid, ws, pj, UUID(stack.agent["id"]), uuid7())
    pool = Database(app_url, pool_size=3)
    hooks = PolicyBudgetHooks()
    done = threading.Event()
    try:
        first = pool.session()
        db.set_tenant(first, tid)
        hooks.preflight(first, scope)  # takes the quota's lock; the transaction stays open

        def second() -> None:
            with pool.session() as s2:
                db.set_tenant(s2, tid)
                hooks.preflight(s2, scope)
                s2.commit()
            done.set()

        t = threading.Thread(target=second)
        t.start()
        assert not done.wait(0.8), "the second starter did not wait for the first"
        first.commit()
        assert done.wait(15), "the second starter never proceeded after the first committed"
        t.join(5)
        first.close()
    finally:
        pool.dispose()


def test_the_run_quota_denies_the_run_after_the_last_free_slot(
    client: TestClient, world: World, owner_session: Session, app_url: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    r = client.post(
        f"{A}/quotas",
        json={"scope": agent_scope(stack.agent["id"]), "metric": "runs_per_day", "limit": 1},
        headers=h,
    )
    assert r.status_code == 201, r.text
    first = start_run(client, h, stack.agent["id"], "one", key="q-1")
    second = start_run(client, h, stack.agent["id"], "two", key="q-2")
    assert [first["status"], second["status"]] == ["pending", "failed"]
    assert second["error_class"] == "budget_denied" and "quota" in second["error_message"]


# ------------------------------------------------------------------ quotas
def test_concurrent_runs_and_token_quotas_are_enforced(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    for metric, limit in (("concurrent_runs", 1), ("tokens_per_day", 1)):
        r = client.post(
            f"{A}/quotas",
            json={"scope": tenant_scope(owner_session), "metric": metric, "limit": limit},
            headers=h,
        )
        assert r.status_code == 201, r.text
    dup = client.post(
        f"{A}/quotas",
        json={"scope": tenant_scope(owner_session), "metric": "concurrent_runs", "limit": 5},
        headers=h,
    )
    assert dup.status_code == 409
    one = start_run(client, h, stack.agent["id"], "one", key="c-1")
    two = start_run(client, h, stack.agent["id"], "two", key="c-2")
    assert one["status"] == "pending" and two["error_class"] == "budget_denied"
    assert "concurrent_runs quota" in two["error_message"]
    with thread_worker(settings):  # the admitted run hits the token quota at its first call
        done = wait_run(client, h, one["id"])
    assert done["status"] == "failed" and done["error_class"] == "budget_denied"
    assert "quota" in done["error_message"]
    quotas = client.get(f"{A}/quotas", headers=h).json()["items"]
    assert {q["metric"] for q in quotas} == {"concurrent_runs", "tokens_per_day"}


# ------------------------------------------------------------------ soft budgets and alerts
def test_a_soft_budget_never_denies_but_alerts_once_per_threshold(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    b = budget(
        client, h, project_scope(owner_session), amount=10, enforcement="soft",
        alert_thresholds=[0.5, 1.0, 5.0],
    )  # fmt: skip
    for text_in, key in (("what is the weather", "s1"), ("weather again", "s2")):
        run = start_run(client, h, stack.agent["id"], text_in, key=key)
        with thread_worker(settings):
            assert wait_run(client, h, run["id"])["status"] == "completed"
    status = client.get(f"{A}/budgets/{b['id']}/status", headers=h).json()
    assert status["alerts_fired"] == [0.5, 1.0, 5.0] and status["utilization"] > 5
    alerts = _rows(owner_session, "SELECT count(*) FROM shifde.budget_alerts")[0][0]
    assert alerts == 3  # fired once each across both runs
    audited = _rows(
        owner_session,
        "SELECT count(*) FROM shifde.audit_events WHERE action = 'governance.budget_alert'",
    )[0][0]
    assert audited == 3


def test_an_unpriced_model_follows_the_tenants_unknown_cost_policy(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, priced=False)
    budget(client, h, project_scope(owner_session), metric="cost", amount=100)
    allowed = start_run(client, h, stack.agent["id"], "what is the weather", key="u-1")
    with thread_worker(settings):
        done = wait_run(client, h, allowed["id"])
    assert done["status"] == "completed"  # default allow_unmeasured
    assert events_of(owner_session, allowed["id"])[0]["cost_kind"] == "unmeasured"
    set_tenant_setting(owner_session, "demo-a", "unknown_cost_policy", "deny")
    refused = start_run(client, h, stack.agent["id"], "what is the weather", key="u-2")
    with thread_worker(settings):
        done = wait_run(client, h, refused["id"])
    assert done["status"] == "failed" and done["error_class"] == "budget_denied"
    assert "no effective rate" in done["error_message"]


def test_stale_reservations_expire_and_stop_counting(
    client: TestClient, world: World, owner_session: Session, app_url: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    tid = tenant_id(owner_session, "demo-a")
    b = budget(client, h, tenant_scope(owner_session), amount=100)
    run = start_run(client, h, stack.agent["id"], "go")
    owner_session.execute(
        text(
            "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, budget_ids, "
            "amount, status, expires_at) VALUES (gen_random_uuid(), :t, :r, 'stale', "
            "ARRAY[CAST(:b AS uuid)], 90, 'held', :exp)"
        ),
        {"t": tid, "r": run["id"], "b": b["id"], "exp": utcnow() - timedelta(minutes=1)},
    )
    owner_session.commit()
    status = client.get(f"{A}/budgets/{b['id']}/status", headers=h).json()
    assert status["reserved"] == 0 and status["stale_holds"] == 1  # not counted, but visible
    row = owner_session.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": run["id"]}
    ).one()
    pool = Database(app_url, pool_size=1)
    try:
        with pool.session() as s:  # the next reservation fits and flips the stale hold to expired
            db.set_tenant(s, tid)
            scope = BudgetScope(tid, row[0], row[1], None, UUID(run["id"]))
            res = PolicyBudgetHooks().reserve(s, scope, UUID(run["id"]), "new", 60)
            s.commit()
            assert res.id is not None
    finally:
        pool.dispose()
    states = {
        r[0]: r[1]
        for r in _rows(owner_session, "SELECT step_key, status FROM shifde.budget_reservations")
    }
    assert states == {"stale": "expired", "new": "held"}


def test_a_budget_can_be_disabled_and_then_no_longer_blocks(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    b = budget(client, h, tenant_scope(owner_session), amount=0)
    assert start_run(client, h, stack.agent["id"], "x", key="d-1")["error_class"] == "budget_denied"
    off = client.patch(f"{A}/budgets/{b['id']}", json={"status": "disabled"}, headers=h)
    assert off.status_code == 200 and off.json()["status"] == "disabled"
    assert start_run(client, h, stack.agent["id"], "x", key="d-2")["status"] == "pending"
