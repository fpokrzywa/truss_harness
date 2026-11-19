"""Delivery target and executor routes, client A against client B (plan recipe step 5, B-1019,
B-2664).

Every route of `delivery.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first succeeds on their own object, then asks for B's target, executor or credential
under A's organization and gets exactly the answer an id that does not exist gets (404, same body),
and finds B's rows absent from A's lists. B's slug in the path gives A the unknown-organization
404. An executor token answers for its own organization's executor only, also when two
organizations' credentials share a prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: target and executor rows, JSON bodies

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery import api as delivery_api
from shifde.identity.apikeys import hash_key
from tests.connections.routes import as_user, org
from tests.delivery.routes import (
    WHOAMI,
    address,
    bearer,
    create_executor,
    executors,
    issue,
    register,
    target_body,
    targets,
)
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
EXECUTORS = "/api/v1/orgs/{org}/delivery/executors"
CREDENTIAL = EXECUTORS + "/{executor_id}/credentials/{credential_id}"
ROUTE_TESTS = {
    ("POST", "/api/v1/orgs/{org}/delivery/targets"): "test_bs_organization_is_unknown_to_a",
    ("GET", "/api/v1/orgs/{org}/delivery/targets"): "test_a_lists_only_its_own_rows",
    ("GET", "/api/v1/orgs/{org}/delivery/targets/{target_id}"): "test_a_cannot_reach_bs_target",
    ("POST", "/api/v1/orgs/{org}/delivery/targets/{target_id}/archive"): (
        "test_a_cannot_reach_bs_target"
    ),
    ("POST", EXECUTORS): "test_bs_executors_are_unknown_to_a",
    ("GET", EXECUTORS): "test_a_lists_only_its_own_executors",
    ("GET", EXECUTORS + "/{executor_id}"): "test_a_cannot_reach_bs_executor",
    ("PATCH", EXECUTORS + "/{executor_id}"): "test_a_cannot_reach_bs_executor",
    ("GET", EXECUTORS + "/{executor_id}/credentials"): "test_a_cannot_reach_bs_executor",
    ("POST", EXECUTORS + "/{executor_id}/credentials"): "test_a_cannot_reach_bs_executor",
    ("POST", CREDENTIAL + "/rotate"): "test_a_cannot_reach_bs_executor",
    ("POST", CREDENTIAL + "/revoke"): "test_a_cannot_reach_bs_executor",
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


# ------------------------------------------------------------------ executors
@dataclass(frozen=True)
class ExecSide:
    slug: str
    email: str
    executor: str
    credential: str
    token: str


@dataclass(frozen=True)
class TwoExecutors:
    a: ExecSide
    b: ExecSide


@pytest.fixture
def two_executors(client: TestClient, world: World, people: People) -> TwoExecutors:
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        h = as_user(client, email)
        made = create_executor(client, h, c.slug)
        out = issue(client, h, c.slug, made["id"])
        sides.append(ExecSide(c.slug, email, made["id"], out["credential"]["id"], out["secret"]))
    client.cookies.clear()
    return TwoExecutors(sides[1], sides[0])


def _b_executor_state(owner_db: Database, two: TwoExecutors) -> tuple[Any, ...]:
    with owner_db.session() as o:
        executor = o.execute(
            text(
                "SELECT name, status, last_seen_at, version, updated_at "
                "FROM shifde.delivery_executors WHERE id = :i"
            ),
            {"i": two.b.executor},
        ).one()
        creds = o.execute(
            text(
                "SELECT id, prefix, secret_hash, expires_at, revoked_at, updated_at "
                "FROM shifde.delivery_executor_credentials WHERE executor_id = :i ORDER BY id"
            ),
            {"i": two.b.executor},
        ).all()
    return tuple(executor), tuple(tuple(c) for c in creds)


def test_bs_executors_are_unknown_to_a(
    client: TestClient, owner_db: Database, two_executors: TwoExecutors
) -> None:
    two = two_executors
    h = as_user(client, two.a.email)
    url = f"{executors(two.a.slug)}/{two.a.executor}"
    assert client.patch(url, json={"status": "disabled"}, headers=h).status_code == 200
    own = client.post(executors(two.a.slug), json={"name": "own"}, headers=h)
    assert own.status_code == 201, own.text  # own organization first
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    ex, cred = two.b.executor, two.b.credential
    before = _b_executor_state(owner_db, two)
    for method, path, payload in (
        ("GET", "/delivery/executors", None),
        ("POST", "/delivery/executors", {"name": "intruder"}),
        ("GET", f"/delivery/executors/{ex}", None),
        ("PATCH", f"/delivery/executors/{ex}", {"status": "disabled"}),
        ("GET", f"/delivery/executors/{ex}/credentials", None),
        ("POST", f"/delivery/executors/{ex}/credentials", None),
        ("POST", f"/delivery/executors/{ex}/credentials/{cred}/rotate", None),
        ("POST", f"/delivery/executors/{ex}/credentials/{cred}/revoke", None),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same_404(
            client.request(method, f"{org(two.b.slug)}{path}", headers=h, json=payload), missing
        )
    assert _b_executor_state(owner_db, two) == before


def test_a_cannot_reach_bs_executor(
    client: TestClient, owner_db: Database, two_executors: TwoExecutors
) -> None:
    two = two_executors
    A, h = executors(two.a.slug), as_user(client, two.a.email)
    before = _b_executor_state(owner_db, two)
    nobody = str(uuid7())

    def check(method: str, suffix: str, ok: int, payload: Any = None) -> Any:
        """Own executor first, then B's executor and an unknown id under A's organization."""
        r = client.request(method, f"{A}/{two.a.executor}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)
        _same_404(
            client.request(method, f"{A}/{two.b.executor}{suffix}", headers=h, json=payload),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h, json=payload),
        )
        return r.json()

    check("GET", "", 200)
    check("PATCH", "", 200, {"status": "active"})
    check("GET", "/credentials", 200)
    rotated = check("POST", f"/credentials/{two.a.credential}/rotate", 201)
    check("POST", f"/credentials/{two.a.credential}/revoke", 200)
    check("POST", "/credentials", 201)
    # B's credential under A's own executor: the answer an unknown credential id gets
    base = f"{A}/{two.a.executor}/credentials"
    for action in ("rotate", "revoke"):
        _same_404(
            client.post(f"{base}/{two.b.credential}/{action}", headers=h),
            client.post(f"{base}/{nobody}/{action}", headers=h),
        )
    listed = {c["id"] for c in client.get(base, headers=h).json()}
    assert two.b.credential not in listed and rotated["credential"]["id"] in listed
    assert _b_executor_state(owner_db, two) == before


def test_a_lists_only_its_own_executors(client: TestClient, two_executors: TwoExecutors) -> None:
    two = two_executors
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{executors(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [e["id"] for e in listed.json()["items"]]
        assert ids == [me.executor] and other.executor not in ids
        creds = client.get(f"{executors(me.slug)}/{me.executor}/credentials", headers=h).json()
        assert [c["id"] for c in creds] == [me.credential]


def test_bs_executor_token_sees_only_bs_executor(
    client: TestClient, owner_db: Database, two_executors: TwoExecutors
) -> None:
    two = two_executors
    r = client.get(WHOAMI, headers={**bearer(two.b.token), "X-Executor-Version": "b-1"})
    assert r.status_code == 200, r.text
    assert r.json()["executor_id"] == two.b.executor and r.json()["version"] == "b-1"
    with owner_db.session() as o:
        seen = dict(
            o.execute(
                text(
                    "SELECT id::text, last_seen_at FROM shifde.delivery_executors "
                    "WHERE id IN (:a, :b)"
                ),
                {"a": two.a.executor, "b": two.b.executor},
            ).all()
        )
    assert seen[two.a.executor] is None and seen[two.b.executor] is not None


def test_a_shared_prefix_resolves_per_organization(
    client: TestClient,
    world: World,
    owner_db: Database,
    settings: Settings,
    two_executors: TwoExecutors,
) -> None:
    two = two_executors
    prefix = "Shared01"
    tokens = {}
    with owner_db.session() as o:
        for side, tenant in ((two.a, world.a.tenant), (two.b, world.b.tenant)):
            token = f"shx_{prefix}_{uuid7().hex}{'s' * 11}"
            tokens[side.executor] = token
            o.execute(
                text(
                    "INSERT INTO shifde.delivery_executor_credentials (id, tenant_id, executor_id, "
                    "prefix, secret_hash, created_by, expires_at) VALUES (:i, :t, :e, :p, :h, :c, "
                    "now() + interval '1 day')"
                ),
                {
                    "i": uuid7(),
                    "t": tenant,
                    "e": side.executor,
                    "p": prefix,
                    "h": hash_key(settings.pepper, token),
                    "c": uuid7(),
                },
            )
        o.commit()
    for executor, token in tokens.items():
        r = client.get(WHOAMI, headers=bearer(token))
        assert r.status_code == 200 and r.json()["executor_id"] == executor, r.text
