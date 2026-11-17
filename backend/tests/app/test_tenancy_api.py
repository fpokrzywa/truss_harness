"""Tenancy API: isolation, authorization, archive semantics, dashboard, platform endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import __version__
from shifde.core.settings import Settings
from tests._support.fullapp import World, login
from tests.identity import oidc_flow

ADMIN_A = "admin@demo-a.local"


VIEWER_A = "viewer@demo-a.local"


ADMIN_B = "admin@demo-b.local"


PLATFORM = "platform@shifde.local"


WS = "/api/v1/orgs/demo-a/workspaces"


def test_admin_creates_workspace_and_project_with_audit(
    client: TestClient, world: World, owner_session: Session
) -> None:
    headers = login(client, world, ADMIN_A)
    r = client.post(WS, json={"slug": "eng", "name": "Eng", "description": "d"}, headers=headers)
    assert r.status_code == 201
    ws = r.json()
    assert ws["status"] == "active" and ws["row_version"] == 1 and ws["description"] == "d"
    p = client.post(f"{WS}/eng/projects", json={"slug": "api", "name": "API"}, headers=headers)
    assert p.status_code == 201 and p.json()["workspace_slug"] == "eng"
    actions = set(
        owner_session.execute(text("SELECT action FROM shifde.audit_events")).scalars().all()
    )
    assert {"tenancy.workspace.created", "tenancy.project.created"} <= actions


def test_validation_and_conflicts(client: TestClient, world: World) -> None:
    headers = login(client, world, ADMIN_A)
    assert (
        client.post(WS, json={"slug": "Bad_Slug", "name": "x"}, headers=headers).status_code == 422
    )
    assert (
        client.post(WS, json={"slug": "main", "name": "dup"}, headers=headers).json()["code"]
        == "conflict"
    )
    assert (
        client.post(WS, json={"slug": "ok", "name": "x", "extra": 1}, headers=headers).status_code
        == 422
    )
    stale = client.patch(f"{WS}/main", json={"name": "New", "row_version": 99}, headers=headers)
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"
    ok = client.patch(f"{WS}/main", json={"name": "New", "row_version": 1}, headers=headers)
    assert ok.status_code == 200 and ok.json()["name"] == "New" and ok.json()["row_version"] == 2
    assert ok.json()["updated_at"] is not None
    assert client.patch(f"{WS}/main", json={"row_version": 2}, headers=headers).status_code == 422


def test_archived_workspace_is_read_only_hidden_by_default_and_restorable(
    client: TestClient, world: World, owner_session: Session
) -> None:
    headers = login(client, world, ADMIN_A)
    arch = client.patch(
        f"{WS}/main", json={"status": "archived", "row_version": 1}, headers=headers
    )
    assert arch.status_code == 200 and arch.json()["status"] == "archived"
    assert client.get(WS).json()["items"] == []
    assert [w["slug"] for w in client.get(f"{WS}?include_archived=true").json()["items"]] == [
        "main"
    ]
    assert client.get(f"{WS}/main").status_code == 200  # still readable
    # mutations rejected
    r = client.post(f"{WS}/main/projects", json={"slug": "p2", "name": "P2"}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "scope_archived"
    r = client.patch(f"{WS}/main", json={"name": "x", "row_version": 2}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "scope_archived"
    r = client.patch(
        f"{WS}/main/projects/sample", json={"name": "x", "row_version": 1}, headers=headers
    )
    assert r.status_code == 409 and r.json()["code"] == "scope_archived"
    # restore
    back = client.patch(f"{WS}/main", json={"status": "active", "row_version": 2}, headers=headers)
    assert back.status_code == 200 and back.json()["status"] == "active"
    assert (
        client.post(
            f"{WS}/main/projects", json={"slug": "p2", "name": "P2"}, headers=headers
        ).status_code
        == 201
    )
    actions = set(owner_session.execute(text("SELECT action FROM shifde.audit_events")).scalars())
    assert {"tenancy.workspace.archived", "tenancy.workspace.restored"} <= actions


def test_archived_project_and_org_semantics(client: TestClient, world: World) -> None:
    headers = login(client, world, ADMIN_A)
    P = f"{WS}/main/projects"
    assert (
        client.patch(
            f"{P}/sample", json={"status": "archived", "row_version": 1}, headers=headers
        ).status_code
        == 200
    )
    assert client.get(P).json()["items"] == []
    assert len(client.get(f"{P}?include_archived=true").json()["items"]) == 1
    r = client.patch(f"{P}/sample", json={"description": "x", "row_version": 2}, headers=headers)
    assert r.json()["code"] == "scope_archived"
    org = client.patch(
        "/api/v1/orgs/demo-a", json={"status": "archived", "row_version": 1}, headers=headers
    )
    assert org.status_code == 200
    assert client.get("/api/v1/orgs").json()["items"] == []
    assert len(client.get("/api/v1/orgs?include_archived=true").json()["items"]) == 1
    r = client.post(WS, json={"slug": "w2", "name": "W2"}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "scope_archived"


def test_members_listing(client: TestClient, world: World) -> None:
    login(client, world, ADMIN_A)
    page = client.get("/api/v1/orgs/demo-a/members").json()
    by_email = {m["user"]["email"]: m for m in page["items"]}
    assert set(by_email) == {ADMIN_A, VIEWER_A}
    assert by_email[VIEWER_A]["roles"] == ["Viewer"] and by_email[VIEWER_A]["status"] == "active"
    one = client.get("/api/v1/orgs/demo-a/members?limit=1").json()
    assert len(one["items"]) == 1 and one["next_cursor"]
    rest = client.get(f"/api/v1/orgs/demo-a/members?limit=1&cursor={one['next_cursor']}").json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert client.get("/api/v1/orgs/demo-a/members?limit=0").status_code == 422
    assert client.get("/api/v1/orgs/demo-a/members?cursor=zzz").status_code == 422


def test_dashboard_counts_scope_and_activity(client: TestClient, world: World) -> None:
    headers = login(client, world, ADMIN_A)
    client.post(WS, json={"slug": "eng", "name": "Eng"}, headers=headers)
    client.post(f"{WS}/eng/projects", json={"slug": "api", "name": "API"}, headers=headers)
    d = client.get("/api/v1/orgs/demo-a/dashboard").json()
    assert d["scope"] == {"org": "demo-a", "workspace": None, "project": None}
    assert d["counts"] == {"workspaces": 2, "projects": 2, "members": 2}
    acts = d["recent_activity"]
    assert acts and set(acts[0]) == {"at", "actor", "action", "target"}
    assert {a["action"] for a in acts} >= {"tenancy.project.created", "identity.login.succeeded"}
    ws = client.get("/api/v1/orgs/demo-a/dashboard?workspace=eng").json()
    assert ws["scope"] == {"org": "demo-a", "workspace": "eng", "project": None}
    assert ws["counts"]["workspaces"] == 1 and ws["counts"]["projects"] == 1
    pr = client.get("/api/v1/orgs/demo-a/dashboard?workspace=eng&project=api").json()
    assert pr["scope"]["project"] == "api" and pr["counts"]["projects"] == 1
    assert {a["action"] for a in pr["recent_activity"]} == {"tenancy.project.created"}
    assert client.get("/api/v1/orgs/demo-a/dashboard?project=api").status_code == 422
    assert client.get("/api/v1/orgs/demo-a/dashboard?workspace=nope").status_code == 404
    # tenant-level activity is not shown to a plain viewer
    client.post("/api/auth/logout", headers=headers)
    login(client, world, VIEWER_A)
    v = client.get("/api/v1/orgs/demo-a/dashboard").json()
    assert v["counts"]["members"] is None  # viewer lacks member:read
    assert all(
        a["action"].startswith("tenancy.workspace") or a["action"].startswith("tenancy.project")
        for a in v["recent_activity"]
    )


def test_platform_admin_creates_org_and_lists_all(client: TestClient, world: World) -> None:
    headers = login(client, world, PLATFORM)
    r = client.post(
        "/api/v1/orgs",
        json={"slug": "newco", "name": "NewCo", "admin_email": ADMIN_B},
        headers=headers,
    )
    assert r.status_code == 201 and r.json()["slug"] == "newco"
    assert (
        client.post(
            "/api/v1/orgs", json={"slug": "newco", "name": "x"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/orgs",
            json={"slug": "x1", "name": "x", "admin_email": "ghost@x.local"},
            headers=headers,
        ).status_code
        == 422
    )
    everything = client.get("/api/v1/orgs?scope=all").json()["items"]
    assert [o["slug"] for o in everything] == ["demo-a", "demo-b", "newco"]
    # an entry is `OrgSummaryOut`: what the chooser needs, nothing the client wrote about itself
    # (tenancy review 1, B-1011; SETH also returned description, row_version and the timestamps)
    assert set(everything[0]) == {"id", "slug", "name", "status"}
    # without scope=all: only memberships (platform admin has none)
    assert client.get("/api/v1/orgs").json()["items"] == []
    # platform admin has no implicit tenant-content access
    assert client.get("/api/v1/orgs/demo-a/workspaces").status_code == 404
    # new org's admin can sign in and sees it
    client.post("/api/auth/logout", headers=headers)
    login(client, world, ADMIN_B)
    assert {o["slug"] for o in client.get("/api/v1/orgs").json()["items"]} == {"demo-b", "newco"}
    assert client.get("/api/v1/orgs/newco/workspaces").status_code == 200


STATUS = "/api/v1/platform/status"


def test_platform_status(client: TestClient, world: World) -> None:
    """SETH 3ba0e06 `test_api_tenancy.py::test_platform_status`, assertions unchanged; the caller
    is the platform administrator, not SETH's viewer (SEC Minor-2, B-2401)."""
    login(client, world, PLATFORM)
    s = client.get(STATUS).json()
    assert s["version"] == __version__ and s["environment"] == "test"
    assert s["database"]["status"] == "ok" and s["database"]["revision"] == s["database"]["head"]
    assert s["auth"] == {"local_enabled": True, "oidc_providers": 0}
    assert s["worker"] == {"status": "not_running", "count": 0, "last_heartbeat_at": None}


def test_platform_status_counts_installation_identity_providers_only(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Review 1 note (B-2441): `auth.oidc_providers` counts the active installation IdPs
    (`tenant_id IS NULL`, as the installation overview lists them); an active IdP of a client
    and a disabled installation IdP are not counted."""
    login(client, world, PLATFORM)
    tenant_a = owner_session.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = 'demo-a'")
    ).scalar_one()
    oidc_flow.owner_oidc_idp(owner_session, tenant_a, "https://client-idp.example.test")
    owner_session.commit()
    assert client.get(STATUS).json()["auth"]["oidc_providers"] == 0  # a client's IdP only
    oidc_flow.owner_oidc_idp(owner_session, None, "https://install-idp.example.test")
    oidc_flow.owner_oidc_idp(
        owner_session, None, "https://disabled-idp.example.test", status="disabled"
    )
    owner_session.commit()
    assert client.get(STATUS).json()["auth"]["oidc_providers"] == 1


@pytest.mark.parametrize("who", [VIEWER_A, ADMIN_A, ADMIN_B])
def test_platform_status_refuses_everyone_but_the_platform_administrator(
    client: TestClient, world: World, who: str
) -> None:
    """SEC Minor-2 (B-2401): a viewer, a client's own administrator and the administrator of
    another client are refused alike, with no installation detail in the answer."""
    login(client, world, who)
    r = client.get(STATUS)
    assert r.status_code == 403, r.text
    assert "worker" not in r.json() and __version__ not in r.text
    client.cookies.clear()
    assert client.get(STATUS).status_code == 401  # and nobody signed in


def test_platform_status_worker_health_window(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """J m2: healthy when a running worker beat within three lease periods (at least 15 s), stale
    when workers are known but none beat recently; stopped workers never count."""
    login(client, world, PLATFORM)
    window = max(settings.worker_lease_seconds * 3, 15)

    def add(wid: str, age: int, status: str = "running") -> None:
        owner_session.execute(
            text(
                "INSERT INTO shifde.workers (id, hostname, pid, heartbeat_at, version, "
                "concurrency, status) VALUES (:i, 'h', 1, now() - make_interval(secs => :a), "
                "'v', 1, :s)"
            ),
            {"i": wid, "a": age, "s": status},
        )
        owner_session.commit()

    add("w-old", window + 30)
    add("w-stopped", 1, status="stopped")
    w = client.get(STATUS).json()["worker"]
    assert w["status"] == "stale" and w["count"] == 0 and w["last_heartbeat_at"] is not None
    add("w-live", 1)
    add("w-edge", window - 5)
    w = client.get(STATUS).json()["worker"]
    assert w["status"] == "healthy" and w["count"] == 2
