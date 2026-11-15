"""models_admin routes, client A against client B (plan recipe step 5, B-1019, B-1718).

Every route of `models_admin.api.router` has a test here (`ROUTE_TESTS`, matched against the
router): the caller first gets their own object (200), then asks for B's object under A's
organization and gets exactly the answer an id that does not exist gets (404, same body), and
finds B's rows absent from A's lists. B's slug in the path gives A the unknown-organization 404.
The shared SHI provider and its models are never written through a client route, and a client
never configures the provider or flips its own switch. Each side is built through the routes by
its own owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.models_admin import api as models_api
from shifde.models_admin import repository
from tests.connections.conftest import owner_vendor
from tests.connections.routes import as_user, org
from tests.models_admin.routes import (
    PLATFORM,
    fake_model_stack,
    invoke_body,
    shared_model,
    switch,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

_VARYING = {"instance", "request_id"}
SHI = str(repository.SHI_PROVIDER_ID)


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
    tenant: UUID
    connection: str
    provider: str
    model: str


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side
    shared_model: str  # the SHI provider's model; A's switch is on, B's off
    platform: str


def _side(client: TestClient, tenant: UUID, slug: str, email: str) -> Side:
    base = org(slug)
    h = as_user(client, email)
    ids = fake_model_stack(client, h, base, conn_name=f"fake-{slug}", model_key=f"m-{slug}")
    return Side(base, email, tenant, ids["connection"], ids["provider"], ids["model"])


@pytest.fixture
def two(client: TestClient, world: World, people: People, app_db: Database) -> Two:
    b = _side(client, world.b.tenant, world.b.slug, people.owner_b)
    a = _side(client, world.a.tenant, world.a.slug, people.owner_a)
    h = as_user(client, people.platform)
    r = client.put(
        f"{PLATFORM}/shi-provider", headers=h, json={"adapter_key": "fake", "status": "active"}
    )
    assert r.status_code == 200, r.text
    model = shared_model(app_db)
    switch(client, people.platform, world.a.slug, True)
    return Two(a, b, model, people.platform)


def _rows(owner_db: Database, two: Two) -> tuple[Any, ...]:
    """B's rows and the shared rows, as the owner (every column)."""
    with owner_db.session() as o:
        providers = [
            tuple(r)
            for r in o.execute(
                text("SELECT * FROM shifde.model_providers WHERE id IN (:b, :s) ORDER BY id"),
                {"b": two.b.provider, "s": SHI},
            )
        ]
        models = [
            tuple(r)
            for r in o.execute(
                text("SELECT * FROM shifde.models WHERE id IN (:b, :s) ORDER BY id"),
                {"b": two.b.model, "s": two.shared_model},
            )
        ]
        access = [tuple(r) for r in o.execute(text("SELECT * FROM shifde.shi_provider_access"))]
        b_audit = o.execute(
            text(
                "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :b "
                "AND action LIKE 'model.%'"
            ),
            {"b": two.b.tenant},
        ).scalar_one()
    return (providers, models, access, b_audit)


# ------------------------------------------------------------------ route table
ROUTE_TESTS = {
    ("GET", "/api/v1/orgs/{org}/model-providers"): "test_a_lists_only_its_own_rows",
    ("POST", "/api/v1/orgs/{org}/model-providers"): "test_a_cannot_build_on_bs_rows",
    ("GET", "/api/v1/orgs/{org}/model-providers/{provider_id}"): (
        "test_a_cannot_reach_bs_provider"
    ),
    ("PATCH", "/api/v1/orgs/{org}/model-providers/{provider_id}"): (
        "test_a_cannot_reach_bs_provider"
    ),
    ("POST", "/api/v1/orgs/{org}/model-providers/{provider_id}/health-check"): (
        "test_a_cannot_reach_bs_provider"
    ),
    ("POST", "/api/v1/orgs/{org}/model-providers/{provider_id}/discover-models"): (
        "test_a_cannot_reach_bs_provider"
    ),
    ("GET", "/api/v1/orgs/{org}/models"): "test_a_lists_only_its_own_rows",
    ("POST", "/api/v1/orgs/{org}/models"): "test_a_cannot_build_on_bs_rows",
    ("GET", "/api/v1/orgs/{org}/models/{model_id}"): "test_a_cannot_reach_bs_model",
    ("PATCH", "/api/v1/orgs/{org}/models/{model_id}"): "test_a_cannot_reach_bs_model",
    ("GET", "/api/v1/orgs/{org}/shi-provider-access"): "test_each_client_reads_only_its_switch",
    ("GET", "/api/v1/platform/shi-provider"): "test_a_client_cannot_configure_the_shi_provider",
    ("PUT", "/api/v1/platform/shi-provider"): "test_a_client_cannot_configure_the_shi_provider",
    ("GET", "/api/v1/platform/orgs/{org}/shi-provider-access"): (
        "test_a_client_cannot_flip_any_switch"
    ),
    ("PUT", "/api/v1/platform/orgs/{org}/shi-provider-access"): (
        "test_a_client_cannot_flip_any_switch"
    ),
    # part 2b (B-1731); `test_bs_organization_is_unknown_to_a_on_the_2b_routes` covers B's slug
    ("GET", "/api/v1/orgs/{org}/model-allowlists"): "test_a_cannot_reach_bs_allowlists",
    ("PUT", "/api/v1/orgs/{org}/model-allowlists"): "test_a_cannot_reach_bs_allowlists",
    ("GET", "/api/v1/orgs/{org}/models/effective"): "test_a_lists_only_its_own_2b_rows",
    ("POST", "/api/v1/orgs/{org}/models/{model_id}/invoke"): "test_a_cannot_invoke_bs_model",
    ("GET", "/api/v1/orgs/{org}/model-routes"): "test_a_lists_only_its_own_2b_rows",
    ("POST", "/api/v1/orgs/{org}/model-routes"): "test_a_cannot_reach_or_name_bs_routes",
    ("GET", "/api/v1/orgs/{org}/model-routes/{route_id}"): "test_a_cannot_reach_or_name_bs_routes",
    ("PATCH", "/api/v1/orgs/{org}/model-routes/{route_id}"): (
        "test_a_cannot_reach_or_name_bs_routes"
    ),
}


def test_every_models_admin_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in models_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


# ------------------------------------------------------------------ organization
def test_bs_organization_is_unknown_to_a(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    provider = {"name": "x", "adapter_key": "fake", "connection_id": two.b.connection}
    model = {
        "provider_id": two.b.provider,
        "model_key": "x",
        "display_name": "x",
        "context_window": 1,
        "max_output_tokens": 1,
    }
    assert client.get(f"{two.a.base}/models/{two.a.model}", headers=h).status_code == 200
    for method, path, payload in (
        ("GET", "/model-providers", None),
        ("POST", "/model-providers", provider),
        ("GET", f"/model-providers/{two.b.provider}", None),
        ("PATCH", f"/model-providers/{two.b.provider}", {"name": "stolen"}),
        ("POST", f"/model-providers/{two.b.provider}/health-check", None),
        ("POST", f"/model-providers/{two.b.provider}/discover-models", None),
        ("GET", "/models", None),
        ("POST", "/models", model),
        ("GET", f"/models/{two.b.model}", None),
        ("PATCH", f"/models/{two.b.model}", {"display_name": "stolen"}),
        ("GET", "/shi-provider-access", None),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same(client.request(method, f"{two.b.base}{path}", headers=h, json=payload), missing)


# ------------------------------------------------------------------ lists
def test_a_lists_only_its_own_rows(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    providers = client.get(f"{two.a.base}/model-providers", headers=h).json()["items"]
    assert {p["id"] for p in providers} == {two.a.provider, SHI}  # A's switch is on
    models = client.get(f"{two.a.base}/models", headers=h).json()["items"]
    assert {m["id"] for m in models} == {two.a.model, two.shared_model}
    filtered = client.get(
        f"{two.a.base}/models", headers=h, params={"provider_id": two.b.provider}
    ).json()["items"]
    assert filtered == []
    hb = as_user(client, two.b.email)
    providers = client.get(f"{two.b.base}/model-providers", headers=hb).json()["items"]
    assert {p["id"] for p in providers} == {two.b.provider}  # B's switch is off
    models = client.get(f"{two.b.base}/models", headers=hb).json()["items"]
    assert {m["id"] for m in models} == {two.b.model}


# ------------------------------------------------------------------ B's objects under A's org
def test_a_cannot_reach_bs_provider(client: TestClient, owner_db: Database, two: Two) -> None:
    h = as_user(client, two.a.email)
    own = f"{two.a.base}/model-providers/{two.a.provider}"
    assert client.get(own, headers=h).status_code == 200
    assert client.post(f"{own}/health-check", headers=h).status_code == 200
    assert client.post(f"{own}/discover-models", headers=h).status_code == 200
    before = _rows(owner_db, two)
    for method, suffix, payload in (
        ("GET", "", None),
        ("PATCH", "", {"name": "stolen"}),
        ("POST", "/health-check", None),
        ("POST", "/discover-models", None),
    ):
        missing = client.request(
            method, f"{two.a.base}/model-providers/{uuid7()}{suffix}", headers=h, json=payload
        )
        theirs = client.request(
            method,
            f"{two.a.base}/model-providers/{two.b.provider}{suffix}",
            headers=h,
            json=payload,
        )
        _same(theirs, missing)
    assert _rows(owner_db, two) == before


def test_a_cannot_reach_bs_model(client: TestClient, owner_db: Database, two: Two) -> None:
    h = as_user(client, two.a.email)
    own = client.get(f"{two.a.base}/models/{two.a.model}", headers=h)
    assert own.status_code == 200
    patched = client.patch(
        f"{two.a.base}/models/{two.a.model}", headers=h, json={"display_name": "mine"}
    )
    assert patched.status_code == 200, patched.text
    before = _rows(owner_db, two)
    for method, payload in (("GET", None), ("PATCH", {"display_name": "stolen"})):
        missing = client.request(method, f"{two.a.base}/models/{uuid7()}", headers=h, json=payload)
        theirs = client.request(
            method, f"{two.a.base}/models/{two.b.model}", headers=h, json=payload
        )
        _same(theirs, missing)
    assert _rows(owner_db, two) == before


def test_a_cannot_build_on_bs_rows(client: TestClient, owner_db: Database, two: Two) -> None:
    h = as_user(client, two.a.email)
    before = _rows(owner_db, two)

    def provider(connection: str, name: str) -> httpx.Response:
        body = {"name": name, "adapter_key": "fake", "connection_id": connection}
        return client.post(f"{two.a.base}/model-providers", headers=h, json=body)

    def model(provider_id: str, key: str) -> httpx.Response:
        body = {
            "provider_id": provider_id,
            "model_key": key,
            "display_name": key,
            "context_window": 10,
            "max_output_tokens": 10,
        }
        return client.post(f"{two.a.base}/models", headers=h, json=body)

    assert provider(two.a.connection, "own-second").status_code == 201
    assert model(two.a.provider, "own-second").status_code == 201
    _same(provider(two.b.connection, "steal"), provider(str(uuid7()), "steal"))
    _same(model(two.b.provider, "steal"), model(str(uuid7()), "steal"))
    assert _rows(owner_db, two) == before


# ------------------------------------------------------------------ the SHI-wide provider
def test_no_client_writes_the_shi_provider_or_its_models(
    client: TestClient, owner_db: Database, two: Two
) -> None:
    """A (switch on) sees the shared rows read-only: every write is 403. B (switch off) does not
    see them: every route answers as for an unknown id. Nothing changes either way."""
    shi_model = {
        "provider_id": SHI,
        "model_key": "client-made",
        "display_name": "x",
        "context_window": 10,
        "max_output_tokens": 10,
    }
    attempts: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
        ("PATCH", f"/model-providers/{SHI}", {"status": "disabled"}),
        ("POST", f"/model-providers/{SHI}/health-check", None),
        ("POST", f"/model-providers/{SHI}/discover-models", None),
        ("POST", "/models", shi_model),
        ("PATCH", f"/models/{two.shared_model}", {"lifecycle": "retired"}),
    )
    before = _rows(owner_db, two)
    h = as_user(client, two.a.email)
    got = client.get(f"{two.a.base}/model-providers/{SHI}", headers=h)
    assert got.status_code == 200 and got.json()["connection_id"] is None
    assert got.json()["settings"] == {}
    assert client.get(f"{two.a.base}/models/{two.shared_model}", headers=h).status_code == 200
    for method, path, payload in attempts:
        r = client.request(method, f"{two.a.base}{path}", headers=h, json=payload)
        assert r.status_code == 403, (method, path, r.text)
    hb = as_user(client, two.b.email)
    for method, path, payload in (
        ("GET", f"/model-providers/{SHI}", None),
        ("GET", f"/models/{two.shared_model}", None),
        *attempts,
    ):
        unknown = path.replace(SHI, str(uuid7())).replace(two.shared_model, str(uuid7()))
        body = (
            {**payload, "provider_id": str(uuid7())} if path == "/models" and payload else payload
        )
        missing = client.request(method, f"{two.b.base}{unknown}", headers=hb, json=body)
        _same(client.request(method, f"{two.b.base}{path}", headers=hb, json=payload), missing)
    assert _rows(owner_db, two) == before


def test_each_client_reads_only_its_switch(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    mine = client.get(f"{two.a.base}/shi-provider-access", headers=h)
    assert mine.status_code == 200 and mine.json()["enabled"] is True, mine.text
    assert set(mine.json()) == {"enabled", "changed_at", "row_version"}  # no person outside A
    hb = as_user(client, two.b.email)
    theirs = client.get(f"{two.b.base}/shi-provider-access", headers=hb)
    assert theirs.status_code == 200
    assert theirs.json() == {"enabled": False, "changed_at": None, "row_version": None}


def test_a_client_cannot_configure_the_shi_provider(
    client: TestClient, owner_db: Database, two: Two
) -> None:
    hp = as_user(client, two.platform)
    assert client.get(f"{PLATFORM}/shi-provider", headers=hp).status_code == 200
    before = _rows(owner_db, two)
    h = as_user(client, two.a.email)  # A's owner
    assert client.get(f"{PLATFORM}/shi-provider", headers=h).status_code == 403
    r = client.put(
        f"{PLATFORM}/shi-provider", headers=h, json={"adapter_key": "fake", "status": "disabled"}
    )
    assert r.status_code == 403, r.text
    assert _rows(owner_db, two) == before


def test_a_client_cannot_flip_any_switch(
    client: TestClient, owner_db: Database, world: World, two: Two
) -> None:
    hp = as_user(client, two.platform)
    url = f"{PLATFORM}/orgs/{world.a.slug}/shi-provider-access"
    assert client.get(url, headers=hp).json()["enabled"] is True
    before = _rows(owner_db, two)
    unknown = f"no-such-{uuid7().hex[-12:]}"
    for email in (two.a.email, two.b.email):
        h = as_user(client, email)
        for slug in (world.a.slug, world.b.slug, unknown):  # own, the other, none
            target = f"{PLATFORM}/orgs/{slug}/shi-provider-access"
            got = client.get(target, headers=h)
            put = client.put(target, headers=h, json={"enabled": True})
            assert (got.status_code, put.status_code) == (403, 403), (email, slug, put.text)
            assert put.json()["detail"] == got.json()["detail"]  # nothing said about `slug`
    assert _rows(owner_db, two) == before


# ------------------------------------------------------------------ part 2b routes
def _b_route_and_allowlist(client: TestClient, two: Two) -> str:
    """B's owner gives B a route and an enabled tenant allowlist; returns the route id."""
    hb = as_user(client, two.b.email)
    r = client.put(
        f"{two.b.base}/model-allowlists",
        headers=hb,
        params={"scope_type": "tenant", "scope_id": str(two.b.tenant)},
        json={"enabled": True, "model_ids": [two.b.model]},
    )
    assert r.status_code == 200, r.text
    route = client.post(
        f"{two.b.base}/model-routes",
        headers=hb,
        json={
            "name": "b-route",
            "scope": {"type": "tenant", "id": str(two.b.tenant)},
            "primary_model_id": two.b.model,
        },
    )
    assert route.status_code == 201, route.text
    return str(route.json()["id"])


def _route_body(two: Two, primary: str, fallbacks: list[str], name: str) -> dict[str, Any]:
    return {
        "name": name,
        "scope": {"type": "tenant", "id": str(two.a.tenant)},
        "primary_model_id": primary,
        "fallback_model_ids": fallbacks,
    }


def _b_workspace(owner_db: Database, two: Two) -> UUID:
    with owner_db.session() as o:
        return UUID(
            str(
                o.execute(
                    text("SELECT id FROM shifde.workspaces WHERE tenant_id = :b LIMIT 1"),
                    {"b": two.b.tenant},
                ).scalar_one()
            )
        )


def test_bs_organization_is_unknown_to_a_on_the_2b_routes(client: TestClient, two: Two) -> None:
    b_route = _b_route_and_allowlist(client, two)
    h = as_user(client, two.a.email)
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    scope = f"scope_type=tenant&scope_id={two.b.tenant}"
    route = _route_body(two, two.b.model, [], "x")
    for method, path, payload in (
        ("GET", f"/model-allowlists?{scope}", None),
        ("PUT", f"/model-allowlists?{scope}", {"enabled": False, "model_ids": []}),
        ("GET", "/models/effective", None),
        ("POST", f"/models/{two.b.model}/invoke", invoke_body()),
        ("GET", "/model-routes", None),
        ("POST", "/model-routes", route),
        ("GET", f"/model-routes/{b_route}", None),
        ("PATCH", f"/model-routes/{b_route}", {"name": "stolen"}),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same(client.request(method, f"{two.b.base}{path}", headers=h, json=payload), missing)


def test_a_lists_only_its_own_2b_rows(client: TestClient, two: Two) -> None:
    _b_route_and_allowlist(client, two)
    h = as_user(client, two.a.email)
    own = client.post(
        f"{two.a.base}/model-routes", headers=h, json=_route_body(two, two.a.model, [], "a-route")
    )
    assert own.status_code == 201, own.text
    routes = client.get(f"{two.a.base}/model-routes", headers=h).json()["items"]
    assert [r["id"] for r in routes] == [own.json()["id"]]
    effective = client.get(f"{two.a.base}/models/effective", headers=h).json()["items"]
    assert {m["id"] for m in effective} == {two.a.model, two.shared_model}  # A's switch is on
    hb = as_user(client, two.b.email)
    effective = client.get(f"{two.b.base}/models/effective", headers=hb).json()["items"]
    assert {m["id"] for m in effective} == {two.b.model}  # B's switch is off: no SHI model


def test_a_cannot_reach_bs_allowlists(client: TestClient, owner_db: Database, two: Two) -> None:
    _b_route_and_allowlist(client, two)
    b_ws = _b_workspace(owner_db, two)
    h = as_user(client, two.a.email)
    url = f"{two.a.base}/model-allowlists"
    own = {"scope_type": "tenant", "scope_id": str(two.a.tenant)}
    assert client.get(url, headers=h, params=own).status_code == 200
    put = client.put(url, headers=h, params=own, json={"enabled": False, "model_ids": []})
    assert put.status_code == 200, put.text
    before = _rows(owner_db, two)
    body = {"enabled": True, "model_ids": [two.a.model]}
    for scope_type, theirs_id in (("tenant", two.b.tenant), ("workspace", b_ws)):
        for method, payload in (("GET", None), ("PUT", body)):
            missing = client.request(
                method,
                url,
                headers=h,
                params={"scope_type": scope_type, "scope_id": str(uuid7())},
                json=payload,
            )
            theirs = client.request(
                method,
                url,
                headers=h,
                params={"scope_type": scope_type, "scope_id": str(theirs_id)},
                json=payload,
            )
            _same(theirs, missing)
    # naming B's model or a shared SHI model (B-1733) is refused like an unknown model id
    for model in (two.b.model, two.shared_model):
        missing = client.put(
            url, headers=h, params=own, json={"enabled": True, "model_ids": [str(uuid7())]}
        )
        theirs = client.put(
            url, headers=h, params=own, json={"enabled": True, "model_ids": [model]}
        )
        _same(theirs, missing, 422)
    assert _rows(owner_db, two) == before


def test_a_cannot_invoke_bs_model(client: TestClient, owner_db: Database, two: Two) -> None:
    h = as_user(client, two.a.email)
    own = client.post(f"{two.a.base}/models/{two.a.model}/invoke", headers=h, json=invoke_body())
    assert own.status_code == 200, own.text
    before = _rows(owner_db, two)
    missing = client.post(f"{two.a.base}/models/{uuid7()}/invoke", headers=h, json=invoke_body())
    theirs = client.post(f"{two.a.base}/models/{two.b.model}/invoke", headers=h, json=invoke_body())
    _same(theirs, missing)
    assert _rows(owner_db, two) == before
    with owner_db.session() as o:
        b_usage = o.execute(
            text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :b"),
            {"b": two.b.tenant},
        ).scalar_one()
    assert b_usage == 0


def test_a_cannot_reach_or_name_bs_routes(client: TestClient, owner_db: Database, two: Two) -> None:
    b_route = _b_route_and_allowlist(client, two)
    h = as_user(client, two.a.email)
    created = client.post(
        f"{two.a.base}/model-routes", headers=h, json=_route_body(two, two.a.model, [], "mine")
    )
    assert created.status_code == 201, created.text
    own = f"{two.a.base}/model-routes/{created.json()['id']}"
    assert client.get(own, headers=h).status_code == 200
    assert client.patch(own, headers=h, json={"name": "mine-2"}).status_code == 200
    before = _rows(owner_db, two)
    for method, payload in (("GET", None), ("PATCH", {"name": "stolen"})):
        missing = client.request(
            method, f"{two.a.base}/model-routes/{uuid7()}", headers=h, json=payload
        )
        theirs = client.request(
            method, f"{two.a.base}/model-routes/{b_route}", headers=h, json=payload
        )
        _same(theirs, missing)
    # B's model or a shared SHI model as the primary or any fallback: an unknown model's 422
    unknown = str(uuid7())
    for name, (primary, fallbacks), (ref_primary, ref_fallbacks) in (
        ("p-b", (two.b.model, []), (unknown, [])),
        ("f-b", (two.a.model, [two.b.model]), (two.a.model, [unknown])),
        ("p-shi", (two.shared_model, []), (unknown, [])),
        ("f-shi", (two.a.model, [two.shared_model]), (two.a.model, [unknown])),
    ):
        missing = client.post(
            f"{two.a.base}/model-routes",
            headers=h,
            json=_route_body(two, ref_primary, ref_fallbacks, name),
        )
        theirs = client.post(
            f"{two.a.base}/model-routes",
            headers=h,
            json=_route_body(two, primary, fallbacks, name),
        )
        _same(theirs, missing, 422)
        patched_missing = client.patch(
            own,
            headers=h,
            json={"primary_model_id": ref_primary, "fallback_model_ids": ref_fallbacks},
        )
        patched = client.patch(
            own, headers=h, json={"primary_model_id": primary, "fallback_model_ids": fallbacks}
        )
        _same(patched, patched_missing, 422)
    assert _rows(owner_db, two) == before
    with owner_db.session() as o:
        b_routes = o.execute(
            text(
                "SELECT name, primary_model_id, fallback_model_ids FROM shifde.model_routes "
                "WHERE tenant_id = :b"
            ),
            {"b": two.b.tenant},
        ).all()
    assert [tuple(r) for r in b_routes] == [("b-route", UUID(two.b.model), [])]


def test_a_provider_naming_bs_private_vendor_is_answered_like_an_unknown_vendor(
    client: TestClient, owner_db: Database, two: Two
) -> None:
    """Isolation review 2 item 3: B's private vendor, active or disabled, is an unknown vendor
    id to A on `POST /model-providers`."""
    with owner_db.session() as o:
        active, disabled = owner_vendor(o, two.b.tenant), owner_vendor(o, two.b.tenant)
        o.execute(
            text("UPDATE shifde.vendors SET status = 'disabled' WHERE id = :i"), {"i": disabled}
        )
        o.commit()
    h = as_user(client, two.a.email)
    url = f"{two.a.base}/model-providers"

    def body(vendor: object, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "adapter_key": "fake",
            "connection_id": two.a.connection,
            "vendor_id": str(vendor),
        }

    before = _rows(owner_db, two)
    for n, vendor in enumerate((active, disabled)):
        missing = client.post(url, headers=h, json=body(uuid7(), f"v-{n}"))
        assert missing.status_code in (404, 422), missing.text
        theirs = client.post(url, headers=h, json=body(vendor, f"v-{n}"))
        _same(theirs, missing, missing.status_code)
    assert _rows(owner_db, two) == before
