"""The mounted route table, the branding exclusion, and two seeded organizations seen through the
mounted API (plan row 19). The OpenID Connect routes (B-2263), the runtime routes and
`GET /api/v1/platform/status` (APP-2, B-2400, B-2401) are mounted."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World, fresh_world, full_client, login

SNAPSHOT = json.loads((Path(__file__).parent / "seth_routes_3ba0e06.json").read_text("utf-8"))

OIDC = {("GET", "/api/auth/oidc/{idp_id}/login"), ("GET", "/api/auth/oidc/{idp_id}/callback")}
# mounted with the runtime port (APP-2, B-2401; was B-1020)
PLATFORM_STATUS = {("GET", "/api/v1/platform/status")}
# unified addition (kept from the skeleton, B-2231)
HEALTHZ = {("GET", "/healthz")}
# Groups are platform-level in this build (identity_0005, B-928): SETH's per-organization group
# writes are gone and platform-administrator routes replace them; the SHI provider routes are
# models_admin's shared provider (B-1711..B-1717).
SETH_GROUP_WRITES = {
    ("PATCH", "/api/v1/orgs/{org}/groups/{group_id}"),
    ("POST", "/api/v1/orgs/{org}/groups"),
    ("PUT", "/api/v1/orgs/{org}/groups/{group_id}/members"),
}
UNIFIED_ONLY = {
    ("GET", "/api/v1/orgs/{org}/shi-provider-access"),
    ("GET", "/api/v1/platform/groups"),
    ("GET", "/api/v1/platform/groups/{group_id}"),
    ("GET", "/api/v1/platform/orgs/{org}/shi-provider-access"),
    ("GET", "/api/v1/platform/shi-provider"),
    ("PATCH", "/api/v1/platform/groups/{group_id}"),
    ("POST", "/api/v1/platform/groups"),
    ("POST", "/api/v1/platform/orgs/{org}/group-bindings"),
    ("POST", "/api/v1/platform/orgs/{org}/suspend"),  # F-03, B-2511
    ("POST", "/api/v1/platform/orgs/{org}/unsuspend"),
    ("PUT", "/api/v1/platform/groups/{group_id}/members"),
    ("PUT", "/api/v1/platform/orgs/{org}/shi-provider-access"),
    ("PUT", "/api/v1/platform/shi-provider"),
    ("PUT", "/api/v1/orgs/{org}/capture-content"),  # J-M2, B-2550
    # ARCH M2 (issue #10, B-2610): the connection test runs on the worker; its job's status
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}/test-jobs/{job_id}"),
}
# native delivery (design 2026-11-17 section 9 item 2, B-2501): not a SETH module
EXECUTOR_CREDENTIAL = (
    "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials/{credential_id}"
)
DELIVERY = {
    ("GET", "/api/v1/orgs/{org}/delivery/targets"),
    ("GET", "/api/v1/orgs/{org}/delivery/targets/{target_id}"),
    ("POST", "/api/v1/orgs/{org}/delivery/targets"),
    ("POST", "/api/v1/orgs/{org}/delivery/targets/{target_id}/archive"),
    ("GET", "/api/v1/orgs/{org}/delivery/executors"),
    ("POST", "/api/v1/orgs/{org}/delivery/executors"),
    ("GET", "/api/v1/orgs/{org}/delivery/executors/{executor_id}"),
    ("PATCH", "/api/v1/orgs/{org}/delivery/executors/{executor_id}"),
    ("GET", "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials"),
    ("POST", "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials"),
    ("POST", EXECUTOR_CREDENTIAL + "/rotate"),
    ("POST", EXECUTOR_CREDENTIAL + "/revoke"),
    # the executor gateway (design 5, B-2664): executor tokens only, no user session
    ("GET", "/api/executor/v1/whoami"),
    # the job queue (B-2669): lease, then per-job calls under the lease proof
    ("POST", "/api/executor/v1/lease"),
    ("POST", "/api/executor/v1/jobs/{job_id}/heartbeat"),
    ("GET", "/api/executor/v1/jobs/{job_id}/artifact"),
    ("POST", "/api/executor/v1/jobs/{job_id}/credential-grant"),
    ("POST", "/api/executor/v1/jobs/{job_id}/checkpoint"),
    ("POST", "/api/executor/v1/jobs/{job_id}/result"),
}


def _pairs(rows: list[list[str]]) -> set[tuple[str, str]]:
    return {(m, p) for m, p in rows}


def _mounted(client: TestClient) -> set[tuple[str, str]]:
    paths = client.get("/api/openapi.json").json()["paths"]
    return {(m.upper(), p) for p, ops in paths.items() for m in ops}


def test_the_mounted_route_table_is_sethss_minus_branding(
    client: TestClient,
) -> None:
    seth = _pairs(SNAPSHOT["all"])
    runtime, branding = _pairs(SNAPSHOT["runtime"]), _pairs(SNAPSHOT["branding"])
    assert runtime and branding and seth >= OIDC | PLATFORM_STATUS | SETH_GROUP_WRITES
    expected = (seth - branding - SETH_GROUP_WRITES) | HEALTHZ | UNIFIED_ONLY | DELIVERY
    mounted = _mounted(client)
    assert mounted - expected == set(), "mounted but not in SETH"
    assert expected - mounted == set(), "in SETH but not mounted"
    assert mounted >= OIDC  # B-2263: the OpenID Connect routes are SETH's
    assert mounted >= runtime | PLATFORM_STATUS  # APP-2: SETH's 16 runtime routes and status
    assert len(mounted) > 250


def test_oidc_routes_are_mounted_and_an_unknown_idp_is_the_generic_failure(
    client: TestClient,
) -> None:
    # B-2263 (was B-2232): identity review 4 item 5 (B-2260) and the IdP-sync self-add audit
    # (B-1624, B-2262) are closed; an unknown IdP id answers the one generic sign-in failure
    for path in ("login", "callback"):
        r = client.get(
            f"/api/auth/oidc/00000000-0000-0000-0000-000000000000/{path}",
            params={"code": "c", "state": "s"} if path == "callback" else None,
            follow_redirects=False,
        )
        assert r.status_code == 302, (path, r.status_code)
        assert r.headers["location"] == "/login?error=oidc_failed"
    assert client.get("/api/auth/config").status_code == 200


def test_every_mounted_v1_route_is_behind_authentication(client: TestClient) -> None:
    for method, path in sorted(_mounted(client)):
        if not path.startswith("/api/v1"):
            continue
        url = path.format_map(_Any())
        r = client.request(method, url, json={} if method != "GET" else None)
        assert r.status_code == 401, (method, path, r.status_code)


class _Any(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "x" if key in {"org", "ws", "proj"} else "00000000-0000-0000-0000-000000000000"


def test_limiters_and_body_cap_are_wired(settings: Settings, world: World) -> None:
    with full_client(settings.model_copy(update={"max_request_body_bytes": 100})) as c:
        state = c.app.state  # type: ignore[attr-defined]  # reason: TestClient.app is typed ASGI
        for name in ("login_ip_limiter", "login_account_limiter", "api_key_failure_limiter"):
            assert hasattr(state, name), name
        assert hasattr(state, "oidc_limiter") and hasattr(state, "oidc") and state.features.keys()
        r = c.post("/api/auth/local/login", content=b"x" * 500)
        assert r.status_code == 413 and r.headers["x-request-id"]


def test_two_seeded_organizations_with_separate_groups_see_only_themselves(
    settings: Settings, owner_session: Session
) -> None:
    world = fresh_world(owner_session, with_groups=True)
    a, b = "demo-a", "demo-b"
    with full_client(settings) as c:
        seen: dict[str, dict[str, list[str]]] = {}
        for slug in (a, b):
            for who in (f"admin@{slug}.local", f"staff@{slug}.local"):
                login(c, world, who)
                orgs = [o["slug"] for o in c.get("/api/v1/orgs").json()["items"]]
                groups = c.get(f"/api/v1/orgs/{slug}/groups")
                other = b if slug == a else a
                # the other organization is unknown to this person, on every kind of route
                assert c.get(f"/api/v1/orgs/{other}").status_code == 404, who
                assert c.get(f"/api/v1/orgs/{other}/workspaces").status_code == 404, who
                assert c.get(f"/api/v1/orgs/{other}/groups").status_code == 404, who
                # a viewer-level staff person may not read groups (403); an administrator may
                names = [g["name"] for g in groups.json().get("items", [])]
                assert groups.status_code == (200 if who.startswith("admin") else 403), who
                seen[who] = {"orgs": orgs, "groups": names}
    for slug in (a, b):
        for who in (f"admin@{slug}.local", f"staff@{slug}.local"):
            assert seen[who]["orgs"] == [slug], who
        # the group bound in this organization is listed here and the other one's never is
        assert seen[f"admin@{slug}.local"]["groups"] == [f"Staff of {slug}"]
