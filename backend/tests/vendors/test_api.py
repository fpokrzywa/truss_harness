"""Vendor catalog and adapter routes: SETH 3ba0e06 `tests/test_p3_models.py` (vendor part), moved.

SETH's seeded world (`demo-a`/`demo-b`, `ADMIN`, `VIEWER`, `PLATFORM`) becomes the tenancy test
world and its people (`tests/tenancy/routes.py`: `owner_a`, `viewer_a`, `owner_b`, `platform`);
the assertions are SETH's (DECISIONS B-1303). The model-registry tests of that file belong to
models_admin; the vendor-status gate tests of `test_p3_connections.py` to connections.
"""

from __future__ import annotations

from typing import Any  # Any: JSON response bodies

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters import evidence
from shifde.core.clock import utcnow
from tests.identity.harness import login
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _post(
    client: TestClient, url: str, h: dict[str, str], body: dict[str, Any], expected: int = 201
) -> dict[str, Any]:
    r = client.post(url, json=body, headers=h)
    assert r.status_code == expected, r.text
    out: dict[str, Any] = r.json()
    return out


def _caps(client: TestClient, base: str, vendor_key: str) -> dict[str, Any]:
    vendors = client.get(f"{base}/vendors?limit=200").json()["items"]
    vid = next(v["id"] for v in vendors if v["key"] == vendor_key)
    items = client.get(f"{base}/vendors/{vid}/capabilities").json()["items"]
    return {i["capability"]: i for i in items}


def _login(client: TestClient, email: str) -> dict[str, str]:
    """Sign in as `email` (a new session replaces the previous one); the CSRF header."""
    client.cookies.clear()
    return login(client, email)


def test_global_seeds_and_a_tenant_private_vendor(
    client: TestClient, world: World, people: People
) -> None:
    a_org, b_org = f"/api/v1/orgs/{world.a.slug}", f"/api/v1/orgs/{world.b.slug}"
    h = _login(client, people.owner_a)
    listing = client.get(f"{a_org}/vendors?limit=200")
    assert listing.status_code == 200 and listing.json()["next_cursor"] is None
    by_key = {v["key"]: v for v in listing.json()["items"]}
    assert {
        "databricks",
        "snowflake",
        "microsoft-fabric",
        "postgresql",
        "generic-rest",
        "generic-mcp",
        "openai-compatible",
        "anthropic",
    } <= set(by_key)
    assert all(v["is_global"] and v["tenant_id"] is None for v in by_key.values())
    assert by_key["databricks"]["category"] == "data_platform"
    assert not any("client" in v["name"].lower() for v in by_key.values())  # no client names
    only_models = client.get(f"{a_org}/vendors?category=model_provider").json()["items"]
    assert {v["key"] for v in only_models} == {"openai-compatible", "anthropic"}

    created = _post(
        client,
        f"{a_org}/vendors",
        h,
        {
            "key": "acme-crm",
            "name": "Acme CRM",
            "category": "saas_api",
            "version": "2",
            "configuration_requirements": ["base URL"],
            "adapter_keys": ["rest.http"],
        },
    )
    assert created["is_global"] is False and created["tenant_id"] is not None
    assert created["adapter_keys"] == ["rest.http"] and created["status"] == "active"
    patched = client.patch(
        f"{a_org}/vendors/{created['id']}",
        json={"description": "CRM", "row_version": created["row_version"]},
        headers=h,
    )
    assert patched.status_code == 200 and patched.json()["description"] == "CRM"
    stale = client.patch(
        f"{a_org}/vendors/{created['id']}", json={"name": "x", "row_version": 1}, headers=h
    )
    assert stale.status_code == 409
    status = client.post(
        f"{a_org}/vendors/{created['id']}/status",
        json={"status": "disabled", "row_version": patched.json()["row_version"]},
        headers=h,
    )
    assert status.status_code == 200 and status.json()["status"] == "disabled"
    assert _post(
        client,
        f"{a_org}/vendors",
        h,
        {"key": "acme-crm", "name": "dup", "category": "other"},
        409,
    )
    bad = client.post(
        f"{a_org}/vendors",
        headers=h,
        json={"key": "x-y", "name": "n", "category": "other", "adapter_keys": ["nope"]},
    )
    assert bad.status_code == 422
    # private vendors are invisible to other tenants
    _login(client, people.owner_b)
    assert client.get(f"{b_org}/vendors/{created['id']}").status_code == 404
    assert "acme-crm" not in {v["key"] for v in client.get(f"{b_org}/vendors").json()["items"]}


def test_vendor_permissions_and_global_catalog_is_read_only_for_tenants(
    client: TestClient, world: World, people: People
) -> None:
    a_org = f"/api/v1/orgs/{world.a.slug}"
    hv = _login(client, people.viewer_a)
    assert client.get(f"{a_org}/vendors").status_code == 200  # vendor:read
    body = {"key": "mine", "name": "Mine", "category": "other"}
    assert client.post(f"{a_org}/vendors", json=body, headers=hv).status_code == 403
    assert client.post("/api/v1/platform/vendors", json=body, headers=hv).status_code == 403
    ha = _login(client, people.owner_a)
    gid = next(
        v["id"]
        for v in client.get(f"{a_org}/vendors?limit=200").json()["items"]
        if v["key"] == "snowflake"
    )
    assert (
        client.patch(f"{a_org}/vendors/{gid}", json={"name": "Hacked"}, headers=ha).status_code
        == 403
    )
    assert (
        client.post(
            f"{a_org}/vendors/{gid}/status", json={"status": "retired"}, headers=ha
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/v1/adapters").status_code == 401  # authentication required


def test_platform_admin_manages_the_global_catalog(
    client: TestClient, world: World, people: People
) -> None:
    a_org = f"/api/v1/orgs/{world.a.slug}"
    h = _login(client, people.platform)
    created = _post(
        client,
        "/api/v1/platform/vendors",
        h,
        {"key": "global-extra", "name": "Global Extra", "category": "other"},
    )
    assert created["is_global"] and created["tenant_id"] is None
    listing = client.get("/api/v1/platform/vendors?limit=200").json()["items"]
    assert "global-extra" in {v["key"] for v in listing} and all(v["is_global"] for v in listing)
    moved = client.patch(
        f"/api/v1/platform/vendors/{created['id']}",
        json={"status": "retired", "row_version": created["row_version"]},
        headers=h,
    )
    assert moved.status_code == 200 and moved.json()["status"] == "retired"
    _login(client, people.owner_a)
    seen = {v["key"]: v for v in client.get(f"{a_org}/vendors?limit=200").json()["items"]}
    assert seen["global-extra"]["status"] == "retired"  # tenants see the global change
    assert client.get("/api/v1/platform/vendors").status_code == 403


def test_capabilities_come_from_the_registry_and_recorded_evidence(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a_org = f"/api/v1/orgs/{world.a.slug}"
    _login(client, people.owner_a)
    owner_session.execute(text("TRUNCATE shifde.adapter_conformance_runs"))
    owner_session.commit()
    for key in ("databricks", "snowflake", "microsoft-fabric"):
        caps = _caps(client, a_org, key)
        assert caps and {c["maturity"] for c in caps.values()} == {"planned"}, key
        assert all(c["adapter_key"] is None and c["evidence"] is None for c in caps.values())
    pg = _caps(client, a_org, "postgresql")
    assert pg["query.sql"]["maturity"] == "experimental"  # implemented, no evidence yet
    assert pg["query.pushdown_join"]["maturity"] == "planned"  # declared, not built
    adapters = {a["key"]: a for a in client.get("/api/v1/adapters").json()["items"]}
    assert {a["maturity"] for a in adapters["databricks"]["capabilities"]} == {"planned"}
    assert adapters["postgres"]["connection_type"] == "database"
    assert adapters["rest.http"]["secret_fields"] and "config_schema" in adapters["rest.http"]

    run = evidence.ConformanceRun(
        adapter_key="postgres",
        adapter_version="1.0.0",
        suite_version="1",
        started_at=utcnow(),
        finished_at=utcnow(),
        outcome="pass",
        validated_capabilities=("query.sql", "discover.schema"),
    )
    try:
        evidence.record_run(owner_session, run)
        owner_session.commit()
        pg = _caps(client, a_org, "postgresql")
        assert pg["query.sql"]["maturity"] == "validated"
        assert set(pg["query.sql"]["evidence"]) == {"run_id", "finished_at", "suite_version"}
        assert pg["query.sql"]["evidence"]["run_id"] == str(run.id)
        assert pg["query.pushdown_join"]["maturity"] == "planned"
        runs = client.get("/api/v1/adapters/postgres/conformance-runs").json()["items"]
        assert [r["id"] for r in runs] == [str(run.id)] and runs[0]["outcome"] == "pass"
        # a failing run is newer: the adapter is demoted
        bad = evidence.ConformanceRun(
            adapter_key="postgres",
            adapter_version="1.0.0",
            suite_version="1",
            started_at=utcnow(),
            finished_at=utcnow(),
            outcome="fail",
        )
        evidence.record_run(owner_session, bad)
        owner_session.commit()
        assert _caps(client, a_org, "postgresql")["query.sql"]["maturity"] == "experimental"
        assert client.get("/api/v1/adapters/nope/conformance-runs").status_code == 404
    finally:
        owner_session.execute(text("TRUNCATE shifde.adapter_conformance_runs"))
        owner_session.commit()


def test_maturity_cannot_be_set_through_the_api(
    client: TestClient, world: World, people: People
) -> None:
    a_org = f"/api/v1/orgs/{world.a.slug}"
    h = _login(client, people.owner_a)
    forged = {"key": "forge", "name": "Forge", "category": "other"}
    for extra in (
        {"maturity": "validated"},
        {"capabilities": [{"capability": "query.sql", "maturity": "validated"}]},
        {"evidence": {"run_id": "x"}},
    ):
        assert (
            client.post(f"{a_org}/vendors", json={**forged, **extra}, headers=h).status_code == 422
        )
    vendor = _post(client, f"{a_org}/vendors", h, forged)
    for extra in ({"maturity": "validated"}, {"capabilities": []}):
        r = client.patch(
            f"{a_org}/vendors/{vendor['id']}",
            json={**extra, "row_version": vendor["row_version"]},
            headers=h,
        )
        assert r.status_code == 422
    for path in ("/api/v1/adapters", "/api/v1/adapters/postgres/conformance-runs"):
        for method in ("post", "put", "patch", "delete"):
            r = getattr(client, method)(path, headers=h)
            assert r.status_code in (404, 405), (method, path)  # no write route exists
    ha = _login(client, people.platform)
    assert (
        client.post(
            "/api/v1/platform/vendors",
            headers=ha,
            json={"key": "f2", "name": "f", "category": "other", "maturity": "validated"},
        ).status_code
        == 422
    )
