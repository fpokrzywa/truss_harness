"""P8 review round 1 tests that need runs, direct invokes or the app, moved from SETH 3ba0e06
`tests/test_p8_review_r1.py`; assertions unchanged, imports rewritten to `tests._support.*`.

Not here (moved earlier, same names and assertions):
`test_patching_the_amount_of_a_legacy_run_count_budget_keeps_it_a_run_budget`,
`test_the_usage_export_filters_in_sql_and_flags_truncation`,
`test_a_project_slug_that_exists_in_several_workspaces_needs_the_workspace` and
`test_rates_and_amounts_are_bounded_to_the_column_precision_and_reject_nan` are in
`tests/governance/test_p8_budgets_usage.py` (B-1813);
`test_only_the_guard_may_close_a_version_and_a_version_belongs_to_its_cards_tenant` and
`test_csv_cells_neutralise_formulas_but_keep_numbers_numeric` are in
`tests/governance/test_p8_pricing.py` (B-1805).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.app import create_app
from shifde.core import db
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.observability import metrics, tracing
from shifde.runtime.governance import BudgetDenied, BudgetScope, PolicyBudgetHooks
from tests._support.fullapp import ORIGIN, World
from tests._support.p2util import login, tenant_id
from tests._support.p3util import ADMIN, A
from tests._support.p4util import set_tenant_setting, tenant_scope
from tests._support.p5util import start_run, thread_worker, wait_run
from tests._support.p8util import budget, events_of, priced_stack, rate_version


@pytest.fixture(autouse=True)
def clean_telemetry() -> Iterator[None]:
    tracing.tracer.configure("none")
    metrics.registry.reset()
    yield
    tracing.tracer.configure("none")


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


MESSAGES = {"messages": [{"role": "user", "content": "what is the weather"}]}


# ------------------------------------------------------------------ 1: settle between the reads
def test_a_settle_committing_between_the_used_and_held_reads_is_never_missed(
    client: TestClient,
    world: World,
    owner_session: Session,
    app_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Used and held were read by two statements (two snapshots); a settle committing in between
    was counted by neither and a hard limit was overspent. A settle is injected right after the
    ledger read: the hold it settles must still be counted."""
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    tid = tenant_id(owner_session, "demo-a")
    budget(client, h, tenant_scope(owner_session), amount=100)
    pair = [
        UUID(start_run(client, h, stack.agent["id"], f"go {i}", key=f"settle-{i}")["id"])
        for i in range(2)
    ]
    ws, pj = owner_session.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": pair[0]}
    ).one()
    agent = UUID(stack.agent["id"])
    pool = Database(app_url, pool_size=4)
    hooks = PolicyBudgetHooks()
    try:
        with pool.session() as s1:
            db.set_tenant(s1, tid)
            hold = hooks.reserve(s1, BudgetScope(tid, ws, pj, agent, pair[0]), pair[0], "a", 60)
            s1.commit()
        with pool.session() as checker, pool.session() as settler:
            db.set_tenant(checker, tid)
            db.set_tenant(settler, tid)
            fired: list[bool] = []
            real_execute = checker.execute

            def spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = real_execute(statement, *args, **kwargs)
                if not fired and "usage_events" in str(statement):
                    fired.append(True)
                    hooks.settle(settler, hold, 60)  # the hold becomes settled usage ...
                    ledger.record(
                        settler,
                        {
                            "tenant_id": tid,
                            "event_key": "settled-60",
                            "event_type": "model_call",
                            "input_tokens": 60,
                            "output_tokens": 0,  # setup only (B-602)
                        },
                    )
                    settler.commit()  # ... and commits between the checker's reads
                return result

            monkeypatch.setattr(checker, "execute", spy)
            with pytest.raises(BudgetDenied):
                hooks.reserve(checker, BudgetScope(tid, ws, pj, agent, pair[1]), pair[1], "b", 60)
            assert fired, "the interleaved settle never ran"
    finally:
        pool.dispose()


# ------------------------------------------------------------------ 2: direct invokes
def test_a_direct_model_invoke_and_a_prompt_test_are_checked_against_budgets(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    url = f"{A}/models/{stack.model}/invoke"
    ok = client.post(url, json=MESSAGES, headers=h)
    assert ok.status_code == 200, ok.text
    events = _rows(owner_session, "SELECT count(*) FROM shifde.usage_events")[0][0]
    version = client.get(f"{A}/prompts/{stack.prompt['id']}/versions", headers=h).json()[0]["id"]
    b = budget(client, h, tenant_scope(owner_session), amount=0)  # exhausted tenant budget
    for denied in (
        client.post(url, json=MESSAGES, headers=h),
        client.post(
            f"{A}/prompts/{stack.prompt['id']}/test",
            json={"version_id": version, "variables": {"message": "hi"}, "model_id": stack.model},
            headers=h,
        ),
    ):
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "budget_denied" and "budget" in denied.json()["detail"]
    # nothing reached the ledger; both refusals are recorded decisions and audited
    assert _rows(owner_session, "SELECT count(*) FROM shifde.usage_events")[0][0] == events
    decisions = _rows(
        owner_session,
        "SELECT reason_codes, policy_ids FROM shifde.policy_decisions "
        "WHERE decision = 'deny' AND run_id IS NULL AND action = 'model:invoke'",
    )
    assert len(decisions) == 2
    assert all(d[0] == ["budget_denied"] and [str(i) for i in d[1]] == [b["id"]] for d in decisions)
    audited = _rows(
        owner_session,
        "SELECT count(*) FROM shifde.audit_events WHERE action = 'model.invoked' "
        "AND outcome = 'denied' AND details->>'reason' = 'budget_denied'",
    )
    assert audited[0][0] == 2
    client.patch(f"{A}/budgets/{b['id']}", json={"status": "disabled"}, headers=h)
    assert client.post(url, json=MESSAGES, headers=h).status_code == 200  # no longer blocked


def test_a_direct_invoke_honours_the_unknown_cost_policy_and_counts_settled_usage(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, priced=False)  # no rate card: price unknown
    url = f"{A}/models/{stack.model}/invoke"
    budget(client, h, tenant_scope(owner_session), metric="cost", amount=1000)
    assert client.post(url, json=MESSAGES, headers=h).status_code == 200  # allow_unmeasured
    set_tenant_setting(owner_session, "demo-a", "unknown_cost_policy", "deny")
    denied = client.post(url, json=MESSAGES, headers=h)
    assert denied.status_code == 403 and "unknown_cost_policy" in denied.json()["detail"]
    set_tenant_setting(owner_session, "demo-a", "unknown_cost_policy", "allow_unmeasured")
    used = int(
        _rows(owner_session, "SELECT sum(input_tokens + output_tokens) FROM shifde.usage_events")[
            0
        ][0]
    )
    tok = budget(client, h, tenant_scope(owner_session), amount=used + 5)
    assert client.post(url, json=MESSAGES, headers=h).status_code == 403  # settled usage counts
    assert client.get(f"{A}/budgets/{tok['id']}/status", headers=h).json()["settled"] == used


# ------------------------------------------------------------------ 4 + 10: rate card versions
def _future(days: int) -> str:
    from datetime import timedelta

    from shifde.core.clock import utcnow

    return (utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_new_version_supersedes_an_open_ended_one_and_backdating_is_still_refused(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)  # chat-a: one open-ended version since 2000
    cards = client.get(f"{A}/rate-cards", headers=h).json()["items"]
    (card,) = [c for c in cards if c["model_key"] == "chat-a"]
    run1 = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        assert wait_run(client, h, run1["id"])["status"] == "completed"
    e1 = events_of(owner_session, run1["id"])[0]
    # a price change: a version starting later than the open-ended one closes it at that moment
    # (round 2: it must start now or later, never in the past)
    v2_from = _future(30)
    v2 = rate_version(
        client,
        h,
        card["id"],
        effective_from=v2_from,
        input_rate=5.0,
        output_rate=6.0,
    )
    assert v2["version_no"] == 2 and v2["effective_to"] is None
    versions = client.get(f"{A}/rate-cards/{card['id']}/versions", headers=h).json()["items"]
    v1 = next(v for v in versions if v["version_no"] == 1)
    assert v1["effective_to"].startswith(v2_from[:19])
    q = "provider_key=fake&model_key=chat-a"
    old = client.get(f"{A}/rate-cards/resolve?{q}&at=2010-01-01T00:00:00Z", headers=h).json()
    new = client.get(f"{A}/rate-cards/resolve?{q}&at={_future(31)}", headers=h).json()
    assert (old["version"]["version_no"], new["version"]["version_no"]) == (1, 2)
    # a third supersedes the second the same way
    v3 = rate_version(client, h, card["id"], effective_from=_future(60), input_rate=7.0)
    assert v3["version_no"] == 3
    # true overlaps are still refused: backdated before the open-ended version or inside history
    for start in ("2024-01-01T00:00:00Z", "2023-06-01T00:00:00Z", "1999-01-01T00:00:00Z"):
        clash = rate_version(client, h, card["id"], effective_from=start, status=409)
        assert "overlaps" in clash["detail"]
    bounded = rate_version(
        client,
        h,
        card["id"],
        effective_from="2010-01-01T00:00:00Z",
        effective_to="2012-01-01T00:00:00Z",
        status=409,
    )
    assert "overlaps" in bounded["detail"]
    # events already written are never repriced; new ones use the version in force
    again = events_of(owner_session, run1["id"])[0]
    assert (again["cost_amount"], again["rate_snapshot"]) == (
        e1["cost_amount"],
        e1["rate_snapshot"],
    )
    run2 = start_run(client, h, stack.agent["id"], "what is the weather today")
    with thread_worker(settings):
        assert wait_run(client, h, run2["id"])["status"] == "completed"
    e2 = events_of(owner_session, run2["id"])[0]
    assert e2["rate_snapshot"]["version_no"] == 1  # the superseding versions are not in force yet


# ------------------------------------------------------------------ 6: metrics exposure
def _metrics_client(settings: Settings, host: str, **update: Any) -> TestClient:
    s = settings.model_copy(update={"metrics_token": None, **update})
    return TestClient(
        create_app(s),
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        client=(host, 5000),
    )


def test_metrics_refuse_proxied_loopback_requests_and_production_without_a_token(
    world: World, settings: Settings
) -> None:
    with _metrics_client(settings, "127.0.0.1") as local:
        assert local.get("/api/metrics").status_code == 200
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
            proxied = local.get("/api/metrics", headers={header: "203.0.113.9"})
            assert proxied.status_code == 403, header
            assert "proxy" in proxied.text
    with _metrics_client(settings, "127.0.0.1", env="production") as prod:
        refused = prod.get("/api/metrics")
        assert refused.status_code == 403 and "production" in refused.text
    from pydantic import SecretStr

    with _metrics_client(
        settings, "203.0.113.9", env="production", metrics_token=SecretStr("metrics-token-123456")
    ) as prod_token:
        # a configured token works from anywhere, even through a proxy
        ok = prod_token.get(
            "/api/metrics",
            headers={"Authorization": "Bearer metrics-token-123456", "X-Forwarded-For": "1.2.3.4"},
        )
        assert ok.status_code == 200


# ------------------------------------------------------------------ 11: tracing endpoint display
def test_the_tracing_status_never_echoes_credentials_or_the_query_of_the_endpoint() -> None:
    tracing.tracer.configure("otlp", endpoint="http://user:s3cret@127.0.0.1:9/base?token=abc#frag")
    otlp = tracing.tracer.status()["otlp"]
    assert otlp["endpoint"] == "http://127.0.0.1:9/base/v1/traces"
    assert "s3cret" not in str(tracing.tracer.status()) and "abc" not in str(
        tracing.tracer.status()
    )
    assert tracing._safe_url("https://[::1]:4318/x?q=1") == "https://[::1]:4318/x"
    assert tracing._safe_url("http://h:notaport/x") == "(invalid endpoint)"
