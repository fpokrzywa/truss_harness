"""Budgets, usage export and audit export through the API, moved from SETH 3ba0e06
(`test_p8_budgets.py`, `test_p8_review_r1.py`, `test_p8_review_r2.py`); assertions unchanged.

Setup changes only: SETH `login` / `user_session` become `as_user` on the tenancy route-test
people, `limited_user` the connections helper, `tenant_scope` / `project_scope` / `budget` the
helpers in `tests.governance.routes`, workspaces and projects come from the tenancy world or are
written as the schema owner, and a `data_export` policy (policy routes are part 3) is inserted as
the schema owner. `GET /audit-events?category=governance` (an audit route, not in this router) is
answered by `audit.list_events` with the same category on the schema owner's session.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.audit import service as audit
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.governance import service as gov_service
from tests.connections.routes import as_user, limited_user, org
from tests.governance.routes import (
    CURRENCY,
    budget,
    project_scope,
    rate_card,
    rate_version,
    tenant_scope,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


# ------------------------------------------------------------------ budgets (SETH p8_budgets)
def test_budget_administration_validates_audits_and_uses_optimistic_locking(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    scope = tenant_scope(world.a)
    assert budget(
        client, h, base, scope, metric="cost", amount=5, currency=None, status=422
    )  # no currency
    assert (
        client.post(
            f"{base}/budgets",
            json={"scope": scope, "period": "daily", "amount": 1.5, "metric": "tokens"},
            headers=h,
        ).status_code
        == 422
    )  # fractional tokens
    assert (
        client.post(
            f"{base}/budgets",
            json={"scope": scope, "period": "daily", "amount": 1, "metric": "tokens", "bogus": 1},
            headers=h,
        ).status_code
        == 422
    )
    b = budget(client, h, base, scope, metric="cost", amount=5, on_exceed="queue_until_reset")
    assert b["on_exceed"] == "queue_until_reset" and b["on_exceed_effective"] == "deny"
    assert b["currency"] == CURRENCY and b["alert_thresholds"] == [0.5, 0.8, 1.0]
    ok = client.patch(
        f"{base}/budgets/{b['id']}",
        json={"amount": 9, "row_version": b["row_version"]},
        headers=h,
    )
    assert ok.status_code == 200 and ok.json()["amount"] == 9 and ok.json()["row_version"] == 2
    stale = client.patch(
        f"{base}/budgets/{b['id']}",
        json={"amount": 10, "row_version": b["row_version"]},
        headers=h,
    )
    assert stale.status_code == 409
    assert client.patch(f"{base}/budgets/{b['id']}", json={}, headers=h).status_code == 422
    actions = [
        r[0]
        for r in _rows(
            owner_session,
            "SELECT action FROM shifde.audit_events WHERE tenant_id = :t ORDER BY occurred_at",
            t=world.a.tenant,
        )
    ]
    assert "governance.budget.created" in actions and "governance.budget.updated" in actions
    gov = audit.list_events(
        owner_session,
        tenant_id=world.a.tenant,
        category="governance",
        actor=None,
        action=None,
        occurred_from=None,
        occurred_to=None,
        before=None,
        limit=200,
    )
    assert gov and all(e.action.startswith("governance.") for e in gov)
    listed = client.get(f"{base}/budgets", headers=h).json()["items"]
    assert [x["id"] for x in listed] == [b["id"]]


def test_budget_admin_requires_the_budget_permissions_and_is_tenant_scoped(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    base_a, base_b = org(world.a.slug), org(world.b.slug)
    h = as_user(client, people.owner_a)
    scope = tenant_scope(world.a)
    b = budget(client, h, base_a, scope, amount=50)
    quota = client.post(
        f"{base_a}/quotas",
        json={"scope": scope, "metric": "runs_per_day", "limit": 3},
        headers=h,
    ).json()
    # a viewer reads usage but holds no budget permission
    vh = as_user(client, people.viewer_a)
    assert client.get(f"{base_a}/budgets", headers=vh).status_code == 403
    assert client.get(f"{base_a}/budgets/{b['id']}/status", headers=vh).status_code == 403
    assert (
        client.post(
            f"{base_a}/budgets",
            json={"scope": scope, "period": "daily", "amount": 1, "metric": "tokens"},
            headers=vh,
        ).status_code
        == 403
    )
    # budget:read alone can look but not change
    email, _ = limited_user(owner_db, world.a.tenant, ["budget:read"])
    rh = as_user(client, email)
    assert client.get(f"{base_a}/budgets", headers=rh).status_code == 200
    assert client.get(f"{base_a}/quotas/{quota['id']}", headers=rh).status_code == 200
    assert (
        client.patch(f"{base_a}/budgets/{b['id']}", json={"amount": 1}, headers=rh).status_code
        == 403
    )
    assert (
        client.patch(f"{base_a}/quotas/{quota['id']}", json={"limit": 1}, headers=rh).status_code
        == 403
    )
    # another tenant sees nothing of it and cannot target this tenant's scopes
    bh = as_user(client, people.owner_b)
    assert client.get(f"{base_b}/budgets", headers=bh).json()["items"] == []
    assert client.get(f"{base_b}/budgets/{b['id']}", headers=bh).status_code == 404
    assert (
        client.patch(f"{base_b}/budgets/{b['id']}", json={"amount": 1}, headers=bh).status_code
        == 404
    )
    assert client.get(f"{base_a}/budgets", headers=bh).status_code == 404  # not a member of A
    foreign = project_scope(world.a)  # a project of A
    for body in (
        {"scope": foreign, "period": "daily", "amount": 1, "metric": "tokens"},
        {"scope": tenant_scope(world.a), "period": "daily", "amount": 1, "metric": "tokens"},
    ):
        assert client.post(f"{base_b}/budgets", json=body, headers=bh).status_code == 404
    still = client.get(f"{base_a}/budgets", headers=as_user(client, people.owner_a)).json()["items"]
    assert [x["amount"] for x in still] == [50]


def test_a_client_may_echo_the_whole_budget_back_but_not_change_its_scope_or_metric(
    client: TestClient, world: World, people: People
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    scope = tenant_scope(world.a)
    b = budget(client, h, base, scope, amount=50)  # a token budget
    echo = {
        "scope": scope, "metric": "tokens", "period": "monthly", "amount": 60,
        "currency": "USD", "enforcement": "soft", "on_exceed": "deny",
        "alert_thresholds": [0.9], "status": "active", "row_version": b["row_version"],
    }  # fmt: skip
    ok = client.patch(f"{base}/budgets/{b['id']}", json=echo, headers=h)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert (body["period"], body["amount"], body["enforcement"]) == ("monthly", 60, "soft")
    assert body["alert_thresholds"] == [0.9] and body["currency"] is None  # tokens have no currency
    other = project_scope(world.a)
    for change in ({"scope": other}, {"metric": "cost"}):
        bad = client.patch(
            f"{base}/budgets/{b['id']}", json={**echo, **change, "row_version": 2}, headers=h
        )
        assert bad.status_code == 422 and "cannot be changed" in bad.text


# ------------------------------------------------------------------ SETH p8_review_r1 / r2
def _legacy_run_budget(owner: Session, tenant: UUID) -> UUID:
    bid = uuid7()
    owner.execute(  # a P5 row: only limit_runs, metric left at its default
        text(
            "INSERT INTO shifde.budgets (id, tenant_id, scope_type, scope_id, period, limit_runs) "
            "VALUES (:i, :t, 'tenant', :t, 'day', 5)"
        ),
        {"i": bid, "t": tenant},
    )
    owner.commit()
    return bid


def test_patching_the_amount_of_a_legacy_run_count_budget_keeps_it_a_run_budget(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    bid = _legacy_run_budget(owner_session, world.a.tenant)
    shown = client.get(f"{base}/budgets", headers=h).json()["items"]
    (legacy,) = [b for b in shown if b["id"] == str(bid)]
    assert (legacy["metric"], legacy["amount"]) == ("runs", 5)
    resp = client.patch(
        f"{base}/budgets/{bid}",
        json={"amount": 7, "row_version": legacy["row_version"]},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["metric"], resp.json()["amount"]) == ("runs", 7)
    row = _rows(
        owner_session,
        "SELECT limit_runs, limit_tokens, amount FROM shifde.budgets WHERE id = :i",
        i=bid,
    )[0]
    assert tuple(row) == (7, None, None)  # still a run budget, not a token budget of 7 tokens
    bad = client.patch(f"{base}/budgets/{bid}", json={"amount": 7.5}, headers=h)
    assert bad.status_code == 422 and "whole number" in bad.text


def test_a_legacy_run_budget_can_be_patched_with_its_echoed_runs_metric(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    bid = _legacy_run_budget(owner_session, world.a.tenant)
    (legacy,) = [
        b for b in client.get(f"{base}/budgets", headers=h).json()["items"] if b["id"] == str(bid)
    ]
    assert legacy["metric"] == "runs"
    echoed = {**legacy, "amount": 9}
    patch = {k: echoed[k] for k in ("scope", "metric", "amount", "name", "status", "row_version")}
    resp = client.patch(f"{base}/budgets/{bid}", json=patch, headers=h)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["metric"], resp.json()["amount"]) == ("runs", 9)
    # a real token budget still refuses a different (or the legacy) metric
    tok = budget(client, h, base, tenant_scope(world.a))
    bad = client.patch(f"{base}/budgets/{tok['id']}", json={"metric": "runs"}, headers=h)
    assert bad.status_code == 422 and "metric" in bad.text


def test_a_soft_budget_reports_alert_only_and_a_hard_one_deny(
    client: TestClient, world: World, people: People
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    scope = tenant_scope(world.a)
    soft = budget(client, h, base, scope, enforcement="soft", amount=100, period="monthly")
    hard = budget(client, h, base, scope, enforcement="hard", amount=100, period="daily")
    assert soft["on_exceed_effective"] == "alert_only"
    assert hard["on_exceed_effective"] == "deny"
    listed = {b["id"]: b for b in client.get(f"{base}/budgets", headers=h).json()["items"]}
    assert listed[soft["id"]]["on_exceed_effective"] == "alert_only"
    assert listed[hard["id"]]["on_exceed_effective"] == "deny"
    status = client.get(f"{base}/budgets/{soft['id']}/status", headers=h).json()
    assert status["on_exceed_effective"] == "alert_only"
    assert (
        client.get(f"{base}/budgets/{hard['id']}/status", headers=h).json()["on_exceed_effective"]
        == "deny"
    )
    # switching enforcement changes what is reported
    patched = client.patch(
        f"{base}/budgets/{soft['id']}",
        json={"enforcement": "hard", "row_version": soft["row_version"]},
        headers=h,
    )
    assert patched.status_code == 200 and patched.json()["on_exceed_effective"] == "deny"


def _export(client: TestClient, base: str, h: dict[str, str], query: str = "") -> Any:
    return client.get(f"{base}/usage/export?{query}", headers=h)


def _event(owner: Session, tenant: UUID, key: str, workspace: UUID, hours_ago: int) -> None:
    ledger.record(
        owner,
        {
            "tenant_id": tenant,
            "event_key": key,
            "event_type": "tool_call",
            "workspace_id": workspace,
            "occurred_at": utcnow() - timedelta(hours=hours_ago),
        },
    )


def test_the_usage_export_filters_in_sql_and_flags_truncation(
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    main, other = world.a.ws, world.a.ws2
    for i in range(6):  # older events of the main workspace come first in the export order
        _event(owner_session, world.a.tenant, f"m-{i}", main, 5)
    for i in range(4):
        _event(owner_session, world.a.tenant, f"o-{i}", other, 1)
    owner_session.commit()
    monkeypatch.setattr(gov_service, "EXPORT_ROW_LIMIT", 3)
    ws = f"workspace={other}"
    cut = _export(client, base, h, ws)
    assert cut.status_code == 200, cut.text
    lines = list(csv.reader(io.StringIO(cut.text)))
    data = [r for r in lines[1:] if not r[0].startswith("#")]
    assert len(data) == 3 and {r[5] for r in data} == {str(other)}  # filtered BEFORE the cap
    assert cut.headers["x-export-truncated"] == "true"
    assert lines[-1][0].startswith("# TRUNCATED") and "3 rows" in lines[-1][0]
    audited = _rows(
        owner_session,
        "SELECT details FROM shifde.audit_events WHERE action = 'governance.usage.exported'",
    )
    assert audited[-1][0]["truncated"] is True and audited[-1][0]["rows"] == 3
    monkeypatch.setattr(gov_service, "EXPORT_ROW_LIMIT", 4)  # exactly the 4 matching rows: no cut
    whole = _export(client, base, h, ws)
    assert "x-export-truncated" not in whole.headers and "TRUNCATED" not in whole.text
    assert len(list(csv.reader(io.StringIO(whole.text)))) == 1 + 4
    # an unknown cost kind is a client error, a known one filters in SQL as well
    assert _export(client, base, h, "cost_kind=bogus").status_code == 422
    none = _export(client, base, h, f"{ws}&cost_kind=calculated")
    assert none.status_code == 200 and len(none.text.strip().splitlines()) == 1  # header only


def test_a_project_slug_that_exists_in_several_workspaces_needs_the_workspace(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    unique = project_scope(world.a)["id"]  # an id is never ambiguous
    slug = owner_session.execute(
        text("SELECT slug FROM shifde.projects WHERE id = :p"), {"p": world.a.project}
    ).scalar_one()
    owner_session.execute(
        text(
            "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
            "VALUES (:i, :t, :w, :s, 'Twin')"
        ),
        {"i": uuid7(), "t": world.a.tenant, "w": world.a.ws2, "s": slug},
    )
    owner_session.commit()
    summary = f"{base}/usage/summary?group_by=model"
    ambiguous = client.get(f"{summary}&project={slug}", headers=h)
    assert ambiguous.status_code == 422 and "workspace" in ambiguous.text
    for ws in (world.a.ws, world.a.ws2):
        assert client.get(f"{summary}&project={slug}&workspace={ws}", headers=h).status_code == 200
    assert _export(client, base, h, f"project={slug}").status_code == 422
    assert client.get(f"{summary}&project={unique}", headers=h).status_code == 200


def test_rates_and_amounts_are_bounded_to_the_column_precision_and_reject_nan(
    client: TestClient, world: World, people: People
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    scope = tenant_scope(world.a)
    for amount in (1e30, 1e16):
        huge = client.post(
            f"{base}/budgets",
            json={"scope": scope, "period": "daily", "metric": "tokens", "amount": amount},
            headers=h,
        )
        assert huge.status_code == 422, (amount, huge.text)
    nan = client.post(
        f"{base}/budgets",
        content=f'{{"scope": {{"type": "tenant", "id": "{scope["id"]}"}}, '
        '"period": "daily", "metric": "tokens", "amount": NaN}',
        headers={**h, "Content-Type": "application/json"},
    )
    assert nan.status_code == 422
    b = budget(client, h, base, scope, amount=50)
    assert (
        client.patch(f"{base}/budgets/{b['id']}", json={"amount": 1e30}, headers=h).status_code
        == 422
    )
    assert budget(client, h, base, project_scope(world.a), amount=1e15)["amount"] == 1e15
    card = rate_card(client, h, base, "m-bounds", "fake")
    for field in ("input_rate", "output_rate", "cached_read_rate", "cached_write_rate"):
        bad = rate_version(client, h, base, card["id"], status=422, **{field: 1e20})
        assert "detail" in bad or "errors" in bad
    assert rate_version(client, h, base, card["id"], input_rate=1e8)["input_rate"] == 1e8
    other = rate_card(client, h, base, "m-bounds-2", "fake")
    odd = rate_version(client, h, base, other["id"], status=422, other_rates={"x": 1e30})
    assert odd


# ------------------------------------------------------------------ audit export (r2)
def _generate_audit_events(client: TestClient, email: str) -> None:
    for _ in range(4):
        as_user(client, email)


def test_the_audit_export_flags_truncation_in_csv_and_ndjson(
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = org(world.a.slug)
    _generate_audit_events(client, people.owner_a)
    h = as_user(client, people.owner_a)  # the last login owns the session cookie / CSRF
    monkeypatch.setattr(gov_service, "EXPORT_ROW_LIMIT", 3)
    cut = client.get(f"{base}/audit-events/export?format=csv", headers=h)
    assert cut.status_code == 200, cut.text
    assert cut.headers["x-export-truncated"] == "true"
    lines = list(csv.reader(io.StringIO(cut.text)))
    data = [r for r in lines[1:] if not r[0].startswith("#")]
    assert len(data) == 3
    assert lines[-1][0].startswith("# TRUNCATED") and "3 rows" in lines[-1][0]

    nd = client.get(f"{base}/audit-events/export?format=json", headers=h)
    assert nd.headers["x-export-truncated"] == "true"
    objs = [json.loads(line) for line in nd.text.splitlines()]
    assert objs[-1] == {"truncated": True, "row_cap": 3}
    assert len(objs) == 4 and all("id" in o for o in objs[:-1])

    audited = _rows(
        owner_session,
        "SELECT details FROM shifde.audit_events WHERE action = 'audit.exported' "
        "AND tenant_id = :t ORDER BY chain_seq",
        t=world.a.tenant,
    )
    assert audited[-1][0]["truncated"] is True and audited[-1][0]["row_cap"] == 3
    assert audited[-1][0]["rows"] == 3


def test_an_audit_export_exactly_at_the_cap_is_not_flagged_and_a_policy_cap_applies(
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = org(world.a.slug)
    _generate_audit_events(client, people.owner_a)
    h = as_user(client, people.owner_a)  # the last login owns the session cookie / CSRF
    full = client.get(f"{base}/audit-events/export?format=json", headers=h)
    assert "x-export-truncated" not in full.headers
    total = len(full.text.splitlines())
    assert total >= 4
    # Exactly at the cap: the previous export added one audit event, so the next export holds
    # exactly total + 1 rows; with the cap at that number nothing is cut and nothing is flagged.
    monkeypatch.setattr(gov_service, "EXPORT_ROW_LIMIT", total + 1)
    exact = client.get(f"{base}/audit-events/export?format=json", headers=h)
    assert "x-export-truncated" not in exact.headers
    exact_objs = [json.loads(line) for line in exact.text.splitlines()]
    assert len(exact_objs) == total + 1 and all("id" in o for o in exact_objs)
    # one row over the cap is flagged
    monkeypatch.setattr(gov_service, "EXPORT_ROW_LIMIT", total + 1)
    over = client.get(f"{base}/audit-events/export?format=json", headers=h)
    assert over.headers["x-export-truncated"] == "true"
    monkeypatch.undo()
    # a data_export policy caps the rows (the explicit max_rows of the policy)
    owner_session.execute(
        text(
            "INSERT INTO shifde.governance_policies (id, tenant_id, kind, name, definition) "
            "VALUES (:i, :t, 'data_export', 'cap', CAST(:d AS jsonb))"
        ),
        {"i": uuid7(), "t": world.a.tenant, "d": json.dumps({"max_rows": 2})},
    )
    owner_session.commit()
    cut = client.get(f"{base}/audit-events/export?format=json", headers=h)
    assert cut.headers["x-export-truncated"] == "true"
    assert len(cut.text.splitlines()) == 3  # 2 rows + the marker object
