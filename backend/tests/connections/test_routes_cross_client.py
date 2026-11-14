"""Connections and egress routes, client A against client B (plan recipe step 5, B-1019).

Every route of `connections.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first gets their own object (200), then asks for B's object under A's organization and
gets exactly the answer an id that does not exist gets (404, same body), and finds B's rows absent
from A's lists. The client comes only from `tenancy.dependencies.resolve_org`: B's slug in the path
gives A the unknown-organization 404. Each side is built through the routes by its own owner.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.testservers import RestTestServer
from shifde.connections import api as connections_api
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests.connections.routes import (
    REST_TOKEN,
    activate,
    as_user,
    connection,
    grant,
    limited_user,
    org,
    post,
    rest_body,
    secret,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws2_slug, ws_slug

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
    base: str
    email: str
    tenant: UUID
    secret: str
    connection: str
    policy: str


def _side(
    client: TestClient,
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    slug: str,
    email: str,
    server: RestTestServer,
) -> Side:
    base = org(slug)
    h = as_user(client, email)
    sid = secret(app_db, settings, tenant, REST_TOKEN)
    conn = connection(client, h, rest_body(server, sid, name=f"rest-{slug}"), base)
    activate(client, h, conn["id"], base)
    found = client.post(f"{base}/connections/{conn['id']}/discover", headers=h)
    assert found.status_code == 200, found.text
    policy = post(client, f"{base}/egress-policies", h, {"name": f"policy-{slug}"})
    return Side(base, email, tenant, sid, str(conn["id"]), str(policy["id"]))


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def b_server() -> Iterator[RestTestServer]:
    """B's own listener (connections review 2 item 3, B-1513): a request reaching it while A
    calls the routes would mean A drove B's connection."""
    with RestTestServer(token=REST_TOKEN) as server:
        yield server


@pytest.fixture
def two(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    b_server: RestTestServer,
) -> Two:
    b = _side(client, app_db, settings, world.b.tenant, world.b.slug, people.owner_b, b_server)
    a = _side(client, app_db, settings, world.a.tenant, world.a.slug, people.owner_a, rest_server)
    return Two(a, b)


def _b_state(owner_db: Database, two: Two) -> tuple[Any, ...]:
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT name, status, health_status, row_version, config, secret_ref_id "
                "FROM shifde.connections WHERE id = :i"
            ),
            {"i": two.b.connection},
        ).one()
        tests = o.execute(
            text("SELECT count(*) FROM shifde.connection_tests WHERE connection_id = :i"),
            {"i": two.b.connection},
        ).scalar_one()
        found = o.execute(
            text("SELECT count(*) FROM shifde.connection_discoveries WHERE connection_id = :i"),
            {"i": two.b.connection},
        ).scalar_one()
        pol = o.execute(
            text("SELECT name, row_version FROM shifde.egress_policies WHERE id = :i"),
            {"i": two.b.policy},
        ).one()
    return (tuple(row), tests, found, tuple(pol))


# ------------------------------------------------------------------ route table
ROUTE_TESTS = {
    ("GET", "/api/v1/orgs/{org}/connections"): "test_a_lists_only_its_own_rows",
    ("POST", "/api/v1/orgs/{org}/connections"): "test_a_cannot_bind_bs_secret_or_policy",
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}"): "test_a_cannot_reach_bs_connection",
    ("PATCH", "/api/v1/orgs/{org}/connections/{connection_id}"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/status"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/test"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}/tests"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/discover"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}/discoveries/latest"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/invoke"): (
        "test_a_cannot_reach_bs_connection"
    ),
    ("GET", "/api/v1/orgs/{org}/egress-policies"): "test_a_lists_only_its_own_rows",
    ("POST", "/api/v1/orgs/{org}/egress-policies"): "test_bs_organization_is_unknown_to_a",
    ("GET", "/api/v1/orgs/{org}/egress-policies/{policy_id}"): "test_a_cannot_reach_bs_policy",
    ("PATCH", "/api/v1/orgs/{org}/egress-policies/{policy_id}"): "test_a_cannot_reach_bs_policy",
    ("GET", "/api/v1/platform/egress-default"): "test_the_installation_default_is_not_a_clients",
    ("PUT", "/api/v1/platform/egress-default"): "test_the_installation_default_is_not_a_clients",
}


def test_every_connections_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in connections_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


# ------------------------------------------------------------------ organization
def test_bs_organization_is_unknown_to_a(client: TestClient, world: World, two: Two) -> None:
    h = as_user(client, two.a.email)
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    body = {"name": "x"}
    own = client.post(f"{two.a.base}/egress-policies", headers=h, json={"name": "own-new"})
    assert own.status_code == 201, own.text
    for method, path, payload in (
        ("GET", "/connections", None),
        ("POST", "/connections", rest_body_none()),
        ("GET", f"/connections/{two.b.connection}", None),
        ("GET", "/egress-policies", None),
        ("POST", "/egress-policies", body),
        ("GET", f"/egress-policies/{two.b.policy}", None),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same_404(client.request(method, f"{two.b.base}{path}", headers=h, json=payload), missing)


def rest_body_none() -> dict[str, Any]:
    return {
        "name": "anon",
        "connection_type": "rest",
        "adapter_key": "rest.http",
        "config": {"base_url": "https://api.example.com"},
    }


# ------------------------------------------------------------------ connections
def test_a_cannot_reach_bs_connection(
    client: TestClient,
    owner_db: Database,
    two: Two,
    rest_server: RestTestServer,
    b_server: RestTestServer,
) -> None:
    A, h = two.a.base, as_user(client, two.a.email)
    before = _b_state(owner_db, two)
    b_before = list(b_server.requests)  # B's own test and discovery during setup
    own, theirs, nobody = two.a.connection, two.b.connection, str(uuid7())
    invoke = {"method": "GET", "path": "/health"}
    cases: list[tuple[str, str, Any, int]] = [
        ("GET", "", None, 200),
        ("GET", "/tests", None, 200),
        ("GET", "/discoveries/latest", None, 200),
        ("POST", "/discover", None, 200),
        ("POST", "/invoke", invoke, 200),
        ("POST", "/test", None, 200),
        ("PATCH", "", {"name": "renamed"}, 200),
        ("POST", "/status", {"status": "disabled"}, 200),
    ]
    for method, suffix, payload, ok in cases:
        r = client.request(method, f"{A}/connections/{own}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        missing = client.request(
            method, f"{A}/connections/{nobody}{suffix}", headers=h, json=payload
        )
        _same_404(
            client.request(method, f"{A}/connections/{theirs}{suffix}", headers=h, json=payload),
            missing,
        )
    assert _b_state(owner_db, two) == before  # nothing of B's changed, no test or discovery added
    a_calls = [r for r in rest_server.requests if r["method"] == "GET" and r["path"] == "/health"]
    assert a_calls  # A's own calls reached A's listener
    assert b_server.requests == b_before  # B's connection was never used by A


def test_a_lists_only_its_own_rows(client: TestClient, two: Two) -> None:
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        conns = client.get(f"{me.base}/connections?limit=200", headers=h)
        assert conns.status_code == 200, conns.text
        assert [c["id"] for c in conns.json()["items"]] == [me.connection]
        tests = client.get(f"{me.base}/connections/{me.connection}/tests", headers=h).json()
        assert tests["items"] and {t["connection_id"] for t in tests["items"]} == {me.connection}
        policies = client.get(f"{me.base}/egress-policies?limit=200", headers=h)
        assert policies.status_code == 200, policies.text
        ids = {p["id"] for p in policies.json()["items"]}
        assert me.policy in ids and other.policy not in ids
        assert ids == {me.policy}  # neither B's nor the installation default


def test_a_cannot_bind_bs_secret_or_policy(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    two: Two,
    rest_server: RestTestServer,
) -> None:
    A, h = two.a.base, as_user(client, two.a.email)
    own = connection(
        client,
        h,
        {**rest_body(rest_server, two.a.secret, name="own-bind"), "egress_policy_id": two.a.policy},
        A,
    )
    assert own["secret_set"] is True and own["egress_policy_id"] == two.a.policy
    nobody = str(uuid7())
    for field, theirs in (("secret_id", two.b.secret), ("egress_policy_id", two.b.policy)):
        body = {**rest_body(rest_server, two.a.secret, name=f"x-{field}")}
        missing = client.post(
            f"{A}/connections", headers=h, json=_confirmed({**body, field: nobody})
        )
        _same_404(
            client.post(f"{A}/connections", headers=h, json=_confirmed({**body, field: theirs})),
            missing,
        )
        row = client.get(f"{A}/connections/{own['id']}", headers=h).json()
        patch_missing = client.patch(
            f"{A}/connections/{own['id']}",
            headers=h,
            json={field: nobody, "row_version": row["row_version"], "read_only_confirmed": True},
        )
        _same_404(
            client.patch(
                f"{A}/connections/{own['id']}",
                headers=h,
                json={
                    field: theirs,
                    "row_version": row["row_version"],
                    "read_only_confirmed": True,
                },
            ),
            patch_missing,
        )
    names = [c["name"] for c in client.get(f"{A}/connections", headers=h).json()["items"]]
    assert not any(n.startswith("x-") for n in names)


def _confirmed(body: dict[str, Any]) -> dict[str, Any]:
    return {"scope": {}, "access_mode": "read", "read_only_confirmed": True, **body}


def test_a_secret_of_another_workspace_cannot_be_bound(
    client: TestClient,
    world: World,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Connections review 1 item 3: nothing in the schema ties a secret's scope to the
    connection's; `_check_secret` asks for `secret:use` at the secret's own scope (hidden unless
    `secret:read` there), so a workspace-1 manager cannot bind a workspace-2 secret."""
    a = world.a
    email, uid = limited_user(owner_db, a.tenant, ["connection:read"], label="ws-manager")
    grant(
        owner_db,
        a.tenant,
        uid,
        ["workspace:read", "connection:read", "connection:manage", "secret:use", "secret:read"],
        scope_type="workspace",
        scope_id=a.ws,
    )
    ws1 = secret(app_db, settings, a.tenant, REST_TOKEN, scope_type="workspace", scope_id=a.ws)
    ws2 = secret(app_db, settings, a.tenant, REST_TOKEN, scope_type="workspace", scope_id=a.ws2)
    A, h = org(a.slug), as_user(client, email)
    scope = {"workspace": ws_slug(a)}
    own = connection(client, h, {**rest_body(rest_server, ws1, name="ws1"), "scope": scope}, A)
    assert own["secret_set"] is True and own["scope"]["workspace"] == ws_slug(a)
    missing = client.post(
        f"{A}/connections",
        headers=h,
        json=_confirmed({**rest_body(rest_server, str(uuid7()), name="ws2"), "scope": scope}),
    )
    _same_404(
        client.post(
            f"{A}/connections",
            headers=h,
            json=_confirmed({**rest_body(rest_server, ws2, name="ws2"), "scope": scope}),
        ),
        missing,
    )
    row = client.get(f"{A}/connections/{own['id']}", headers=h).json()
    swap = client.patch(
        f"{A}/connections/{own['id']}",
        headers=h,
        json={"secret_id": ws2, "row_version": row["row_version"], "read_only_confirmed": True},
    )
    assert swap.status_code == 404, swap.text
    # the second workspace is not the manager's at all
    other_ws = connection(
        client, h, {**rest_body(rest_server, None, name="w2"), "scope": {"workspace": ws2_slug(a)}},
        A, status=404,
    )  # fmt: skip
    assert other_ws["status"] == 404


# ------------------------------------------------------------------ egress policies
def test_a_cannot_reach_bs_policy(client: TestClient, owner_db: Database, two: Two) -> None:
    A, h = two.a.base, as_user(client, two.a.email)
    before = _b_state(owner_db, two)
    nobody = str(uuid7())
    own = client.get(f"{A}/egress-policies/{two.a.policy}", headers=h)
    assert own.status_code == 200 and own.json()["id"] == two.a.policy
    _same_404(
        client.get(f"{A}/egress-policies/{two.b.policy}", headers=h),
        client.get(f"{A}/egress-policies/{nobody}", headers=h),
    )
    patched = client.patch(
        f"{A}/egress-policies/{two.a.policy}", headers=h, json={"name": "own-renamed"}
    )
    assert patched.status_code == 200 and patched.json()["name"] == "own-renamed", patched.text
    _same_404(
        client.patch(f"{A}/egress-policies/{two.b.policy}", headers=h, json={"name": "taken"}),
        client.patch(f"{A}/egress-policies/{nobody}", headers=h, json={"name": "taken"}),
    )
    assert _b_state(owner_db, two) == before


# ------------------------------------------------------------------ installation default
def test_the_installation_default_is_not_a_clients(
    client: TestClient, people: People, owner_db: Database, app_db: Database, two: Two
) -> None:
    """Only the platform administrator reads or writes the installation default; the change is
    an installation event (no client), recorded in the platform-administrator context (B-701)."""
    with owner_db.session() as o:
        saved = o.execute(
            text(
                "SELECT id, timeout_seconds, row_version FROM shifde.egress_policies "
                "WHERE tenant_id IS NULL AND is_default"
            )
        ).one()
    try:
        admin = as_user(client, people.platform)
        current = client.get("/api/v1/platform/egress-default", headers=admin)
        assert current.status_code == 200, current.text
        changed = client.put(
            "/api/v1/platform/egress-default",
            headers=admin,
            json={"timeout_seconds": 7.5, "row_version": current.json()["row_version"]},
        )
        assert changed.status_code == 200, changed.text
        for email in (two.a.email, two.b.email):
            h = as_user(client, email)
            assert client.get("/api/v1/platform/egress-default", headers=h).status_code == 403
            refused = client.put(
                "/api/v1/platform/egress-default", headers=h, json={"timeout_seconds": 1.0}
            )
            assert refused.status_code == 403
        with owner_db.session() as o:
            events = o.execute(
                text(
                    "SELECT tenant_id FROM shifde.audit_events "
                    "WHERE action = 'egress.default.updated'"
                )
            ).all()
            assert [tuple(e) for e in events] == [(None,)]
        for side in (two.a, two.b):  # neither client sees the installation event
            with app_session(app_db, side.tenant) as s:
                seen = s.execute(
                    text(
                        "SELECT count(*) FROM shifde.audit_events "
                        "WHERE action = 'egress.default.updated'"
                    )
                ).scalar_one()
                assert seen == 0
    finally:
        with owner_db.session() as o:
            o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
            o.execute(
                text(
                    "UPDATE shifde.egress_policies SET timeout_seconds = :t, row_version = :v "
                    "WHERE id = :i"
                ),
                {"t": saved.timeout_seconds, "v": saved.row_version, "i": saved.id},
            )
            o.commit()
