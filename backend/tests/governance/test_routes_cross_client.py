"""governance routes (part 1: rate cards), client A against client B (plan recipe step 5, B-1801).

Every route of `governance.api.router` has a test here (`ROUTE_TESTS`, matched against the
router): the caller first gets their own object (200 / 201), then asks for B's object under A's
organization and gets exactly the answer an id that does not exist gets (404, same body), and finds
B's rows absent from A's lists. B's slug in the path gives A the unknown-organization 404. The
platform price-list routes are the platform administrator's: a client user is refused.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from shifde.core.ids import uuid7
from shifde.governance import api as governance_api
from tests.connections.routes import as_user, org
from tests.governance.routes import PLATFORM, rate_card, rate_version
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
