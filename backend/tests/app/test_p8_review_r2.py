"""P8 review round 2 tests that need the app, moved from SETH 3ba0e06
`tests/test_p8_review_r2.py`; assertions unchanged, imports rewritten to `tests._support.*`.

Not here (moved earlier, same names and assertions):
`test_the_audit_export_flags_truncation_in_csv_and_ndjson`,
`test_an_audit_export_exactly_at_the_cap_is_not_flagged_and_a_policy_cap_applies`,
`test_a_soft_budget_reports_alert_only_and_a_hard_one_deny` and
`test_a_legacy_run_budget_can_be_patched_with_its_echoed_runs_metric` are in
`tests/governance/test_p8_budgets_usage.py` (B-1813);
`test_a_version_that_would_close_an_open_ended_one_in_the_past_is_refused` is in
`tests/governance/test_p8_pricing.py` (B-1805).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.settings import Settings
from shifde.ledger import budgeting
from tests._support.fullapp import ORIGIN, World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A
from tests._support.p4util import tenant_scope
from tests._support.p8util import agent_scope, budget, priced_stack


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


# ------------------------------------------------------------------ 6: agent tests carry the agent
def test_an_agent_test_is_checked_against_and_counted_by_the_agents_budget(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    agent_id = stack.agent["id"]
    url = f"{A}/agents/{agent_id}/test"
    body = {"version_id": stack.agent_v["id"], "variables": {}, "input": "what is the weather"}
    ok = client.post(url, json=body, headers=h)
    assert ok.status_code == 200, ok.text
    (event,) = _rows(
        owner_session,
        "SELECT agent_id, is_test, input_tokens + output_tokens FROM shifde.usage_events "
        "WHERE is_test AND event_type = 'model_call'",
    )
    assert str(event[0]) == agent_id and event[1] is True
    used = int(event[2])
    assert used > 0
    # the agent budget now sees that spend ...
    capped = budget(client, h, agent_scope(agent_id), amount=used + 1)
    status = client.get(f"{A}/budgets/{capped['id']}/status", headers=h).json()
    assert status["settled"] == used
    # ... and denies the next test of that agent
    denied = client.post(url, json=body, headers=h)
    assert denied.status_code == 403 and denied.json()["code"] == "budget_denied"
    exhausted = budget(client, h, agent_scope(agent_id), amount=0)
    assert exhausted["id"] != capped["id"]


# ------------------------------------------------------------------ 7: status reads once
def test_budget_status_reads_settled_and_reserved_in_one_statement(
    client: TestClient,
    world: World,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = login(client, world.passwords, ADMIN)
    b = budget(client, h, tenant_scope(owner_session), amount=1000)
    calls: list[str] = []
    real = budgeting.used_and_held

    def spy(*a: Any, **k: Any) -> Any:
        calls.append("used_and_held")
        return real(*a, **k)

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("two separate snapshots")

    monkeypatch.setattr(budgeting, "used_and_held", spy)
    monkeypatch.setattr(budgeting, "used_amount", forbidden)
    monkeypatch.setattr(budgeting, "held_amount", forbidden)
    status = client.get(f"{A}/budgets/{b['id']}/status", headers=h)
    assert status.status_code == 200, status.text
    assert calls == ["used_and_held"]


# ------------------------------------------------------------------ 3: web proxy header
def _metrics_client(settings: Settings, host: str, **update: Any) -> TestClient:
    s = settings.model_copy(update={"metrics_token": None, **update})
    return TestClient(
        create_app(s),
        base_url="https://testserver",
        headers={"Origin": ORIGIN},
        client=(host, 5000),
    )


def test_metrics_refuse_tokenless_requests_that_came_through_the_web_proxy(
    world: World, settings: Settings
) -> None:
    from pydantic import SecretStr

    with _metrics_client(settings, "127.0.0.1") as local:
        assert local.get("/api/metrics").status_code == 200
        via = local.get("/api/metrics", headers={"X-Shifde-Via": "web"})
        assert via.status_code == 403 and "proxy" in via.text
    # even with trusted proxies configured the web proxy marker is refused without a token
    with _metrics_client(settings, "127.0.0.1", trusted_proxies=["127.0.0.1"]) as trusted:
        assert trusted.get("/api/metrics", headers={"X-Shifde-Via": "web"}).status_code == 403
    with _metrics_client(settings, "203.0.113.9", metrics_token=SecretStr("tok-123456789")) as c:
        ok = c.get(
            "/api/metrics", headers={"Authorization": "Bearer tok-123456789", "X-Shifde-Via": "web"}
        )
        assert ok.status_code == 200  # the token path keeps working
