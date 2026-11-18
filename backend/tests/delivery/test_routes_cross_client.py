"""Delivery target routes, client A against client B (plan recipe step 5, B-1019).

Every route of `delivery.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first succeeds on their own object, then asks for B's target under A's organization and
gets exactly the answer an id that does not exist gets (404, same body), and finds B's rows absent
from A's lists. B's slug in the path gives A the unknown-organization 404.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: target rows

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import api as delivery_api
from tests.connections.routes import as_user, org
from tests.delivery.routes import address, register, target_body, targets
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, ws_slug

_VARYING = {"instance", "request_id"}


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    """Refused exactly like an id or slug that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    slug: str
    email: str
    target: str


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def two(client: TestClient, world: World, people: People) -> Two:
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        made = register(client, as_user(client, email), c.slug, target_body(ws_slug(c), address()))
        sides.append(Side(c.slug, email, made["id"]))
    return Two(sides[1], sides[0])


def _b_state(owner_db: Database, two: Two) -> tuple[Any, ...]:
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT name, status, revision, claim_id, updated_at FROM shifde.delivery_targets "
                "WHERE id = :i"
            ),
            {"i": two.b.target},
        ).one()
    return tuple(row)


# ------------------------------------------------------------------ route table
ROUTE_TESTS = {
    ("POST", "/api/v1/orgs/{org}/delivery/targets"): "test_bs_organization_is_unknown_to_a",
    ("GET", "/api/v1/orgs/{org}/delivery/targets"): "test_a_lists_only_its_own_rows",
    ("GET", "/api/v1/orgs/{org}/delivery/targets/{target_id}"): "test_a_cannot_reach_bs_target",
    ("POST", "/api/v1/orgs/{org}/delivery/targets/{target_id}/archive"): (
        "test_a_cannot_reach_bs_target"
    ),
}


def test_every_delivery_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in delivery_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


def test_bs_organization_is_unknown_to_a(
    client: TestClient, world: World, owner_db: Database, two: Two
) -> None:
    h = as_user(client, two.a.email)
    own = client.post(targets(two.a.slug), headers=h, json=target_body(ws_slug(world.a), address()))
    assert own.status_code == 201, own.text
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    body = target_body(ws_slug(world.b), address())
    before = _b_state(owner_db, two)
    for method, path, payload in (
        ("GET", "/delivery/targets", None),
        ("POST", "/delivery/targets", body),
        ("GET", f"/delivery/targets/{two.b.target}", None),
        ("POST", f"/delivery/targets/{two.b.target}/archive", None),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same_404(
            client.request(method, f"{org(two.b.slug)}{path}", headers=h, json=payload), missing
        )
    assert _b_state(owner_db, two) == before


def test_a_cannot_reach_bs_target(client: TestClient, owner_db: Database, two: Two) -> None:
    A, h = targets(two.a.slug), as_user(client, two.a.email)
    before = _b_state(owner_db, two)
    nobody = str(uuid7())
    for method, suffix, ok in (("GET", "", 200), ("POST", "/archive", 200)):
        r = client.request(method, f"{A}/{two.a.target}{suffix}", headers=h)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        _same_404(
            client.request(method, f"{A}/{two.b.target}{suffix}", headers=h),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h),
        )
    assert _b_state(owner_db, two) == before


def test_a_lists_only_its_own_rows(client: TestClient, two: Two) -> None:
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{targets(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [t["id"] for t in listed.json()["items"]]
        assert ids == [me.target] and other.target not in ids
