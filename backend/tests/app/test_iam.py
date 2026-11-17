"""P2 users / groups / roles / bindings: authorization, escalation guards, tenant isolation."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.admin import api as admin_api
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post, system_role, tenant_id

A = "/api/v1/orgs/demo-a"
B = "/api/v1/orgs/demo-b"
RAND = (
    "5f2e88ad-aab0-4d4f-8c83-785b8639f684"  # fixed: parametrize ids must agree across xdist workers
)
ADMIN = "admin@demo-a.local"
VIEWER = "viewer@demo-a.local"


def _custom_role(
    client: TestClient, h: dict[str, str], key: str, perms: list[str], org: str = A
) -> str:
    return str(
        post(client, f"{org}/roles", h, {"key": key, "name": key, "permissions": perms})["id"]
    )


def _bind(
    client: TestClient,
    h: dict[str, str],
    subject_type: str,
    subject_id: str,
    role_id: str,
    scope_type: str = "tenant",
    scope_id: str | None = None,
    org: str = A,
    owner: Session | None = None,
) -> Any:
    if scope_id is None:
        assert owner is not None
        scope_id = str(tenant_id(owner, org.rsplit("/", 1)[1]))
    return client.post(
        f"{org}/role-bindings",
        headers=h,
        json={
            "subject_type": subject_type,
            "subject_id": subject_id,
            "role_id": role_id,
            "scope": {"type": scope_type, "id": scope_id},
        },
    )


PLATFORM = "platform@shifde.local"
PG = "/api/v1/platform/groups"


def _platform(client: TestClient, world: World) -> dict[str, str]:
    """Headers of the platform administrator. Unified setup (B-928, B-1401..B-1406): people and
    groups are created and filled only by the platform administrator; SETH's client administrator
    did both."""
    return login(client, world.passwords, PLATFORM)


# ---------------------------------------------------------------- viewer / anonymous
_BODIES: dict[tuple[str, str], Any] = {
    ("POST", "/orgs/{org}/users"): {"email": "x@example.com", "display_name": "X"},
    ("PATCH", "/orgs/{org}/users/{user_id}"): {"status": "suspended", "row_version": 1},
    ("POST", "/platform/groups"): {"slug": "g1", "name": "G1"},
    ("PATCH", "/platform/groups/{group_id}"): {"name": "n"},
    ("PUT", "/platform/groups/{group_id}/members"): {"user_ids": [], "row_version": 1},
    ("POST", "/platform/orgs/{org}/group-bindings"): {
        "group_id": RAND,
        "role_id": RAND,
        "scope": {"type": "tenant", "id": RAND},
    },
    ("POST", "/orgs/{org}/roles"): {"key": "r1", "name": "R", "permissions": ["workspace:read"]},
    ("PATCH", "/orgs/{org}/roles/{role_id}"): {"name": "n"},
    ("POST", "/orgs/{org}/role-bindings"): {
        "subject_type": "user",
        "subject_id": RAND,
        "role_id": RAND,
        "scope": {"type": "tenant", "id": RAND},
    },
    ("DELETE", "/orgs/{org}/role-bindings/{binding_id}"): None,
    ("POST", "/orgs/{org}/service-accounts"): {"name": "sa"},
    ("PATCH", "/orgs/{org}/service-accounts/{sa_id}"): {"status": "disabled"},
    ("POST", "/orgs/{org}/api-keys"): {
        "name": "k",
        "principal_type": "service_account",
        "principal_id": RAND,
        "scopes": ["workspace:read"],
        "expires_in_days": 30,
    },
    ("POST", "/orgs/{org}/api-keys/{key_id}/rotate"): {},
    ("POST", "/orgs/{org}/api-keys/{key_id}/revoke"): None,
    ("POST", "/platform/idps"): {
        "display_name": "x",
        "issuer": "https://i.example",
        "discovery_url": "https://i.example/.well-known/openid-configuration",
        "client_id": "c",
    },
    ("PATCH", "/platform/idps/{idp_id}"): {"display_name": "n", "row_version": 1},
    ("POST", "/platform/idps/{idp_id}/test"): None,
    ("POST", "/orgs/{org}/idps"): {
        "display_name": "x",
        "issuer": "https://i.example",
        "discovery_url": "https://i.example/.well-known/openid-configuration",
        "client_id": "c",
    },
    ("PATCH", "/orgs/{org}/idps/{idp_id}"): {"display_name": "n", "row_version": 1},
    ("POST", "/orgs/{org}/idps/{idp_id}/test"): None,
    ("POST", "/orgs/{org}/secrets"): {"name": "s", "value": "v-secret-1"},
    ("POST", "/orgs/{org}/secrets/{secret_id}/rotate"): {"value": "v-secret-2"},
    ("PATCH", "/orgs/{org}/secrets/{secret_id}"): {"status": "disabled", "row_version": 1},
}
_READS = [
    "/orgs/{org}/users",
    "/orgs/{org}/groups",
    f"/orgs/{{org}}/groups/{RAND}",
    "/platform/groups",
    f"/platform/groups/{RAND}",
    "/orgs/{org}/roles",
    f"/orgs/{{org}}/roles/{RAND}",
    "/orgs/{org}/role-bindings",
    "/orgs/{org}/service-accounts",
    f"/orgs/{{org}}/service-accounts/{RAND}",
    "/orgs/{org}/api-keys",
    "/orgs/{org}/idps",
    f"/orgs/{{org}}/idps/{RAND}",
    "/orgs/{org}/secrets",
    "/orgs/{org}/audit-events",
    "/orgs/{org}/audit-events/verify",
    "/platform/idps",
]


def _fill(path: str) -> str:
    out = path.replace("{org}", "demo-a")
    for param in (
        "user_id",
        "group_id",
        "role_id",
        "binding_id",
        "sa_id",
        "key_id",
        "idp_id",
        "secret_id",
    ):
        out = out.replace("{" + param + "}", RAND)
    return "/api/v1" + out


def _mutating_admin_routes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in admin_api.router.routes:
        methods = getattr(route, "methods", set())
        path = getattr(route, "path", "").removeprefix("/api/v1")
        for m in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            found.add((m, path))
    return found


def test_the_viewer_test_table_covers_every_mutating_admin_route() -> None:
    assert set(_BODIES) == _mutating_admin_routes()  # a new route forces a conscious test entry


@pytest.mark.parametrize(("method", "path"), sorted(_BODIES))
def test_viewer_cannot_use_any_p2_admin_mutation(
    client: TestClient, world: World, method: str, path: str
) -> None:
    h = login(client, world.passwords, VIEWER)
    r = client.request(method, _fill(path), json=_BODIES[(method, path)], headers=h)
    assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"
    assert r.json()["code"] == "forbidden"


@pytest.mark.parametrize("path", _READS)
def test_viewer_cannot_read_admin_resources(client: TestClient, world: World, path: str) -> None:
    h = login(client, world.passwords, VIEWER)
    assert client.get(_fill(path), headers=h).status_code == 403


@pytest.mark.parametrize(("method", "path"), sorted(_BODIES))
def test_p2_admin_routes_require_authentication(client: TestClient, method: str, path: str) -> None:
    r = client.request(method, _fill(path), json=_BODIES[(method, path)])
    assert r.status_code == 401


def test_permission_catalog_lists_all_later_phase_keys_for_any_user(
    client: TestClient, world: World
) -> None:
    h = login(client, world.passwords, VIEWER)
    body = client.get("/api/v1/permissions", headers=h).json()
    assert body["next_cursor"] is None
    keys = {p["key"] for p in body["items"]}
    for key in (
        "agent:execute",
        "tool:invoke",
        "connection:use",
        "model:invoke",
        "pipeline:publish",
        "ontology:query",
        "data:export",
        "approval:decide",
        "usage:read",
        "budget:manage",
        "skill:publish",
        "run:cancel",
        "vendor:read",
        "connection:write",
        "secret:use",
        "egress:manage",
        "adapter:verify",
        "member:read",
        "group:read",
        "role:read",
        "serviceaccount:read",
        "apikey:read",
        "idp:manage",
        "secret:read",
        "audit:read",
    ):
        assert key in keys, key
    priv = {p["key"] for p in body["items"] if p["is_privileged"]}
    assert {"egress:manage", "adapter:verify", "role:manage", "apikey:manage"} <= priv


# ---------------------------------------------------------------- users
def test_admin_adds_user_with_setup_token_suspends_and_reactivates(
    client: TestClient, world: World, owner_session: Session
) -> None:
    ph = _platform(client, world)
    dev = system_role(owner_session, "developer")
    created = post(
        client,
        f"{A}/users",
        ph,
        {"email": "New.Dev@Example.com", "display_name": "New Dev", "role_ids": [str(dev)]},
    )
    h = login(client, world.passwords, ADMIN)
    assert created["setup_token"] and created["user"]["user"]["email"] == "new.dev@example.com"
    assert [b["role_name"] for b in created["user"]["role_bindings"]] == ["Developer"]
    listing = client.get(f"{A}/users", headers=h).json()
    row = next(i for i in listing["items"] if i["user"]["email"] == "new.dev@example.com")
    assert row["status"] == "active" and "row_version" in row and row["groups"] == []
    uid = row["user"]["id"]
    # suspension requires the current row_version
    bad = client.patch(
        f"{A}/users/{uid}", json={"status": "suspended", "row_version": 99}, headers=h
    )
    assert bad.status_code == 409
    ok = client.patch(
        f"{A}/users/{uid}",
        json={"status": "suspended", "row_version": row["row_version"]},
        headers=h,
    )
    assert ok.status_code == 200 and ok.json()["status"] == "suspended"
    back = client.patch(
        f"{A}/users/{uid}",
        json={"status": "active", "row_version": ok.json()["row_version"]},
        headers=h,
    )
    assert back.json()["status"] == "active"
    actions = {
        r[0]
        for r in owner_session.execute(
            text("SELECT action FROM shifde.audit_events WHERE target_id = :u"), {"u": uid}
        )
    }
    assert {
        "identity.user.created",
        "identity.user.suspended",
        "identity.user.reactivated",
    } <= actions


def test_cannot_suspend_self_or_more_privileged_user(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    me = next(
        i
        for i in client.get(f"{A}/users", headers=h).json()["items"]
        if i["user"]["email"] == ADMIN
    )
    r = client.patch(
        f"{A}/users/{me['user']['id']}",
        json={"status": "suspended", "row_version": me["row_version"]},
        headers=h,
    )
    assert r.status_code == 409
    # a manager without tenant:admin cannot suspend a tenant admin
    mgr_role = _custom_role(
        client, h, "member-mgr", ["member:manage", "member:read", "workspace:read"]
    )
    new_user(client, h, "demo-a", "mgr@example.com", "a-long-password-1", [mgr_role])
    mh = login(client, {"mgr@example.com": "a-long-password-1"}, "mgr@example.com")
    admin_row = next(
        i
        for i in client.get(f"{A}/users", headers=mh).json()["items"]
        if i["user"]["email"] == ADMIN
    )
    denied = client.patch(
        f"{A}/users/{admin_row['user']['id']}",
        json={"status": "suspended", "row_version": admin_row["row_version"]},
        headers=mh,
    )
    assert denied.status_code == 403


def test_duplicate_email_is_a_conflict(client: TestClient, world: World) -> None:
    h = _platform(client, world)
    r = client.post(f"{A}/users", json={"email": VIEWER, "display_name": "dup"}, headers=h)
    assert r.status_code == 409
    # the client administrator no longer creates people at all (B-1401; pinned in
    # tests/admin/test_cross_client.py::test_only_the_platform_administrator_creates_people)


# ---------------------------------------------------------------- groups
def test_group_membership_grants_exactly_the_role_permissions_and_removal_revokes(
    client: TestClient, world: World, owner_session: Session
) -> None:
    # B-928: the group is a platform group, created and filled by the platform administrator;
    # the client administrator binds it (the owner's own act, B-1602). The permission check is
    # SETH's.
    h = login(client, world.passwords, ADMIN)
    role = _custom_role(client, h, "audit-only", ["audit:read", "workspace:read"])
    uid = new_user(client, h, "demo-a", "grp@example.com", "a-long-password-1")
    ph = _platform(client, world)
    group = post(client, PG, ph, {"slug": "auditors", "name": "Auditors"})
    h = login(client, world.passwords, ADMIN)
    assert _bind(client, h, "group", group["id"], role, owner=owner_session).status_code == 201

    def perms_of_member() -> list[str] | None:
        """The member's permissions in A, or None when A is invisible to them (B-1009: a person
        sees a client only through a live role binding; SETH answered 200 with an empty list)."""
        mh = login(client, {"grp@example.com": "a-long-password-1"}, "grp@example.com")
        r = client.get(f"{A}/me/permissions", headers=mh)
        if r.status_code == 404:
            return None
        assert r.status_code == 200
        return list(r.json()["permissions"])

    assert perms_of_member() is None  # member of the org, but no grant: the client is invisible
    ph = _platform(client, world)
    put = client.put(
        f"{PG}/{group['id']}/members",
        json={"user_ids": [uid], "row_version": group["row_version"]},
        headers=ph,
    )
    assert put.status_code == 200 and [m["id"] for m in put.json()["members"]] == [uid]
    assert perms_of_member() == ["audit:read", "workspace:read"]  # exactly the role, nothing more
    ph = _platform(client, world)
    cleared = client.put(
        f"{PG}/{group['id']}/members",
        json={"user_ids": [], "row_version": put.json()["row_version"]},
        headers=ph,
    )
    assert cleared.status_code == 200
    assert perms_of_member() is None  # removal revokes immediately
    users = client.get(f"{A}/users", headers=login(client, world.passwords, ADMIN)).json()["items"]
    assert next(u for u in users if u["user"]["id"] == uid)["groups"] == []
    # another client's administrator sees neither the group nor its binding (cross-client)
    bh = login(client, world.passwords, "admin@demo-b.local")
    assert client.get(f"{A}/groups", headers=bh).status_code == 404
    groups_b = client.get(f"{B}/groups", headers=bh).json()["items"]
    assert all(g["id"] != group["id"] for g in groups_b)


def test_group_crud_conflicts_and_row_versions(client: TestClient, world: World) -> None:
    # B-928: group CRUD is the platform administrator's, at /platform/groups
    h = _platform(client, world)
    g = post(client, PG, h, {"slug": "eng", "name": "Eng", "description": "d"})
    assert g["source"] == "local" and g["member_count"] == 0
    assert client.post(PG, json={"slug": "eng", "name": "x"}, headers=h).status_code == 409
    assert client.get(f"{PG}/{g['id']}", headers=h).json()["members"] == []
    p = client.patch(f"{PG}/{g['id']}", json={"name": "Engineering", "row_version": 1}, headers=h)
    assert p.status_code == 200 and p.json()["row_version"] == 2
    stale = client.patch(f"{PG}/{g['id']}", json={"name": "z", "row_version": 1}, headers=h)
    assert stale.status_code == 409
    assert client.get(f"{PG}/{uuid4()}", headers=h).status_code == 404
    # a client administrator reaches none of it (pinned in tests/admin/test_cross_client.py)
    ah = login(client, world.passwords, ADMIN)
    assert client.get(f"{PG}/{g['id']}", headers=ah).status_code == 403
    assert client.post(PG, json={"slug": "e2", "name": "x"}, headers=ah).status_code == 403


# ---------------------------------------------------------------- roles
def test_custom_roles_templates_readonly_and_privileged_assignment(
    client: TestClient, world: World
) -> None:
    h = login(client, world.passwords, ADMIN)
    roles = client.get(f"{A}/roles", headers=h).json()["items"]
    names = {r["name"] for r in roles}
    assert {
        "Operator",
        "Auditor",
        "Viewer",
        "Tenant Administrator",
    } <= names
    # the platform template is hidden from clients (B-406, B-1010; tests/admin/test_cross_client.py)
    assert "Platform Administrator" not in names
    template = next(r for r in roles if r["name"] == "Viewer")
    assert template["is_system"] is True
    assert (
        client.patch(f"{A}/roles/{template['id']}", json={"name": "x"}, headers=h).status_code
        == 403
    )
    role = post(
        client,
        f"{A}/roles",
        h,
        {"key": "my-role", "name": "Mine", "permissions": ["workspace:read"]},
    )
    assert role["is_system"] is False and role["permissions"] == ["workspace:read"]
    upd = client.patch(
        f"{A}/roles/{role['id']}",
        json={
            "permissions": ["workspace:read", "project:read"],
            "row_version": role["row_version"],
        },
        headers=h,
    )
    assert upd.status_code == 200 and upd.json()["permissions"] == [
        "project:read",
        "workspace:read",
    ]
    assert client.get(f"{A}/roles/{role['id']}", headers=h).json()["permissions"] == [
        "project:read",
        "workspace:read",
    ]
    # validation
    assert (
        client.post(
            f"{A}/roles", json={"key": "my-role", "name": "d", "permissions": []}, headers=h
        ).status_code
        == 409
    )
    for perms in (["nope:nope"], ["platform:admin"]):
        r = client.post(
            f"{A}/roles", json={"key": "bad", "name": "b", "permissions": perms}, headers=h
        )
        assert r.status_code == 422


# ---------------------------------------------------------------- escalation guards
def _delegate(client: TestClient, world: World, owner: Session, perms: list[str]) -> dict[str, str]:
    """Create a delegate user holding exactly `perms` at tenant scope; return its headers."""
    h = login(client, world.passwords, ADMIN)
    role = _custom_role(client, h, "delegate", perms)
    new_user(client, h, "demo-a", "delegate@example.com", "a-long-password-1", [role])
    return login(client, {"delegate@example.com": "a-long-password-1"}, "delegate@example.com")


def test_cannot_create_or_edit_role_granting_permissions_you_do_not_hold(
    client: TestClient, world: World, owner_session: Session
) -> None:
    dh = _delegate(client, world, owner_session, ["role:manage", "role:read", "workspace:read"])
    r = client.post(
        f"{A}/roles", json={"key": "x", "name": "x", "permissions": ["tenant:admin"]}, headers=dh
    )
    assert r.status_code == 403 and "tenant:admin" in r.json()["detail"]
    priv = client.post(
        f"{A}/roles", json={"key": "x", "name": "x", "permissions": ["secret:manage"]}, headers=dh
    )
    assert priv.status_code == 403  # privileged and not held
    ok = client.post(
        f"{A}/roles",
        json={"key": "ok", "name": "ok", "permissions": ["workspace:read"]},
        headers=dh,
    )
    assert ok.status_code == 201
    esc = client.patch(
        f"{A}/roles/{ok.json()['id']}",
        json={"permissions": ["workspace:read", "tenant:admin"], "row_version": 1},
        headers=dh,
    )
    assert esc.status_code == 403


def test_cannot_bind_roles_you_do_not_hold_including_to_yourself(
    client: TestClient, world: World, owner_session: Session
) -> None:
    dh = _delegate(
        client, world, owner_session, ["role:manage", "role:read", "workspace:read", "member:read"]
    )
    me = next(
        i
        for i in client.get(f"{A}/users", headers=dh).json()["items"]
        if i["user"]["email"] == "delegate@example.com"
    )["user"]["id"]
    tenant_admin = str(system_role(owner_session, "tenant_administrator"))
    r = _bind(client, dh, "user", me, tenant_admin, owner=owner_session)
    assert r.status_code == 403  # self-escalation
    viewer_role = str(system_role(owner_session, "viewer"))
    r2 = _bind(client, dh, "user", me, viewer_role, owner=owner_session)
    assert r2.status_code == 403  # Viewer includes keys the delegate lacks (agent:read ...)
    only = _custom_role(client, login(client, world.passwords, ADMIN), "wsr", ["workspace:read"])
    dh = login(client, {"delegate@example.com": "a-long-password-1"}, "delegate@example.com")
    assert _bind(client, dh, "user", me, only, owner=owner_session).status_code == 201
    platform_role = str(system_role(owner_session, "platform_administrator"))
    admin_h = login(client, world.passwords, ADMIN)
    assert _bind(client, admin_h, "user", me, platform_role, owner=owner_session).status_code == 422


def test_scoped_role_manager_cannot_bind_at_a_broader_scope(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    ws1 = post(client, f"{A}/workspaces", h, {"slug": "w1", "name": "W1"})
    ws2 = post(client, f"{A}/workspaces", h, {"slug": "w2", "name": "W2"})
    iam = _custom_role(
        client, h, "ws-iam", ["role:manage", "role:read", "workspace:read", "project:read"]
    )
    uid = new_user(client, h, "demo-a", "wsadmin@example.com", "a-long-password-1")
    h = login(client, world.passwords, ADMIN)
    assert _bind(client, h, "user", uid, iam, "workspace", ws1["id"]).status_code == 201
    wh = login(client, {"wsadmin@example.com": "a-long-password-1"}, "wsadmin@example.com")
    reader = _custom_role(
        client, login(client, world.passwords, ADMIN), "ws-reader", ["workspace:read"]
    )
    wh = login(client, {"wsadmin@example.com": "a-long-password-1"}, "wsadmin@example.com")
    assert _bind(client, wh, "user", uid, reader, "workspace", ws1["id"]).status_code == 201
    assert (
        _bind(client, wh, "user", uid, reader, "workspace", ws2["id"]).status_code == 403
    )  # other ws
    assert _bind(client, wh, "user", uid, reader, owner=owner_session).status_code == 403  # tenant
    listing = client.get(f"{A}/role-bindings", headers=wh).json()["items"]
    assert listing and all(
        b["scope"]["type"] == "workspace" and b["scope"]["id"] == ws1["id"] for b in listing
    )
    # deleting a binding outside the scope is refused too
    admin_h = login(client, world.passwords, ADMIN)
    tenant_binding = client.get(f"{A}/role-bindings", headers=admin_h).json()["items"][0]
    wh = login(client, {"wsadmin@example.com": "a-long-password-1"}, "wsadmin@example.com")
    assert client.delete(f"{A}/role-bindings/{tenant_binding['id']}", headers=wh).status_code in (
        403,
        404,
    )


def test_adding_group_members_cannot_launder_privileges(
    client: TestClient, world: World, owner_session: Session
) -> None:
    ph = _platform(client, world)
    group = post(client, PG, ph, {"slug": "admins", "name": "Admins"})
    h = login(client, world.passwords, ADMIN)
    tenant_admin = str(system_role(owner_session, "tenant_administrator"))
    assert (
        _bind(client, h, "group", group["id"], tenant_admin, owner=owner_session).status_code == 201
    )
    dh = _delegate(
        client,
        world,
        owner_session,
        ["group:manage", "group:read", "member:read", "workspace:read"],
    )
    users = client.get(f"{A}/users", headers=dh).json()["items"]
    target = next(u for u in users if u["user"]["email"] == VIEWER)["user"]["id"]
    body = {"user_ids": [target], "row_version": group["row_version"]}
    # a delegate holding group:manage cannot fill a group that carries tenant administration
    # (SETH: 403; the route is now the platform administrator's, so the refusal is the route's)
    assert client.put(f"{PG}/{group['id']}/members", json=body, headers=dh).status_code == 403
    # SETH: "the admin may". Not any more (B-928): the client administrator is refused too ...
    h = login(client, world.passwords, ADMIN)
    assert client.put(f"{PG}/{group['id']}/members", json=body, headers=h).status_code == 403
    # ... and the platform administrator fills it
    ph = _platform(client, world)
    ok = client.put(f"{PG}/{group['id']}/members", json=body, headers=ph)
    assert ok.status_code == 200


def test_binding_expiry_and_hard_delete_are_audited(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    role = _custom_role(client, h, "short", ["workspace:read"])
    tid = str(tenant_id(owner_session, "demo-a"))
    viewer = next(
        u
        for u in client.get(f"{A}/users", headers=h).json()["items"]
        if u["user"]["email"] == VIEWER
    )
    past = client.post(
        f"{A}/role-bindings",
        headers=h,
        json={
            "subject_type": "user",
            "subject_id": viewer["user"]["id"],
            "role_id": role,
            "scope": {"type": "tenant", "id": tid},
            "expires_at": "2000-01-01T00:00:00Z",
        },
    )
    assert past.status_code == 422
    b = client.post(
        f"{A}/role-bindings",
        headers=h,
        json={
            "subject_type": "user",
            "subject_id": viewer["user"]["id"],
            "role_id": role,
            "scope": {"type": "tenant", "id": tid},
            "expires_at": "2999-01-01T00:00:00Z",
        },
    )
    assert b.status_code == 201
    dup = client.post(
        f"{A}/role-bindings",
        headers=h,
        json={
            "subject_type": "user",
            "subject_id": viewer["user"]["id"],
            "role_id": role,
            "scope": {"type": "tenant", "id": tid},
        },
    )
    assert dup.status_code == 409
    assert client.delete(f"{A}/role-bindings/{b.json()['id']}", headers=h).status_code == 204
    assert client.delete(f"{A}/role-bindings/{b.json()['id']}", headers=h).status_code == 404
    actions = [
        r[0]
        for r in owner_session.execute(
            text(
                "SELECT action FROM shifde.audit_events WHERE action LIKE 'authz.binding.%' ORDER BY id"
            )
        )
    ]
    assert actions == ["authz.binding.created", "authz.binding.deleted"]


def test_ancestor_visibility_for_project_grantee(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """P1 gap: a project-only grantee can read (metadata of) the ancestor workspace, nothing more."""
    h = login(client, world.passwords, ADMIN)
    ws = client.get(f"{A}/workspaces", headers=h).json()["items"][0]
    proj = client.get(f"{A}/workspaces/{ws['slug']}/projects", headers=h).json()["items"][0]
    role = _custom_role(client, h, "proj-reader", ["project:read"])
    uid = new_user(client, h, "demo-a", "pg@example.com", "a-long-password-1")
    h = login(client, world.passwords, ADMIN)
    assert _bind(client, h, "user", uid, role, "project", proj["id"]).status_code == 201
    ph = login(client, {"pg@example.com": "a-long-password-1"}, "pg@example.com")
    assert client.get(f"{A}/workspaces/{ws['slug']}", headers=ph).status_code == 200
    assert [w["slug"] for w in client.get(f"{A}/workspaces", headers=ph).json()["items"]] == [
        ws["slug"]
    ]
    assert (
        client.get(f"{A}/workspaces/{ws['slug']}/projects/{proj['slug']}", headers=ph).status_code
        == 200
    )
    # ...but no write power is implied
    assert (
        client.patch(
            f"{A}/workspaces/{ws['slug']}", json={"name": "x", "row_version": 1}, headers=ph
        ).status_code
        == 403
    )
    assert (
        client.post(f"{A}/workspaces", json={"slug": "n", "name": "n"}, headers=ph).status_code
        == 403
    )
    perms = client.get(f"{A}/me/permissions?workspace={ws['slug']}", headers=ph).json()[
        "permissions"
    ]
    assert perms == []
    perms = client.get(
        f"{A}/me/permissions?workspace={ws['slug']}&project={proj['slug']}", headers=ph
    ).json()["permissions"]
    assert perms == ["project:read"]


def test_me_permissions_reflect_effective_scope(client: TestClient, world: World) -> None:
    h = login(client, world.passwords, VIEWER)
    tenant_perms = client.get(f"{A}/me/permissions", headers=h).json()["permissions"]
    assert "workspace:read" in tenant_perms and "tenant:admin" not in tenant_perms
    h = login(client, world.passwords, ADMIN)
    admin_perms = client.get(f"{A}/me/permissions", headers=h).json()["permissions"]
    assert "role:manage" in admin_perms and "audit:read" in admin_perms
    assert client.get(f"{A}/me/permissions?workspace=nope", headers=h).status_code == 404
    assert client.get(f"{A}/me/permissions?project=x", headers=h).status_code == 422
    assert client.get(f"{B}/me/permissions", headers=h).status_code == 404


def test_session_advertises_exactly_the_implemented_features(
    client: TestClient, world: World
) -> None:
    # SETH 3ba0e06 `test_p2_iam.py`, unchanged: `operate` and `pipelines` come with the runtime
    # router (APP-2, B-2400)
    r = client.post(
        "/api/auth/local/login",
        json={"email": VIEWER, "password": world.passwords[VIEWER]},
    )
    assert r.status_code == 200 and r.json()["features"] == [
        "build",
        "governance",
        "integrate",
        "ontology",
        "operate",
        "pipelines",
        "security",
        "tenancy",
    ]
    assert set(client.get("/api/auth/session").json()["features"]) == {
        "build",
        "tenancy",
        "security",
        "integrate",
        "ontology",
        "operate",
        "pipelines",
        "governance",
    }


def test_feature_registry_is_per_instance_sorted_and_validates_keys() -> None:
    from shifde.core.features import FeatureRegistry

    a, b = FeatureRegistry(), FeatureRegistry()
    a.register("security")
    a.register("integrate")
    a.register("security")  # idempotent
    assert a.keys() == ["integrate", "security"] and b.keys() == []
    for bad in ("", "Has Space", "UPPER", "a-b"):
        with pytest.raises(ValueError):
            a.register(bad)
