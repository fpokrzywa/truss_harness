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
from typing import Any
from uuid import RFC_4122, UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.governance import api as governance_api
from shifde.models_admin.repository import SHI_PROVIDER_ID
from tests._support.runs import add_run
from tests.connections.routes import as_user, org
from tests.governance.routes import (
    CURRENCY,
    PLATFORM,
    budget,
    project_scope,
    rate_card,
    rate_version,
    tenant_scope,
    workspace_scope,
)
from tests.governance.runtime_rows import HOLD, OLD_DAYS, RuntimeRows, seed_runtime
from tests.ledger.util import add_budget, add_model
from tests.tenancy.conftest import Client, World
from tests.tenancy.routes import People

_VARYING = {"instance", "request_id"}

# Event keys of the usage test. Each holds letters and underscores no generated id (UUID, hex
# digest, hex slug tail) can contain (#377). A's is also its event's model_key: the export writes
# that column and not the event key.
USAGE_MARKER_A = "usage_row_owned_by_first_client"
USAGE_MARKER_B_ONE = "usage_row_one_of_second_client"
USAGE_MARKER_B_TWO = "usage_row_two_of_second_client"


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
    ("GET", "/api/v1/orgs/{org}/governance/policies"): "test_a_cannot_reach_bs_policies",
    ("POST", "/api/v1/orgs/{org}/governance/policies"): "test_a_cannot_reach_bs_policies",
    ("GET", "/api/v1/orgs/{org}/governance/policies/{policy_id}"): (
        "test_a_cannot_reach_bs_policies"
    ),
    ("PATCH", "/api/v1/orgs/{org}/governance/policies/{policy_id}"): (
        "test_a_cannot_reach_bs_policies"
    ),
    ("GET", "/api/v1/orgs/{org}/governance/retention"): "test_retention_is_per_client",
    ("PUT", "/api/v1/orgs/{org}/governance/retention"): "test_retention_is_per_client",
    ("POST", "/api/v1/orgs/{org}/governance/retention/preview"): "test_retention_is_per_client",
    ("GET", "/api/v1/orgs/{org}/governance/decisions"): (
        "test_decisions_and_overview_hold_only_as_own"
    ),
    ("GET", "/api/v1/orgs/{org}/governance/overview"): (
        "test_decisions_and_overview_hold_only_as_own"
    ),
    ("GET", "/api/v1/orgs/{org}/observability/performance"): (
        "test_performance_is_scoped_to_the_organization"
    ),
    ("GET", "/api/v1/platform/health-details"): (
        "test_platform_health_details_are_for_the_platform_administrator_only"
    ),
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
def test_no_generated_id_can_contain_a_usage_marker() -> None:
    chance = UUID("01a210aa-1162-7000-8000-000000000000")  # the prefix of B's run id in #377
    assert chance.version == 7 and chance.variant == RFC_4122
    assert "a-1" in str(chance)  # the old marker of A's usage row
    for marker in (USAGE_MARKER_A, USAGE_MARKER_B_ONE, USAGE_MARKER_B_TWO):
        assert set(marker) - set("0123456789abcdef-"), marker


def test_usage_reads_and_exports_hold_only_as_own(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    def run_of(c: Client) -> UUID:
        return add_run(owner_session, c.tenant, workspace_id=c.ws, project_id=c.project)

    run_b = run_of(world.b)  # a real run of B (runtime_0001), with one usage event
    idle_b, idle_a = run_of(world.b), run_of(world.a)  # real runs with no usage event
    model_a = add_model(owner_session, world.a.tenant, "openai", USAGE_MARKER_A)
    model_of_a = {"model_id": model_a, "provider_key": "openai", "model_key": USAGE_MARKER_A}
    for c, key, model in (
        (world.a, USAGE_MARKER_A, model_of_a),
        (world.b, USAGE_MARKER_B_ONE, {}),
        (world.b, USAGE_MARKER_B_TWO, {}),
    ):
        ledger.record(
            owner_session,
            {"tenant_id": c.tenant, "event_key": key, "event_type": "tool_call",
             "workspace_id": c.ws, "input_tokens": 100, "call_count": 1, **model},
        )  # fmt: skip
    # B's usage event on B's run, written as a raw row
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
    assert [e["event_key"] for e in events] == [USAGE_MARKER_A]
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
    # A's own run with no usage yet is A's (200, no events); B's such run is an unknown id
    own_idle = client.get(f"{base}/usage/runs/{idle_a}", headers=h)
    assert own_idle.status_code == 200, own_idle.text
    assert own_idle.json()["run_id"] == str(idle_a) and own_idle.json()["events"] == []
    _same(
        client.get(f"{base}/usage/runs/{idle_b}", headers=h),
        client.get(f"{base}/usage/runs/{uuid7()}", headers=h),
    )
    # a rebuild by A leaves B's rollups alone and B's rows out of the answer
    sql = "SELECT sum(input_tokens) FROM shifde.usage_rollups_daily WHERE tenant_id = :t"
    before = owner_session.execute(text(sql), {"t": world.b.tenant}).scalar_one()
    rebuilt = client.post(f"{base}/usage/rollups/rebuild", headers=h)
    assert rebuilt.status_code == 200 and rebuilt.json()["consistent"] is True
    # `consistent` stays true when B's events reach both sides, so the sums are checked too
    assert rebuilt.json()["events_sum"] == 100 and rebuilt.json()["rollup_sum"] == 100
    owner_session.rollback()
    after = owner_session.execute(text(sql), {"t": world.b.tenant}).scalar_one()
    assert before == after == 200
    exported = client.get(f"{base}/usage/export", headers=h)
    assert exported.status_code == 200
    assert str(run_b) not in exported.text and str(world.b.ws) not in exported.text
    assert str(world.a.ws) in exported.text
    assert USAGE_MARKER_A in exported.text  # so its absence from B's export below says something
    # B sees only its own
    hb = as_user(client, two.b.email)
    b_events = client.get(f"{two.b.base}/usage/events", headers=hb).json()["items"]
    assert sorted(e["event_key"] for e in b_events) == [
        "b-run",
        USAGE_MARKER_B_ONE,
        USAGE_MARKER_B_TWO,
    ]
    assert USAGE_MARKER_A not in client.get(f"{two.b.base}/usage/export", headers=hb).text
    # the raw `b-run` row has no rollup yet: B rebuilds its own before its summary is read
    b_rebuilt = client.post(f"{two.b.base}/usage/rollups/rebuild", headers=hb)
    assert b_rebuilt.status_code == 200, b_rebuilt.text
    b_answer = b_rebuilt.json()
    assert b_answer["consistent"] is True
    assert b_answer["events_sum"] == 200 and b_answer["rollup_sum"] == 200
    read = client.get(f"{two.b.base}/usage/summary?group_by=workspace", headers=hb)
    assert read.status_code == 200, read.text
    b_summary = read.json()
    assert [g["key"] for g in b_summary["groups"]] == [str(world.b.ws)]
    # B's two events of 100 tokens and 1 call, plus the defaults of `b-run` (0 tokens, 1 call)
    assert b_summary["totals"]["input_tokens"] == 200 and b_summary["totals"]["calls"] == 3
    # `consistent` stays true when a leak reaches both sides, so the sums are checked too
    b_rec = b_summary["reconciliation"]
    assert b_rec["consistent"] is True
    assert b_rec["events_sum"] == 200 and b_rec["rollup_sum"] == 200
    assert b_rec["mismatched_rows"] == 0 and b_rec["detail"] == []
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


def _seed_both(owner: Session, world: World) -> tuple[RuntimeRows, RuntimeRows, UUID]:
    """Runtime rows of A (one of each) and B (two of each), committed; the model id is shared."""
    model = uuid7()
    rows_a = seed_runtime(owner, world.a.tenant, world.a.ws, world.a.project, n=1, model=model)
    rows_b = seed_runtime(owner, world.b.tenant, world.b.ws, world.b.project, n=2, model=model)
    owner.commit()
    return rows_a, rows_b, model


def test_a_usage_event_naming_bs_step_lists_no_step_name(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    """The step name comes from the event's own client's steps: an event of A whose `step_id`
    is B's step shows no name (isolation review 1, F1)."""
    rows_a, rows_b, _ = _seed_both(owner_session, world)
    for key, step in (("own-step", rows_a.steps[0]), ("bs-step", rows_b.steps[0])):
        owner_session.execute(
            text(
                "INSERT INTO shifde.usage_events (id, tenant_id, event_key, event_type, "
                "workspace_id, run_id, root_run_id, step_id) "
                "VALUES (:i, :t, :k, 'tool_call', :w, :r, :r, :s)"
            ),
            {"i": uuid7(), "t": world.a.tenant, "k": key, "w": world.a.ws,
             "r": rows_a.runs[0], "s": step},
        )  # fmt: skip
    owner_session.commit()
    h = as_user(client, two.a.email)
    listed = client.get(f"{two.a.base}/usage/events", headers=h)
    on_run = client.get(f"{two.a.base}/usage/runs/{rows_a.runs[0]}", headers=h)
    assert listed.status_code == on_run.status_code == 200, (listed.text, on_run.text)
    for events in (listed.json()["items"], on_run.json()["events"]):
        by_key = {e["event_key"]: e for e in events}
        assert set(by_key) == {"own-step", "bs-step"}
        assert by_key["own-step"]["step_name"] == rows_a.step_name
        assert by_key["bs-step"]["step_id"] == str(rows_b.steps[0])
        assert by_key["bs-step"]["step_name"] is None, by_key["bs-step"]
    assert rows_b.step_name not in listed.text + on_run.text


def test_budget_status_counts_only_the_clients_own_runs_and_holds(
    client: TestClient, world: World, two: Two, gov: Gov, owner_session: Session
) -> None:
    """Held reservations, stale holds and the run count of a run budget come from the client's
    own runs only (isolation review 1, F1)."""
    rows_a, rows_b, _ = _seed_both(owner_session, world)
    runs_budget = {c.tenant: add_budget(owner_session, c.tenant, metric="runs", amount="100")
                   for c in (world.a, world.b)}  # fmt: skip
    owner_session.commit()
    for side, c, rows, tokens_budget in (
        (two.a, world.a, rows_a, gov.a_budget),
        (two.b, world.b, rows_b, gov.b_budget),
    ):
        h = as_user(client, side.email)
        held = client.get(f"{side.base}/budgets/{tokens_budget}/status", headers=h)
        assert held.status_code == 200, held.text
        n = len(rows.runs)
        assert held.json()["reserved"] == HOLD * n and held.json()["stale_holds"] == n, held.text
        runs = client.get(f"{side.base}/budgets/{runs_budget[c.tenant]}/status", headers=h)
        assert runs.status_code == 200, runs.text
        assert runs.json()["metric"] == "runs" and runs.json()["settled"] == rows.root_runs
    assert (rows_a.root_runs, rows_b.root_runs) == (3, 5)


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


def test_pricing_on_an_archived_client_is_refused_reads_included(
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
        # since B-2512 every route of a client that is not active is refused, reads included
        g = client.get(f"{two.a.base}/rate-cards", headers=h)
        assert g.status_code == 409 and g.json()["code"] == "scope_archived", g.text
    finally:
        set_status("active")
    assert client.get(f"{two.a.base}/rate-cards", headers=h).status_code == 200


# ------------------------------------------------------------------ policies (part 3)
def _policy(client: TestClient, h: dict[str, str], base: str, definition: dict[str, Any]) -> Any:
    r = client.post(
        f"{base}/governance/policies",
        json={"kind": "data_export", "name": "t", "definition": definition},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_a_cannot_reach_bs_policies(client: TestClient, world: World, two: Two) -> None:
    hb = as_user(client, two.b.email)
    b_pol = _policy(client, hb, two.b.base, {"allow_export": False})
    b_scoped = client.post(
        f"{two.b.base}/governance/policies",
        json={
            "kind": "cost_threshold",
            "definition": {"threshold_amount": 1, "currency": CURRENCY},
            "scope": workspace_scope(world.b.ws),
        },
        headers=hb,
    )
    assert b_scoped.status_code == 201, b_scoped.text
    h = as_user(client, two.a.email)
    a_pol = _policy(client, h, two.a.base, {"allow_export": True})
    got = client.get(f"{two.a.base}/governance/policies/{a_pol['id']}", headers=h)
    assert got.status_code == 200 and got.json()["id"] == a_pol["id"]
    upd = client.patch(
        f"{two.a.base}/governance/policies/{a_pol['id']}",
        json={"status": "disabled", "row_version": a_pol["row_version"]},
        headers=h,
    )
    assert upd.status_code == 200
    listed = client.get(f"{two.a.base}/governance/policies", headers=h).json()["items"]
    assert [p["id"] for p in listed] == [a_pol["id"]]  # B's two policies are absent
    unknown = uuid7()
    for ident in (b_pol["id"], b_scoped.json()["id"]):
        _same(
            client.get(f"{two.a.base}/governance/policies/{ident}", headers=h),
            client.get(f"{two.a.base}/governance/policies/{unknown}", headers=h),
        )
        _same(
            client.patch(
                f"{two.a.base}/governance/policies/{ident}", json={"status": "disabled"}, headers=h
            ),
            client.patch(
                f"{two.a.base}/governance/policies/{unknown}",
                json={"status": "disabled"},
                headers=h,
            ),
        )
    # a policy cannot be scoped to B's scopes: refused like an unknown scope
    for foreign, nobody in (
        (workspace_scope(world.b.ws), {"type": "workspace", "id": str(unknown)}),
        (project_scope(world.b), {"type": "project", "id": str(unknown)}),
        (tenant_scope(world.b), {"type": "tenant", "id": str(unknown)}),
    ):
        body = {"kind": "cost_threshold", "definition": {"threshold_amount": 1, "currency": "XTS"}}
        _same(
            client.post(
                f"{two.a.base}/governance/policies", json={**body, "scope": foreign}, headers=h
            ),
            client.post(
                f"{two.a.base}/governance/policies", json={**body, "scope": nobody}, headers=h
            ),
        )
    # B's data_export policy (no exports) does not apply in A: A's export works, B's is denied
    assert client.get(f"{two.a.base}/usage/export?format=csv", headers=h).status_code == 200
    hb = as_user(client, two.b.email)
    assert client.get(f"{two.b.base}/usage/export?format=csv", headers=hb).status_code == 403
    # B's policies are untouched; B's slug under A is the unknown organization
    b_list = client.get(f"{two.b.base}/governance/policies", headers=hb).json()["items"]
    assert {p["id"] for p in b_list} == {b_pol["id"], b_scoped.json()["id"]}
    assert all(p["status"] == "active" for p in b_list)
    h = as_user(client, two.a.email)
    nobody_org = org(f"no-such-{uuid7().hex[-12:]}")
    for method, path, payload in (
        ("GET", "governance/policies", None),
        ("GET", f"governance/policies/{b_pol['id']}", None),
        ("POST", "governance/policies", {"kind": "data_export", "definition": {"max_rows": 1}}),
        ("PATCH", f"governance/policies/{b_pol['id']}", {"status": "disabled"}),
    ):
        _same(
            client.request(method, f"{two.b.base}/{path}", json=payload, headers=h),
            client.request(method, f"{nobody_org}/{path}", json=payload, headers=h),
        )


def test_retention_is_per_client(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    _seed_both(owner_session, world)  # old run artifacts: A one of each kind, B two
    h = as_user(client, two.a.email)
    put = client.put(
        f"{two.a.base}/governance/retention",
        json={"items": [{"data_class": "audit_events", "retain_days": 100, "legal_hold": True}]},
        headers=h,
    )
    assert put.status_code == 200
    got = client.get(f"{two.a.base}/governance/retention", headers=h).json()["items"]
    own = {i["data_class"]: i for i in got}
    assert own["audit_events"]["retain_days"] == 100 and own["audit_events"]["legal_hold"] is True
    a_prev = client.post(f"{two.a.base}/governance/retention/preview", headers=h)
    assert a_prev.status_code == 200
    a_counts = {i["data_class"]: i["count"] for i in a_prev.json()["items"]}
    assert (a_counts["run_artifacts"], a_counts["run_steps_content"]) == (1, 1), a_counts
    hb = as_user(client, two.b.email)
    got_b = client.get(f"{two.b.base}/governance/retention", headers=hb).json()["items"]
    theirs = {i["data_class"]: i for i in got_b}
    assert theirs["audit_events"]["is_default"] is True and not theirs["audit_events"]["legal_hold"]
    b_prev = client.post(f"{two.b.base}/governance/retention/preview", headers=hb).json()["items"]
    assert not {i["data_class"]: i for i in b_prev}["audit_events"]["on_legal_hold"]
    b_counts = {i["data_class"]: i["count"] for i in b_prev}
    assert (b_counts["run_artifacts"], b_counts["run_steps_content"]) == (2, 2), b_counts
    assert max(i["retain_days"] for i in got_b if i["data_class"].startswith("run_")) < OLD_DAYS
    h = as_user(client, two.a.email)
    nobody_org = org(f"no-such-{uuid7().hex[-12:]}")
    items = {"items": [{"data_class": "usage_events", "retain_days": 60, "legal_hold": False}]}
    for method, path, payload in (
        ("GET", "governance/retention", None),
        ("PUT", "governance/retention", items),
        ("POST", "governance/retention/preview", None),
    ):
        _same(
            client.request(method, f"{two.b.base}/{path}", json=payload, headers=h),
            client.request(method, f"{nobody_org}/{path}", json=payload, headers=h),
        )
    hb = as_user(client, two.b.email)  # nothing was written to B
    after = client.get(f"{two.b.base}/governance/retention", headers=hb).json()["items"]
    assert all(i["is_default"] for i in after)


def _decision(owner: Session, tenant: UUID, action: str, decision: str, *, policy: bool) -> UUID:
    ident = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.policy_decisions (id, tenant_id, action, decision, reason_codes, "
            "policy_ids) VALUES (:i, :t, :a, :d, CAST(:r AS text[]), "
            "CASE WHEN :p THEN ARRAY[CAST(:i AS uuid)] ELSE CAST('{}' AS uuid[]) END)"
        ),
        {
            "i": ident, "t": tenant, "a": action, "d": decision, "p": policy,
            "r": "{budget_denied}" if decision == "deny" else "{}",
        },
    )  # fmt: skip
    owner.commit()
    return ident


def test_decisions_and_overview_hold_only_as_own(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    a_ids = {
        _decision(owner_session, world.a.tenant, "run:start", "deny", policy=True),
        _decision(owner_session, world.a.tenant, "run:start", "allow", policy=False),
    }
    b_ids = {
        _decision(owner_session, world.b.tenant, "run:start", "deny", policy=True),
        _decision(owner_session, world.b.tenant, "run:start", "deny", policy=True),
        _decision(owner_session, world.b.tenant, "model:invoke", "allow", policy=False),
    }
    h = as_user(client, two.a.email)
    seen = client.get(f"{two.a.base}/governance/decisions", headers=h).json()["items"]
    assert a_ids <= {UUID(d["id"]) for d in seen}  # plus the decisions A's own requests wrote
    assert not {UUID(d["id"]) for d in seen} & b_ids
    deny = "decision=deny&action=run:start"
    denied = client.get(f"{two.a.base}/governance/decisions?{deny}", headers=h).json()
    assert len(denied["items"]) == 1 and denied["items"][0]["reason_codes"] == ["budget_denied"]
    only_b = client.get(f"{two.a.base}/governance/decisions?action=model:invoke", headers=h)
    assert only_b.json()["items"] == []  # B's only model:invoke decision
    page = client.get(
        f"{two.a.base}/governance/decisions?limit=1&action=run:start", headers=h
    ).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = client.get(
        f"{two.a.base}/governance/decisions?limit=1&action=run:start&cursor={page['next_cursor']}",
        headers=h,
    ).json()
    assert {UUID(page["items"][0]["id"]), UUID(rest["items"][0]["id"])} == a_ids
    maybe = client.get(f"{two.a.base}/governance/decisions?decision=maybe", headers=h)
    assert maybe.status_code == 422
    # the overview counts A's denial and A's policies only
    _policy(client, h, two.a.base, {"allow_export": True})
    hb = as_user(client, two.b.email)
    _policy(client, hb, two.b.base, {"allow_export": True})
    _policy(client, hb, two.b.base, {"max_rows": 5})
    h = as_user(client, two.a.email)
    ov_r = client.get(f"{two.a.base}/governance/overview", headers=h)
    assert ov_r.status_code == 200, ov_r.text
    ov = ov_r.json()
    assert ov["denials_7d"] == 1 and ov["policy_counts"] == {"data_export": 1}
    assert ov["pending_approvals"] == 0 and ov["budgets"] == []
    hb = as_user(client, two.b.email)
    b_ov = client.get(f"{two.b.base}/governance/overview", headers=hb).json()
    assert b_ov["denials_7d"] == 2 and b_ov["policy_counts"] == {"data_export": 2}
    # pending approvals on real runs: A has one (and one decided), B two
    _seed_both(owner_session, world)
    for side, expected in ((two.a, 1), (two.b, 2)):
        hx = as_user(client, side.email)
        pending = client.get(f"{side.base}/governance/overview", headers=hx).json()
        assert pending["pending_approvals"] == expected, pending
    h = as_user(client, two.a.email)
    nobody_org = org(f"no-such-{uuid7().hex[-12:]}")
    for path in ("governance/decisions", "governance/overview"):
        _same(
            client.get(f"{two.b.base}/{path}", headers=h),
            client.get(f"{nobody_org}/{path}", headers=h),
        )


def test_performance_is_scoped_to_the_organization(
    client: TestClient, world: World, two: Two, owner_session: Session
) -> None:
    h = as_user(client, two.a.email)
    for group in ("agent", "model", "pipeline"):
        r = client.get(f"{two.a.base}/observability/performance?group_by={group}", headers=h)
        assert r.status_code == 200 and r.json()["groups"] == [], r.text  # no runs yet
    # real finished runs and model steps: A one of each, B two; both call the same model id
    rows_a, rows_b, model = _seed_both(owner_session, world)
    for side, rows in ((two.a, rows_a), (two.b, rows_b)):
        hx = as_user(client, side.email)
        for group, key in (("agent", rows.agent), ("model", model), ("pipeline", rows.pipeline)):
            r = client.get(f"{side.base}/observability/performance?group_by={group}", headers=hx)
            assert r.status_code == 200, r.text
            got = [(g["key"], g["runs"], g["error_count"]) for g in r.json()["groups"]]
            assert got == [(str(key), len(rows.runs), 0)], (group, got)
    h = as_user(client, two.a.email)
    bad = client.get(f"{two.a.base}/observability/performance?group_by=user", headers=h)
    assert bad.status_code == 422
    nobody_org = org(f"no-such-{uuid7().hex[-12:]}")
    _same(
        client.get(f"{two.b.base}/observability/performance", headers=h),
        client.get(f"{nobody_org}/observability/performance", headers=h),
    )


def test_platform_health_details_are_for_the_platform_administrator_only(
    client: TestClient, two: Two
) -> None:
    ph = as_user(client, two.platform)
    got = client.get(f"{PLATFORM}/health-details", headers=ph)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["database"]["reachable"] is True and body["migrations"]["status"] == "ok"
    assert set(body) >= {"workers", "queue", "reaper", "recent_error_rates", "telemetry"}
    refused = []
    for email in (two.a.email, two.b.email):
        h = as_user(client, email)
        r = client.get(f"{PLATFORM}/health-details", headers=h)
        assert r.status_code == 403, r.text
        refused.append(r)
    _same(refused[0], refused[1], 403)  # one answer for every client user


def test_resolve_by_model_id_prices_a_shared_model_from_global_cards_only(
    client: TestClient, world: World, two: Two, owner_session: Session, app_db: Database
) -> None:
    from tests.models_admin.conftest import make_shi

    model_id = str(make_shi(app_db))
    try:
        _resolve_by_model_id(client, world, two, owner_session, model_id)
    finally:
        owner_session.execute(text("DELETE FROM shifde.models WHERE id = :i"), {"i": model_id})
        owner_session.execute(
            text("DELETE FROM shifde.model_providers WHERE id = :i"),
            {"i": str(SHI_PROVIDER_ID)},
        )
        owner_session.commit()


def _resolve_by_model_id(
    client: TestClient, world: World, two: Two, owner_session: Session, model_id: str
) -> None:
    keys = ledger.pricing.rate_keys(owner_session, world.a.tenant, UUID(model_id))
    assert keys is not None and keys[2] is True  # shared
    provider_key, model_key, _ = keys
    h = as_user(client, two.a.email)
    own = rate_card(client, h, two.a.base, model_key, provider_key)
    rate_version(client, h, two.a.base, own["id"])
    hp = as_user(client, two.platform)
    g = client.post(
        f"{PLATFORM}/rate-cards",
        json={"provider_key": provider_key, "model_key": model_key, "name": "g"},
        headers=hp,
    ).json()
    rate_version(client, hp, PLATFORM, g["id"])
    h = as_user(client, two.a.email)
    q = f"provider_key={provider_key}&model_key={model_key}"
    by_keys = client.get(f"{two.a.base}/rate-cards/resolve?{q}", headers=h)
    assert by_keys.status_code == 200 and by_keys.json()["card"]["id"] == own["id"]
    by_id = client.get(f"{two.a.base}/rate-cards/resolve?model_id={model_id}", headers=h)
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["card"]["id"] == g["id"]  # what pricing uses for a shared model
    _same(
        client.get(f"{two.a.base}/rate-cards/resolve?model_id={uuid7()}", headers=h),
        client.get(f"{two.a.base}/rate-cards/resolve?model_id={uuid7()}", headers=h),
    )
    assert client.get(f"{two.a.base}/rate-cards/resolve", headers=h).status_code == 422
