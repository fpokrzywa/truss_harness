"""governance routes (part 1: rate cards), client A against client B (plan recipe step 5, B-1801).

Every route of `governance.api.router` has a test here (`ROUTE_TESTS`, matched against the
router): the caller first gets their own object (200 / 201), then asks for B's object under A's
organization and gets exactly the answer an id that does not exist gets (404, same body), and finds
B's rows absent from A's lists. B's slug in the path gives A the unknown-organization 404. The
platform price-list routes are the platform administrator's: a client user is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.governance import api as governance_api
from tests.connections.routes import as_user, org
from tests.governance.routes import (
    PLATFORM,
    budget,
    project_scope,
    rate_card,
    rate_version,
    tenant_scope,
    workspace_scope,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

_VARYING = {"instance", "request_id"}


def _same(r: httpx.Response, missing: httpx.Response, status: int = 404) -> None:
    """Refused exactly like an id or slug that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == status, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    base: str
    email: str
    card: str
    version: str


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side
    platform: str
    global_card: str


def _side(client: TestClient, slug: str, email: str, model_key: str) -> Side:
    base = org(slug)
    h = as_user(client, email)
    card = rate_card(client, h, base, model_key, "fake")
    version = rate_version(client, h, base, card["id"])
    return Side(base, email, card["id"], version["id"])


@pytest.fixture
def two(client: TestClient, world: World, people: People) -> Two:
    b = _side(client, world.b.slug, people.owner_b, "m-b")
    a = _side(client, world.a.slug, people.owner_a, "m-a")
    h = as_user(client, people.platform)
    r = client.post(
        f"{PLATFORM}/rate-cards",
        json={"provider_key": "fake", "model_key": "m-global", "name": "global"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return Two(a, b, people.platform, r.json()["id"])


# ------------------------------------------------------------------ route table
ROUTE_TESTS = {
    ("GET", "/api/v1/orgs/{org}/rate-cards"): "test_a_lists_only_its_own_and_the_global_cards",
    ("POST", "/api/v1/orgs/{org}/rate-cards"): "test_a_lists_only_its_own_and_the_global_cards",
    ("GET", "/api/v1/orgs/{org}/rate-cards/resolve"): "test_a_cannot_resolve_bs_rate",
    ("GET", "/api/v1/orgs/{org}/usage/events"): ("test_usage_reads_and_exports_hold_only_as_own"),
    ("GET", "/api/v1/orgs/{org}/usage/summary"): ("test_usage_reads_and_exports_hold_only_as_own"),
    ("GET", "/api/v1/orgs/{org}/usage/runs/{run_id}"): (
        "test_usage_reads_and_exports_hold_only_as_own"
    ),
    ("POST", "/api/v1/orgs/{org}/usage/rollups/rebuild"): (
        "test_usage_reads_and_exports_hold_only_as_own"
    ),
    ("GET", "/api/v1/orgs/{org}/usage/export"): ("test_usage_reads_and_exports_hold_only_as_own"),
    ("GET", "/api/v1/orgs/{org}/audit-events/export"): (
        "test_the_audit_export_holds_only_the_callers_chain"
    ),
    ("GET", "/api/v1/orgs/{org}/budgets"): ("test_a_cannot_reach_bs_budgets_and_quotas"),
    ("POST", "/api/v1/orgs/{org}/budgets"): ("test_a_cannot_scope_a_budget_or_quota_to_bs_scopes"),
    ("GET", "/api/v1/orgs/{org}/budgets/{budget_id}"): (
        "test_a_cannot_reach_bs_budgets_and_quotas"
    ),
    ("PATCH", "/api/v1/orgs/{org}/budgets/{budget_id}"): (
        "test_a_cannot_reach_bs_budgets_and_quotas"
    ),
    ("GET", "/api/v1/orgs/{org}/budgets/{budget_id}/status"): (
        "test_a_cannot_reach_bs_budgets_and_quotas"
    ),
    ("GET", "/api/v1/orgs/{org}/quotas"): ("test_a_cannot_reach_bs_budgets_and_quotas"),
    ("POST", "/api/v1/orgs/{org}/quotas"): ("test_a_cannot_scope_a_budget_or_quota_to_bs_scopes"),
    ("GET", "/api/v1/orgs/{org}/quotas/{quota_id}"): ("test_a_cannot_reach_bs_budgets_and_quotas"),
    ("PATCH", "/api/v1/orgs/{org}/quotas/{quota_id}"): (
        "test_a_cannot_reach_bs_budgets_and_quotas"
    ),
    ("GET", "/api/v1/orgs/{org}/rate-cards/{card_id}/versions"): ("test_a_cannot_reach_bs_card"),
    ("POST", "/api/v1/orgs/{org}/rate-cards/{card_id}/versions"): "test_a_cannot_reach_bs_card",
    ("GET", "/api/v1/platform/rate-cards"): "test_a_client_cannot_use_the_platform_price_list",
    ("POST", "/api/v1/platform/rate-cards"): "test_a_client_cannot_use_the_platform_price_list",
    ("GET", "/api/v1/platform/rate-cards/{card_id}/versions"): (
        "test_a_client_cannot_use_the_platform_price_list"
    ),
    ("POST", "/api/v1/platform/rate-cards/{card_id}/versions"): (
        "test_a_client_cannot_use_the_platform_price_list"
    ),
}


def test_every_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)  # type: ignore[attr-defined]  # APIRoute.path
        for route in governance_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


# ------------------------------------------------------------------ organization
def test_bs_organization_is_unknown_to_a(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    body = {"provider_key": "fake", "model_key": "x", "name": "x"}
    version = {
        "effective_from": "2000-01-01T00:00:00Z", "currency": "XTS", "unit": "per_call",
        "input_rate": 1, "output_rate": 0, "source": "s", "revision": "r",
    }  # fmt: skip
    q = "resolve?provider_key=fake&model_key=m-b"
    for method, path, payload in (
        ("GET", "rate-cards", None),
        ("POST", "rate-cards", body),
        ("GET", f"rate-cards/{q}", None),
        ("GET", f"rate-cards/{two.b.card}/versions", None),
        ("POST", f"rate-cards/{two.b.card}/versions", version),
    ):
        r = client.request(method, f"{two.b.base}/{path}", json=payload, headers=h)
        missing = client.request(method, f"{unknown}/{path}", json=payload, headers=h)
        _same(r, missing)


# ------------------------------------------------------------------ rate cards
def test_a_lists_only_its_own_and_the_global_cards(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    own = client.post(
        f"{two.a.base}/rate-cards",
        json={"provider_key": "fake", "model_key": "m-a2", "name": "mine"},
        headers=h,
    )
    assert own.status_code == 201
    listed = client.get(f"{two.a.base}/rate-cards", headers=h)
    assert listed.status_code == 200
    ids = {c["id"] for c in listed.json()["items"]}
    assert ids == {two.a.card, own.json()["id"], two.global_card}
    assert two.b.card not in ids
    # the same model key under B's tenant is B's own card, never A's conflict
    ok = client.post(
        f"{two.a.base}/rate-cards",
        json={"provider_key": "fake", "model_key": "m-b", "name": "a's own m-b"},
        headers=h,
    )
    assert ok.status_code == 201
    hb = as_user(client, two.b.email)
    b_ids = {c["id"] for c in client.get(f"{two.b.base}/rate-cards", headers=hb).json()["items"]}
    assert b_ids == {two.b.card, two.global_card}


def test_a_cannot_reach_bs_card(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    own = client.get(f"{two.a.base}/rate-cards/{two.a.card}/versions", headers=h)
    assert own.status_code == 200 and [v["id"] for v in own.json()["items"]] == [two.a.version]
    own_post = client.post(
        f"{two.a.base}/rate-cards/{two.a.card}/versions",
        json={
            "effective_from": "2999-01-01T00:00:00Z", "currency": "XTS", "unit": "per_call",
            "input_rate": 1, "output_rate": 0, "source": "s", "revision": "r",
        },
        headers=h,
    )  # fmt: skip
    assert own_post.status_code == 201, own_post.text
    unknown = uuid7()
    version = {
        "effective_from": "2999-02-01T00:00:00Z", "currency": "XTS", "unit": "per_call",
        "input_rate": 1, "output_rate": 0, "source": "s", "revision": "r",
    }  # fmt: skip
    _same(
        client.get(f"{two.a.base}/rate-cards/{two.b.card}/versions", headers=h),
        client.get(f"{two.a.base}/rate-cards/{unknown}/versions", headers=h),
    )
    _same(
        client.post(f"{two.a.base}/rate-cards/{two.b.card}/versions", json=version, headers=h),
        client.post(f"{two.a.base}/rate-cards/{unknown}/versions", json=version, headers=h),
    )
    hb = as_user(client, two.b.email)
    b_versions = client.get(f"{two.b.base}/rate-cards/{two.b.card}/versions", headers=hb)
    assert [v["id"] for v in b_versions.json()["items"]] == [two.b.version]  # untouched


def test_a_cannot_resolve_bs_rate(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    own = client.get(f"{two.a.base}/rate-cards/resolve?provider_key=fake&model_key=m-a", headers=h)
    assert own.status_code == 200 and own.json()["card"]["id"] == two.a.card
    _same(
        client.get(f"{two.a.base}/rate-cards/resolve?provider_key=fake&model_key=m-b", headers=h),
        client.get(
            f"{two.a.base}/rate-cards/resolve?provider_key=fake&model_key=m-nobody", headers=h
        ),
    )


# ------------------------------------------------------------------ platform price list
def test_a_client_cannot_use_the_platform_price_list(client: TestClient, two: Two) -> None:
    ph = as_user(client, two.platform)
    listed = client.get(f"{PLATFORM}/rate-cards", headers=ph).json()["items"]
    assert [c["id"] for c in listed] == [two.global_card]  # no client's card
    h = as_user(client, two.a.email)
    body = {"provider_key": "fake", "model_key": "x", "name": "x"}
    version = {
        "effective_from": "2999-01-01T00:00:00Z", "currency": "XTS", "unit": "per_call",
        "input_rate": 1, "output_rate": 0, "source": "s", "revision": "r",
    }  # fmt: skip
    for method, path, payload in (
        ("GET", "rate-cards", None),
        ("POST", "rate-cards", body),
        ("GET", f"rate-cards/{two.global_card}/versions", None),
        ("POST", f"rate-cards/{two.global_card}/versions", version),
    ):
        r = client.request(method, f"{PLATFORM}/{path}", json=payload, headers=h)
        assert r.status_code == 403, (method, path, r.text)
    # the platform administrator cannot see a client's card as a global one either
    ph = as_user(client, two.platform)
    for card in (two.a.card, two.b.card):
        r = client.get(f"{PLATFORM}/rate-cards/{card}/versions", headers=ph)
        assert r.status_code == 404


# ------------------------------------------------------------------ budgets and quotas
@dataclass(frozen=True)
class Gov:
    a_budget: str
    b_budget: str
    a_quota: str
    b_quota: str


@pytest.fixture
def gov(client: TestClient, world: World, two: Two) -> Gov:
    hb = as_user(client, two.b.email)
    quota = {"metric": "runs_per_day", "limit": 3}
    b_budget = budget(client, hb, two.b.base, tenant_scope(world.b), amount=777)
    b_quota = client.post(
        f"{two.b.base}/quotas", json={"scope": tenant_scope(world.b), **quota}, headers=hb
    ).json()["id"]
    ha = as_user(client, two.a.email)
    a_budget = budget(client, ha, two.a.base, tenant_scope(world.a), amount=50)
    a_quota = client.post(
        f"{two.a.base}/quotas", json={"scope": tenant_scope(world.a), **quota}, headers=ha
    ).json()["id"]
    return Gov(a_budget["id"], b_budget["id"], a_quota, b_quota)


def test_a_cannot_reach_bs_budgets_and_quotas(
    client: TestClient, two: Two, gov: Gov, owner_session: Session
) -> None:
    h = as_user(client, two.a.email)
    base = two.a.base
    # own objects first
    assert client.get(f"{base}/budgets/{gov.a_budget}", headers=h).status_code == 200
    assert client.get(f"{base}/budgets/{gov.a_budget}/status", headers=h).status_code == 200
    own = client.patch(f"{base}/budgets/{gov.a_budget}", json={"amount": 51}, headers=h)
    assert own.status_code == 200 and own.json()["amount"] == 51
    assert client.get(f"{base}/quotas/{gov.a_quota}", headers=h).status_code == 200
    own_q = client.patch(f"{base}/quotas/{gov.a_quota}", json={"limit": 4}, headers=h)
    assert own_q.status_code == 200 and own_q.json()["limit"] == 4
    # B's ids under A's organization answer like an id that does not exist
    unknown = uuid7()
    for method, path, payload in (
        ("GET", "budgets/{}", None),
        ("GET", "budgets/{}/status", None),
        ("PATCH", "budgets/{}", {"amount": 1}),
        ("GET", "quotas/{}", None),
        ("PATCH", "quotas/{}", {"limit": 1}),
    ):
        b_id = gov.b_budget if path.startswith("budgets") else gov.b_quota
        _same(
            client.request(method, f"{base}/{path.format(b_id)}", json=payload, headers=h),
            client.request(method, f"{base}/{path.format(unknown)}", json=payload, headers=h),
        )
    # absent from A's lists
    assert {b["id"] for b in client.get(f"{base}/budgets", headers=h).json()["items"]} == {
        gov.a_budget
    }
    assert {q["id"] for q in client.get(f"{base}/quotas", headers=h).json()["items"]} == {
        gov.a_quota
    }
    # B's slug in the path is an unknown organization
    missing = org(f"no-such-{uuid7().hex[-12:]}")
    for method, path, payload in (
        ("GET", "budgets", None),
        ("GET", f"budgets/{gov.b_budget}", None),
        ("PATCH", f"budgets/{gov.b_budget}", {"amount": 1}),
        ("GET", f"budgets/{gov.b_budget}/status", None),
        ("GET", "quotas", None),
        ("GET", f"quotas/{gov.b_quota}", None),
        ("PATCH", f"quotas/{gov.b_quota}", {"limit": 1}),
    ):
        _same(
            client.request(method, f"{two.b.base}/{path}", json=payload, headers=h),
            client.request(method, f"{missing}/{path}", json=payload, headers=h),
        )
    stored = owner_session.execute(
        text("SELECT amount FROM shifde.budgets WHERE id = :i"), {"i": gov.b_budget}
    ).scalar_one()
    assert float(stored) == 777  # B's budget is untouched


def test_a_cannot_scope_a_budget_or_quota_to_bs_scopes(
    client: TestClient, world: World, two: Two
) -> None:
    h = as_user(client, two.a.email)
    base = two.a.base
    own = client.post(
        f"{base}/budgets",
        json={"scope": workspace_scope(world.a.ws), "period": "daily", "amount": 1,
              "metric": "tokens"},
        headers=h,
    )  # fmt: skip
    assert own.status_code == 201, own.text
    own_q = client.post(
        f"{base}/quotas",
        json={"scope": project_scope(world.a), "metric": "runs_per_day", "limit": 1},
        headers=h,
    )
    assert own_q.status_code == 201, own_q.text
    unknown_ws, unknown_pj = uuid7(), uuid7()
    for foreign, nobody in (
        (workspace_scope(world.b.ws), {"type": "workspace", "id": str(unknown_ws)}),
        (project_scope(world.b), {"type": "project", "id": str(unknown_pj)}),
        (tenant_scope(world.b), {"type": "tenant", "id": str(unknown_ws)}),
    ):
        for path, extra in (
            ("budgets", {"period": "daily", "amount": 1, "metric": "tokens"}),
            ("quotas", {"metric": "runs_per_day", "limit": 1}),
        ):
            _same(
                client.post(f"{base}/{path}", json={"scope": foreign, **extra}, headers=h),
                client.post(f"{base}/{path}", json={"scope": nobody, **extra}, headers=h),
            )


# ------------------------------------------------------------------ usage and exports
def test_usage_reads_and_exports_hold_only_as_own(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    run_b = uuid7()
    for c, key in ((world.a, "a-1"), (world.b, "b-1"), (world.b, "b-2")):
        ledger.record(
            owner_session,
            {"tenant_id": c.tenant, "event_key": key, "event_type": "tool_call",
             "workspace_id": c.ws, "input_tokens": 100, "call_count": 1},
        )  # fmt: skip
    # `ledger.record` looks a run up in `runs` (runtime, not ported): B's run is a raw row
    owner_session.execute(
        text(
            "INSERT INTO shifde.usage_events (id, tenant_id, event_key, event_type, workspace_id, "
            "run_id, root_run_id) VALUES (:i, :t, 'b-run', 'tool_call', :w, :r, :r)"
        ),
        {"i": uuid7(), "t": world.b.tenant, "w": world.b.ws, "r": run_b},
    )
    owner_session.commit()
    h = as_user(client, two.a.email)
    base = two.a.base
    events = client.get(f"{base}/usage/events", headers=h).json()["items"]
    assert [e["event_key"] for e in events] == ["a-1"]
    summary = client.get(f"{base}/usage/summary?group_by=workspace", headers=h).json()
    assert [g["key"] for g in summary["groups"]] == [str(world.a.ws)]
    assert summary["totals"]["input_tokens"] == 100 and summary["totals"]["calls"] == 1
    assert summary["reconciliation"]["consistent"] is True
    # B's run under A's organization answers like a run that does not exist
    _same(
        client.get(f"{base}/usage/runs/{run_b}", headers=h),
        client.get(f"{base}/usage/runs/{uuid7()}", headers=h),
    )
    assert client.get(f"{base}/usage/events?run_id={run_b}", headers=h).json()["items"] == []
    # a rebuild by A leaves B's rollups alone and B's rows out of the answer
    sql = "SELECT sum(input_tokens) FROM shifde.usage_rollups_daily WHERE tenant_id = :t"
    before = owner_session.execute(text(sql), {"t": world.b.tenant}).scalar_one()
    rebuilt = client.post(f"{base}/usage/rollups/rebuild", headers=h)
    assert rebuilt.status_code == 200 and rebuilt.json()["consistent"] is True
    owner_session.rollback()
    after = owner_session.execute(text(sql), {"t": world.b.tenant}).scalar_one()
    assert before == after == 200
    exported = client.get(f"{base}/usage/export", headers=h)
    assert exported.status_code == 200
    assert str(run_b) not in exported.text and str(world.b.ws) not in exported.text
    assert str(world.a.ws) in exported.text
    # B sees only its own
    hb = as_user(client, two.b.email)
    b_events = client.get(f"{two.b.base}/usage/events", headers=hb).json()["items"]
    assert sorted(e["event_key"] for e in b_events) == ["b-1", "b-2", "b-run"]
    assert "a-1" not in client.get(f"{two.b.base}/usage/export", headers=hb).text
    h = as_user(client, two.a.email)
    missing = org(f"no-such-{uuid7().hex[-12:]}")
    for path in ("usage/events", "usage/summary", f"usage/runs/{run_b}", "usage/export"):
        _same(
            client.get(f"{two.b.base}/{path}", headers=h),
            client.get(f"{missing}/{path}", headers=h),
        )
    _same(
        client.post(f"{two.b.base}/usage/rollups/rebuild", headers=h),
        client.post(f"{missing}/usage/rollups/rebuild", headers=h),
    )


def test_the_audit_export_holds_only_the_callers_chain(
    client: TestClient, world: World, two: Two, gov: Gov, owner_session: Session
) -> None:
    h = as_user(client, two.a.email)
    r = client.get(f"{two.a.base}/audit-events/export?format=json", headers=h)
    assert r.status_code == 200, r.text
    rows = [json.loads(line) for line in r.text.splitlines()]
    ids = {x["id"] for x in rows}

    def owned(tenant: object) -> set[str]:
        found = owner_session.execute(
            text("SELECT id FROM shifde.audit_events WHERE tenant_id = :t"), {"t": tenant}
        )
        return {str(i[0]) for i in found}

    assert ids and ids <= owned(world.a.tenant) and not ids & owned(world.b.tenant)
    assert gov.b_budget not in r.text  # B's budget id appears in no row of A's chain
    missing = org(f"no-such-{uuid7().hex[-12:]}")
    _same(
        client.get(f"{two.b.base}/audit-events/export", headers=h),
        client.get(f"{missing}/audit-events/export", headers=h),
    )


# ------------------------------------------------------------------ pricing audit and archive
def _audit_count(owner: Session, action: str, tenant: object) -> int:
    return int(
        owner.execute(
            text(
                "SELECT count(*) FROM shifde.audit_events WHERE action = :a "
                "AND tenant_id IS NOT DISTINCT FROM :t"
            ),
            {"a": action, "t": tenant},
        ).scalar_one()
    )


def test_a_global_card_writes_one_installation_audit_row_and_a_client_card_one_client_row(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    card, version = "governance.rate_card.created", "governance.rate_card_version.created"
    counts = {
        (action, t): _audit_count(owner_session, action, t)
        for action in (card, version)
        for t in (None, world.a.tenant, world.b.tenant)
    }
    assert counts[(card, None)] == 1  # the fixture's global card
    ph = as_user(client, two.platform)
    g = client.post(
        f"{PLATFORM}/rate-cards",
        json={"provider_key": "fake", "model_key": "g-2", "name": "g2"},
        headers=ph,
    )
    assert g.status_code == 201, g.text
    gv = rate_version(client, ph, PLATFORM, g.json()["id"])
    assert gv["id"]
    owner_session.rollback()
    assert _audit_count(owner_session, card, None) == counts[(card, None)] + 1
    assert _audit_count(owner_session, version, None) == counts[(version, None)] + 1
    for t in (world.a.tenant, world.b.tenant):  # a global write is in no client's chain
        assert _audit_count(owner_session, card, t) == counts[(card, t)]
        assert _audit_count(owner_session, version, t) == counts[(version, t)]
    h = as_user(client, two.a.email)
    rate_card(client, h, two.a.base, "a-extra", "fake")
    owner_session.rollback()
    assert _audit_count(owner_session, card, world.a.tenant) == counts[(card, world.a.tenant)] + 1
    assert _audit_count(owner_session, card, world.b.tenant) == counts[(card, world.b.tenant)]
    assert _audit_count(owner_session, card, None) == counts[(card, None)] + 1  # still only g-2


def test_pricing_manage_on_an_archived_client_is_refused_while_reads_work(
    client: TestClient, world: World, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)

    def set_status(status: str) -> None:
        with owner_db.session() as o:
            o.execute(
                text("UPDATE shifde.tenants SET status = :s WHERE id = :t"),
                {"s": status, "t": world.a.tenant},
            )
            o.commit()

    set_status("archived")
    try:
        body = {"provider_key": "fake", "model_key": "late", "name": "late"}
        r = client.post(f"{two.a.base}/rate-cards", json=body, headers=h)
        assert r.status_code == 409 and r.json()["code"] == "scope_archived", r.text
        version = {
            "effective_from": "2999-01-01T00:00:00Z", "currency": "XTS", "unit": "per_call",
            "input_rate": 1, "output_rate": 0, "source": "s", "revision": "r",
        }  # fmt: skip
        v = client.post(f"{two.a.base}/rate-cards/{two.a.card}/versions", json=version, headers=h)
        assert v.status_code == 409 and v.json()["code"] == "scope_archived", v.text
        assert client.get(f"{two.a.base}/rate-cards", headers=h).status_code == 200
    finally:
        set_status("active")
