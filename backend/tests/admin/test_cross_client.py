"""Cross-client isolation of the admin routes (part 1: IAM; part 2: service accounts, API keys
and platform group bindings; part 3: identity providers, secrets and audit), through a minimal
app.

Every route of `admin.api.router` has a test here (`ROUTE_TESTS`). Each first gets the caller's
own object (200/201), then asks for client B's object under A's org (exactly the answer a missing
id gets) and under B's org (exactly the answer a missing org gets), and finds B's rows absent
from A's lists. The platform routes are refused for a client owner with one answer whatever the
id; `POST /orgs/{org}/users` is the platform administrator's.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.admin import api, credentials, providers
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import apikeys
from shifde.secrets.service import SECRET_NAME_CONFLICT
from tests.admin.conftest import admin_client
from tests.identity.harness import login
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug

ROUTE_TESTS = {
    ("GET", "/api/v1/permissions"): "test_the_permission_catalog_names_no_client",
    ("GET", "/api/v1/orgs/{org}/users"): "test_users_are_listed_only_in_their_own_client",
    ("POST", "/api/v1/orgs/{org}/users"): "test_only_the_platform_administrator_creates_people",
    (
        "PATCH",
        "/api/v1/orgs/{org}/users/{user_id}",
    ): "test_a_cannot_suspend_or_reactivate_bs_members",
    ("GET", "/api/v1/orgs/{org}/groups"): "test_a_sees_only_the_groups_bound_in_a",
    ("GET", "/api/v1/orgs/{org}/groups/{group_id}"): "test_a_sees_only_the_groups_bound_in_a",
    ("GET", "/api/v1/platform/groups"): "test_platform_group_routes_refuse_a_client_owner",
    ("POST", "/api/v1/platform/groups"): "test_platform_group_routes_refuse_a_client_owner",
    ("GET", "/api/v1/platform/groups/{group_id}"): (
        "test_platform_group_routes_refuse_a_client_owner"
    ),
    ("PATCH", "/api/v1/platform/groups/{group_id}"): (
        "test_platform_group_routes_refuse_a_client_owner"
    ),
    ("PUT", "/api/v1/platform/groups/{group_id}/members"): (
        "test_platform_group_routes_refuse_a_client_owner"
    ),
    ("GET", "/api/v1/orgs/{org}/roles"): "test_a_cannot_list_read_create_or_change_bs_roles",
    ("POST", "/api/v1/orgs/{org}/roles"): "test_a_cannot_list_read_create_or_change_bs_roles",
    ("GET", "/api/v1/orgs/{org}/roles/{role_id}"): (
        "test_a_cannot_list_read_create_or_change_bs_roles"
    ),
    ("PATCH", "/api/v1/orgs/{org}/roles/{role_id}"): (
        "test_a_cannot_list_read_create_or_change_bs_roles"
    ),
    ("GET", "/api/v1/orgs/{org}/role-bindings"): "test_a_cannot_list_create_or_delete_bs_bindings",
    ("POST", "/api/v1/orgs/{org}/role-bindings"): (
        "test_a_cannot_list_create_or_delete_bs_bindings"
    ),
    ("DELETE", "/api/v1/orgs/{org}/role-bindings/{binding_id}"): (
        "test_a_cannot_list_create_or_delete_bs_bindings"
    ),
    ("GET", "/api/v1/orgs/{org}/me/permissions"): "test_my_permissions_are_only_my_own_clients",
    # admin part 2 (B-1601..)
    ("POST", "/api/v1/platform/orgs/{org}/group-bindings"): (
        "test_only_the_platform_administrator_binds_a_group_without_owning_the_client"
    ),
    ("GET", "/api/v1/orgs/{org}/service-accounts"): (
        "test_a_cannot_list_read_or_change_bs_service_accounts"
    ),
    ("POST", "/api/v1/orgs/{org}/service-accounts"): (
        "test_a_cannot_list_read_or_change_bs_service_accounts"
    ),
    ("GET", "/api/v1/orgs/{org}/service-accounts/{sa_id}"): (
        "test_a_cannot_list_read_or_change_bs_service_accounts"
    ),
    ("PATCH", "/api/v1/orgs/{org}/service-accounts/{sa_id}"): (
        "test_a_cannot_list_read_or_change_bs_service_accounts"
    ),
    (
        "GET",
        "/api/v1/orgs/{org}/api-keys",
    ): "test_a_cannot_list_create_rotate_or_revoke_bs_api_keys",
    ("POST", "/api/v1/orgs/{org}/api-keys"): (
        "test_a_cannot_list_create_rotate_or_revoke_bs_api_keys"
    ),
    ("POST", "/api/v1/orgs/{org}/api-keys/{key_id}/rotate"): (
        "test_a_cannot_list_create_rotate_or_revoke_bs_api_keys"
    ),
    ("POST", "/api/v1/orgs/{org}/api-keys/{key_id}/revoke"): (
        "test_a_cannot_list_create_rotate_or_revoke_bs_api_keys"
    ),
    # admin part 3a (B-1621..)
    ("GET", "/api/v1/orgs/{org}/idps"): ("test_a_cannot_list_read_change_or_test_bs_idps"),
    ("POST", "/api/v1/orgs/{org}/idps"): ("test_a_cannot_list_read_change_or_test_bs_idps"),
    ("GET", "/api/v1/orgs/{org}/idps/{idp_id}"): ("test_a_cannot_list_read_change_or_test_bs_idps"),
    ("PATCH", "/api/v1/orgs/{org}/idps/{idp_id}"): (
        "test_a_cannot_list_read_change_or_test_bs_idps"
    ),
    ("POST", "/api/v1/orgs/{org}/idps/{idp_id}/test"): (
        "test_a_cannot_list_read_change_or_test_bs_idps"
    ),
    ("GET", "/api/v1/platform/idps"): ("test_platform_idp_routes_reach_installation_idps_only"),
    ("POST", "/api/v1/platform/idps"): ("test_platform_idp_routes_reach_installation_idps_only"),
    ("GET", "/api/v1/platform/idps/{idp_id}"): (
        "test_platform_idp_routes_reach_installation_idps_only"
    ),
    ("PATCH", "/api/v1/platform/idps/{idp_id}"): (
        "test_platform_idp_routes_reach_installation_idps_only"
    ),
    ("POST", "/api/v1/platform/idps/{idp_id}/test"): (
        "test_platform_idp_routes_reach_installation_idps_only"
    ),
    # admin part 3b (B-1641..)
    ("GET", "/api/v1/orgs/{org}/secrets"): "test_a_cannot_list_create_rotate_or_disable_bs_secrets",
    ("POST", "/api/v1/orgs/{org}/secrets"): (
        "test_a_cannot_list_create_rotate_or_disable_bs_secrets"
    ),
    ("POST", "/api/v1/orgs/{org}/secrets/{secret_id}/rotate"): (
        "test_a_cannot_list_create_rotate_or_disable_bs_secrets"
    ),
    ("PATCH", "/api/v1/orgs/{org}/secrets/{secret_id}"): (
        "test_a_cannot_list_create_rotate_or_disable_bs_secrets"
    ),
    ("GET", "/api/v1/orgs/{org}/audit-events"): (
        "test_a_lists_and_verifies_only_its_own_audit_events"
    ),
    ("GET", "/api/v1/orgs/{org}/audit-events/verify"): (
        "test_a_lists_and_verifies_only_its_own_audit_events"
    ),
}

_VARYING = {"instance", "request_id"}


def test_every_admin_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


def _same(r: httpx.Response, reference: httpx.Response, status: int) -> None:
    """Refused exactly like an id or slug that does not exist: no existence hint."""
    assert r.status_code == reference.status_code == status, (r.status_code, r.text)
    body, ref = r.json(), reference.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in ref.items() if k not in _VARYING
    }


def _uid(owner: Session, email: str) -> UUID:
    found: UUID = owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
    ).scalar_one()
    return found


def _system_role(owner: Session, key: str) -> UUID:
    found: UUID = owner.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": key}
    ).scalar_one()
    return found


def _bind_group(
    c: Any, headers: dict[str, str], slug: str, tenant: UUID, group: UUID, role: UUID
) -> httpx.Response:
    body = {
        "subject_type": "group",
        "subject_id": str(group),
        "role_id": str(role),
        "scope": {"type": "tenant", "id": str(tenant)},
    }
    r: httpx.Response = c.post(f"/api/v1/orgs/{slug}/role-bindings", json=body, headers=headers)
    return r


def test_the_permission_catalog_names_no_client(settings: Settings, people: People) -> None:
    seen = []
    for email in (people.owner_a, people.owner_b):
        with admin_client(settings) as c:
            login(c, email)
            r = c.get("/api/v1/permissions")
            assert r.status_code == 200
            seen.append(r.json())
    assert seen[0] == seen[1]  # one installation-wide catalog: nothing of either client
    assert all(
        set(p) == {"key", "resource_type", "action", "description", "is_privileged"}
        for p in seen[0]["items"]
    )


def test_users_are_listed_only_in_their_own_client(
    settings: Settings, world: World, people: People
) -> None:
    a, b = world.a, world.b
    with admin_client(settings) as c:
        login(c, people.owner_a)
        r = c.get(f"/api/v1/orgs/{a.slug}/users?limit=200")
        assert r.status_code == 200
        ids = {u["user"]["id"] for u in r.json()["items"]}
        emails = {u["user"]["email"] for u in r.json()["items"]}
        assert people.owner_a in emails and people.both in emails and str(a.member) in ids
        assert people.owner_b not in emails and str(b.member) not in ids
        assert str(a.staff) not in ids  # group staff are not members
        for u in r.json()["items"]:
            assert "last_login_at" not in u["user"]
            assert {bd["scope"]["id"] for bd in u["role_bindings"]} <= {str(a.tenant)}
        _same(
            c.get(f"/api/v1/orgs/{b.slug}/users"), c.get("/api/v1/orgs/no-such-client/users"), 404
        )


def test_only_the_platform_administrator_creates_people(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    tag = uuid7().hex[-12:]
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        body = {"email": f"new-{tag}@route.test", "display_name": "New"}
        own = c.post(f"/api/v1/orgs/{a.slug}/users", json=body, headers=headers)
        assert own.status_code == 403  # even in the owner's own client (B-913)
        _same(c.post(f"/api/v1/orgs/{b.slug}/users", json=body, headers=headers), own, 403)
        _same(c.post("/api/v1/orgs/no-such-client/users", json=body, headers=headers), own, 403)
    viewer = _system_role(owner_session, "viewer")
    with admin_client(settings) as c:
        headers = login(c, people.platform)
        body = {"email": f"new-{tag}@route.test", "display_name": "New", "role_ids": [str(viewer)]}
        created = c.post(f"/api/v1/orgs/{a.slug}/users", json=body, headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["setup_token"]
        user = created.json()["user"]
        assert [bd["scope"]["id"] for bd in user["role_bindings"]] == [str(a.tenant)]
        missing = c.post("/api/v1/orgs/no-such-client/users", json=body, headers=headers)
        assert missing.status_code == 404
        # a role of B is not a role of A: refused like a missing role, nothing written
        b_role = uuid7()
        owner_session.execute(
            text(
                "INSERT INTO shifde.roles (id, tenant_id, key, name, is_system, status) "
                "VALUES (:i, :t, :k, 'b role', false, 'active')"
            ),
            {"i": b_role, "t": b.tenant, "k": f"b-{tag}"},
        )
        owner_session.commit()
        for role in (b_role, uuid7()):
            other = {"email": f"o-{role.hex[-8:]}@route.test", "display_name": "O"}
            r = c.post(
                f"/api/v1/orgs/{a.slug}/users",
                json={**other, "role_ids": [str(role)]},
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert r.json()["detail"] == "role not found"
    new_id = UUID(user["user"]["id"])
    rows = owner_session.execute(
        text("SELECT tenant_id FROM shifde.memberships WHERE user_id = :u"), {"u": new_id}
    ).all()
    assert [r[0] for r in rows] == [a.tenant]
    audited = owner_session.execute(
        text(
            "SELECT tenant_id FROM shifde.audit_events "
            "WHERE action = 'identity.user.created' AND target_id = :u"
        ),
        {"u": str(new_id)},
    ).all()
    assert [r[0] for r in audited] == [a.tenant]


def test_a_cannot_suspend_or_reactivate_bs_members(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    viewer_a = _uid(owner_session, people.viewer_a)
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        users = c.get(f"/api/v1/orgs/{a.slug}/users?limit=200").json()["items"]
        version = next(u["row_version"] for u in users if u["user"]["id"] == str(viewer_a))
        body = {"status": "suspended", "row_version": version}
        r = c.patch(f"/api/v1/orgs/{a.slug}/users/{viewer_a}", json=body, headers=headers)
        assert r.status_code == 200 and r.json()["status"] == "suspended", r.text
        back = {"status": "active", "row_version": r.json()["row_version"]}
        r = c.patch(f"/api/v1/orgs/{a.slug}/users/{viewer_a}", json=back, headers=headers)
        assert r.status_code == 200 and r.json()["status"] == "active"
        missing = c.patch(f"/api/v1/orgs/{a.slug}/users/{uuid7()}", json=body, headers=headers)
        _same(
            c.patch(f"/api/v1/orgs/{a.slug}/users/{b.member}", json=body, headers=headers),
            missing,
            404,
        )
        _same(
            c.patch(f"/api/v1/orgs/{b.slug}/users/{b.member}", json=body, headers=headers),
            c.patch(f"/api/v1/orgs/no-such-client/users/{b.member}", json=body, headers=headers),
            404,
        )
        # a person in both clients: suspended in A only
        both = next(u for u in users if u["user"]["id"] == str(people.both_id))
        body = {"status": "suspended", "row_version": both["row_version"]}
        r = c.patch(f"/api/v1/orgs/{a.slug}/users/{people.both_id}", json=body, headers=headers)
        assert r.status_code == 200
    rows = dict(
        owner_session.execute(
            text("SELECT tenant_id, status FROM shifde.memberships WHERE user_id IN (:u, :m)"),
            {"u": people.both_id, "m": b.member},
        ).all()
    )
    assert rows == {a.tenant: "suspended", b.tenant: "active"}


def test_a_sees_only_the_groups_bound_in_a(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")
    with admin_client(settings) as c:
        headers = login(c, people.owner_b)
        assert _bind_group(c, headers, b.slug, b.tenant, b.group, viewer).status_code == 201
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        bound = _bind_group(c, headers, a.slug, a.tenant, a.group, viewer)
        assert bound.status_code == 201, bound.text
        assert bound.json()["subject_label"] == f"fdes-a-{a.tenant.hex[-12:]}"
        listed = c.get(f"/api/v1/orgs/{a.slug}/groups")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert [g["id"] for g in items] == [str(a.group)]
        assert set(items[0]) == {"id", "name", "role_bindings"}  # no members, slug or status
        assert {bd["scope"]["id"] for bd in items[0]["role_bindings"]} == {str(a.tenant)}
        own = c.get(f"/api/v1/orgs/{a.slug}/groups/{a.group}")
        assert own.status_code == 200 and own.json()["id"] == str(a.group)
        missing = c.get(f"/api/v1/orgs/{a.slug}/groups/{uuid7()}")
        _same(c.get(f"/api/v1/orgs/{a.slug}/groups/{b.group}"), missing, 404)
        _same(
            c.get(f"/api/v1/orgs/{b.slug}/groups/{b.group}"),
            c.get(f"/api/v1/orgs/no-such-client/groups/{b.group}"),
            404,
        )
        _same(c.get(f"/api/v1/orgs/{b.slug}/groups"), c.get("/api/v1/orgs/x-none/groups"), 404)
        # SETH's per-client group writes are gone from the client routes
        for method in ("POST", "PATCH", "PUT"):
            path = f"/api/v1/orgs/{a.slug}/groups"
            if method != "POST":
                path += f"/{a.group}" + ("/members" if method == "PUT" else "")
            assert c.request(method, path, json={}, headers=headers).status_code in (404, 405)


def test_platform_group_routes_refuse_a_client_owner(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    tag = uuid7().hex[-12:]
    create = {"slug": f"g-{tag}", "name": "G"}
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        refused = c.get("/api/v1/platform/groups")
        assert refused.status_code == 403
        for group in (a.group, b.group, uuid7()):  # one answer whatever the id
            _same(c.get(f"/api/v1/platform/groups/{group}"), refused, 403)
            _same(
                c.patch(f"/api/v1/platform/groups/{group}", json={"name": "x"}, headers=headers),
                refused,
                403,
            )
            members = {"user_ids": [str(a.member)], "row_version": 1}
            _same(
                c.put(f"/api/v1/platform/groups/{group}/members", json=members, headers=headers),
                refused,
                403,
            )
        _same(c.post("/api/v1/platform/groups", json=create, headers=headers), refused, 403)
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.group_members WHERE user_id = :u"), {"u": a.member}
        ).scalar_one()
        == 0
    )
    with admin_client(settings) as c:  # the platform administrator: each route works
        headers = login(c, people.platform)
        made = c.post("/api/v1/platform/groups", json=create, headers=headers)
        assert made.status_code == 201, made.text
        gid = made.json()["id"]
        listed = c.get("/api/v1/platform/groups?limit=200")
        assert listed.status_code == 200
        got = c.get(f"/api/v1/platform/groups/{gid}")
        assert got.status_code == 200 and got.json()["members"] == []
        renamed = c.patch(
            f"/api/v1/platform/groups/{gid}", json={"name": "G2", "row_version": 1}, headers=headers
        )
        assert renamed.status_code == 200 and renamed.json()["name"] == "G2"
        filled = c.put(
            f"/api/v1/platform/groups/{gid}/members",
            json={"user_ids": [str(a.staff)], "row_version": renamed.json()["row_version"]},
            headers=headers,
        )
        assert filled.status_code == 200
        assert [m["id"] for m in filled.json()["members"]] == [str(a.staff)]


def test_a_cannot_list_read_create_or_change_bs_roles(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    tag = uuid7().hex[-12:]
    with admin_client(settings) as c:
        headers = login(c, people.owner_b)
        made = c.post(
            f"/api/v1/orgs/{b.slug}/roles",
            json={"key": f"b-{tag}", "name": "B", "permissions": ["workspace:read"]},
            headers=headers,
        )
        assert made.status_code == 201, made.text
        b_role = made.json()["id"]
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        own = c.post(
            f"/api/v1/orgs/{a.slug}/roles",
            json={"key": f"b-{tag}", "name": "A", "permissions": ["workspace:read"]},
            headers=headers,
        )
        assert own.status_code == 201, own.text  # B's key is no conflict in A
        a_role = own.json()["id"]
        assert c.get(f"/api/v1/orgs/{a.slug}/roles/{a_role}").status_code == 200
        patch = {"name": "A2", "row_version": own.json()["row_version"]}
        changed = c.patch(f"/api/v1/orgs/{a.slug}/roles/{a_role}", json=patch, headers=headers)
        assert changed.status_code == 200
        listed = c.get(f"/api/v1/orgs/{a.slug}/roles?limit=200")
        assert listed.status_code == 200
        ids = {r["id"] for r in listed.json()["items"]}
        keys = {r["key"] for r in listed.json()["items"]}
        assert a_role in ids and b_role not in ids
        assert "platform_administrator" not in keys
        missing = uuid7()
        _same(
            c.get(f"/api/v1/orgs/{a.slug}/roles/{b_role}"),
            c.get(f"/api/v1/orgs/{a.slug}/roles/{missing}"),
            404,
        )
        _same(
            c.patch(f"/api/v1/orgs/{a.slug}/roles/{b_role}", json=patch, headers=headers),
            c.patch(f"/api/v1/orgs/{a.slug}/roles/{missing}", json=patch, headers=headers),
            404,
        )
        gone = "/api/v1/orgs/no-such-client/roles"
        _same(c.get(f"/api/v1/orgs/{b.slug}/roles"), c.get(gone), 404)
        _same(c.get(f"/api/v1/orgs/{b.slug}/roles/{b_role}"), c.get(f"{gone}/{b_role}"), 404)
        body = {"key": f"c-{tag}", "name": "C", "permissions": ["workspace:read"]}
        _same(
            c.post(f"/api/v1/orgs/{b.slug}/roles", json=body, headers=headers),
            c.post(gone, json=body, headers=headers),
            404,
        )
        _same(
            c.patch(f"/api/v1/orgs/{b.slug}/roles/{b_role}", json=patch, headers=headers),
            c.patch(f"{gone}/{b_role}", json=patch, headers=headers),
            404,
        )
    name = owner_session.execute(
        text("SELECT name FROM shifde.roles WHERE id = :i"), {"i": b_role}
    ).scalar_one()
    assert name == "B"
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.roles WHERE key = :k"), {"k": f"c-{tag}"}
        ).scalar_one()
        == 0
    )


def test_a_cannot_list_create_or_delete_bs_bindings(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")
    b_binding = owner_session.execute(
        text("SELECT id FROM shifde.role_bindings WHERE tenant_id = :t AND subject_id = :u"),
        {"t": b.tenant, "u": _uid(owner_session, people.owner_b)},
    ).scalar_one()
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        own = {
            "subject_type": "user",
            "subject_id": str(a.member),
            "role_id": str(viewer),
            "scope": {"type": "workspace", "id": str(a.ws)},
        }
        made = c.post(f"/api/v1/orgs/{a.slug}/role-bindings", json=own, headers=headers)
        assert made.status_code == 201, made.text
        listed = c.get(f"/api/v1/orgs/{a.slug}/role-bindings?limit=200")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert made.json()["id"] in {i["id"] for i in items}
        assert str(b_binding) not in {i["id"] for i in items}
        assert {i["scope"]["id"] for i in items} <= {
            str(a.tenant),
            str(a.ws),
            str(a.ws2),
            str(a.project),
        }
        # B's subject, scope or role in A: refused like a missing one
        for field, theirs, value in (
            ("subject_id", str(b.member), str(uuid7())),
            ("role_id", None, str(uuid7())),
            (
                "scope",
                {"type": "workspace", "id": str(b.ws)},
                {"type": "workspace", "id": str(uuid7())},
            ),
        ):
            if theirs is None:  # B's custom role
                rid = uuid7()
                owner_session.execute(
                    text(
                        "INSERT INTO shifde.roles (id, tenant_id, key, name, is_system, status) "
                        "VALUES (:i, :t, :k, 'b', false, 'active')"
                    ),
                    {"i": rid, "t": b.tenant, "k": f"b-{rid.hex[-12:]}"},
                )
                owner_session.commit()
                theirs = str(rid)
            url = f"/api/v1/orgs/{a.slug}/role-bindings"
            _same(
                c.post(url, json={**own, field: theirs}, headers=headers),
                c.post(url, json={**own, field: value}, headers=headers),
                422,
            )
        _same(
            c.delete(f"/api/v1/orgs/{a.slug}/role-bindings/{b_binding}", headers=headers),
            c.delete(f"/api/v1/orgs/{a.slug}/role-bindings/{uuid7()}", headers=headers),
            404,
        )
        gone = "/api/v1/orgs/no-such-client/role-bindings"
        _same(c.get(f"/api/v1/orgs/{b.slug}/role-bindings"), c.get(gone), 404)
        _same(
            c.post(f"/api/v1/orgs/{b.slug}/role-bindings", json=own, headers=headers),
            c.post(gone, json=own, headers=headers),
            404,
        )
        _same(
            c.delete(f"/api/v1/orgs/{b.slug}/role-bindings/{b_binding}", headers=headers),
            c.delete(f"{gone}/{b_binding}", headers=headers),
            404,
        )
        deleted = c.delete(
            f"/api/v1/orgs/{a.slug}/role-bindings/{made.json()['id']}", headers=headers
        )
        assert deleted.status_code == 204
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.role_bindings WHERE id = :i"), {"i": b_binding}
        ).scalar_one()
        == 1
    )
    assert (
        owner_session.execute(
            text(
                "SELECT count(*) FROM shifde.role_bindings WHERE tenant_id = :t AND subject_id = :u"
            ),
            {"t": a.tenant, "u": b.member},
        ).scalar_one()
        == 0
    )


def test_my_permissions_are_only_my_own_clients(
    settings: Settings, world: World, people: People
) -> None:
    a, b = world.a, world.b
    with admin_client(settings) as c:
        login(c, people.viewer_a)
        own = c.get(f"/api/v1/orgs/{a.slug}/me/permissions")
        assert own.status_code == 200 and own.json()["permissions"]
        assert "role:manage" not in own.json()["permissions"]
        assert (
            c.get(f"/api/v1/orgs/{a.slug}/me/permissions?workspace={ws_slug(a)}").status_code == 200
        )
        _same(
            c.get(f"/api/v1/orgs/{a.slug}/me/permissions?workspace={ws_slug(b)}"),
            c.get(f"/api/v1/orgs/{a.slug}/me/permissions?workspace=w-missing"),
            404,
        )
        _same(
            c.get(f"/api/v1/orgs/{b.slug}/me/permissions"),
            c.get("/api/v1/orgs/no-such-client/me/permissions"),
            404,
        )
    with admin_client(settings) as c:
        login(c, people.owner_b)
        assert "role:manage" in c.get(f"/api/v1/orgs/{b.slug}/me/permissions").json()["permissions"]


# ------------------------------------------------------------------ admin part 2 (B-1601..)
def _key_body(principal: UUID | str, **extra: Any) -> dict[str, Any]:
    return {
        "name": "k",
        "principal_type": "service_account",
        "principal_id": str(principal),
        "scopes": ["workspace:read"],
        "expires_in_days": 5,
        **extra,
    }


def test_a_cannot_list_read_or_change_bs_service_accounts(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        url = f"/api/v1/orgs/{a.slug}/service-accounts"
        made = c.post(url, json={"name": "etl"}, headers=headers)
        assert made.status_code == 201, made.text
        own = made.json()["id"]
        assert c.get(f"{url}/{own}").status_code == 200
        patched = c.patch(f"{url}/{own}", json={"description": "d"}, headers=headers)
        assert patched.status_code == 200 and patched.json()["description"] == "d"
        listed = {i["id"] for i in c.get(f"{url}?limit=200").json()["items"]}
        assert {own, str(a.service_account)} <= listed
        assert str(b.service_account) not in listed
        # B's service account under A's org: exactly a missing id
        missing = uuid7()
        _same(c.get(f"{url}/{b.service_account}"), c.get(f"{url}/{missing}"), 404)
        body = {"name": "taken", "status": "disabled"}
        _same(
            c.patch(f"{url}/{b.service_account}", json=body, headers=headers),
            c.patch(f"{url}/{missing}", json=body, headers=headers),
            404,
        )
        # B's names are not taken in A (no existence oracle); A's own is one generic 409
        a_name = owner_session.execute(
            text("SELECT name FROM shifde.service_accounts WHERE id = :i"), {"i": a.service_account}
        ).scalar_one()
        owner_session.execute(
            text(
                "INSERT INTO shifde.service_accounts (id, tenant_id, name) "
                "VALUES (:i, :t, 'only-in-b')"
            ),
            {"i": uuid7(), "t": b.tenant},
        )
        owner_session.commit()
        r = c.patch(f"{url}/{own}", json={"name": "only-in-b"}, headers=headers)
        assert r.status_code == 200, r.text
        r = c.patch(f"{url}/{own}", json={"name": a_name}, headers=headers)
        assert r.status_code == 409 and r.json()["detail"] == credentials.SA_NAME_TAKEN, r.text
        # under B's org: exactly a missing org
        gone = "/api/v1/orgs/no-such-client/service-accounts"
        theirs = f"/api/v1/orgs/{b.slug}/service-accounts"
        _same(c.get(theirs), c.get(gone), 404)
        _same(c.get(f"{theirs}/{b.service_account}"), c.get(f"{gone}/{b.service_account}"), 404)
        _same(
            c.post(theirs, json={"name": "x"}, headers=headers),
            c.post(gone, json={"name": "x"}, headers=headers),
            404,
        )
        _same(
            c.patch(f"{theirs}/{b.service_account}", json=body, headers=headers),
            c.patch(f"{gone}/{b.service_account}", json=body, headers=headers),
            404,
        )
    row = owner_session.execute(
        text("SELECT status, name FROM shifde.service_accounts WHERE id = :i"),
        {"i": b.service_account},
    ).one()
    assert tuple(row) == ("active", "sa")
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.service_accounts WHERE tenant_id = :t"),
            {"t": b.tenant},
        ).scalar_one()
        == 2  # the world's and `only-in-b`: nothing created through A's or B's routes
    )


def test_a_cannot_list_create_rotate_or_revoke_bs_api_keys(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with admin_client(settings) as c:
        hb = login(c, people.owner_b)
        made_b = c.post(
            f"/api/v1/orgs/{b.slug}/api-keys", json=_key_body(b.service_account), headers=hb
        )
        assert made_b.status_code == 201, made_b.text
        b_key = made_b.json()["api_key"]
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        url = f"/api/v1/orgs/{a.slug}/api-keys"
        made = c.post(url, json=_key_body(a.service_account), headers=headers)
        assert made.status_code == 201, made.text
        own = made.json()["api_key"]["id"]
        listed = {i["id"] for i in c.get(f"{url}?limit=200").json()["items"]}
        assert own in listed and b_key["id"] not in listed
        # a filter naming B's principal lists what an unknown principal lists: nothing
        for ptype, theirs in (("service_account", b.service_account), ("user", b.member)):
            r = c.get(f"{url}?principal_type={ptype}&principal_id={theirs}")
            assert r.status_code == 200 and r.json()["items"] == []
        # B's service account, workspace or project in A's request: exactly a missing one
        cases: list[tuple[str, Any, Any]] = [
            ("principal_id", str(b.service_account), str(uuid7())),
            ("resource_scope", {"workspace_ids": [str(b.ws)]}, {"workspace_ids": [str(uuid7())]}),
            ("resource_scope", {"project_ids": [str(b.project)]}, {"project_ids": [str(uuid7())]}),
        ]
        for field, theirs_value, unknown in cases:
            body = _key_body(a.service_account)
            _same(
                c.post(url, json={**body, field: theirs_value}, headers=headers),
                c.post(url, json={**body, field: unknown}, headers=headers),
                422,
            )
        # B's key under A's org: exactly a missing id
        missing = uuid7()
        actions: list[tuple[str, Any]] = [("rotate", {"grace_hours": 0}), ("revoke", None)]
        for action, action_body in actions:
            _same(
                c.post(f"{url}/{b_key['id']}/{action}", json=action_body, headers=headers),
                c.post(f"{url}/{missing}/{action}", json=action_body, headers=headers),
                404,
            )
        rotated = c.post(f"{url}/{own}/rotate", json={"grace_hours": 0}, headers=headers)
        assert rotated.status_code == 201, rotated.text
        revoked = c.post(f"{url}/{rotated.json()['api_key']['id']}/revoke", headers=headers)
        assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
        # under B's org: exactly a missing org
        theirs_url = f"/api/v1/orgs/{b.slug}/api-keys"
        gone = "/api/v1/orgs/no-such-client/api-keys"
        _same(c.get(theirs_url), c.get(gone), 404)
        body = _key_body(b.service_account)
        _same(
            c.post(theirs_url, json=body, headers=headers),
            c.post(gone, json=body, headers=headers),
            404,
        )
        for action, action_body in actions:
            _same(
                c.post(f"{theirs_url}/{b_key['id']}/{action}", json=action_body, headers=headers),
                c.post(f"{gone}/{b_key['id']}/{action}", json=action_body, headers=headers),
                404,
            )
    rows = owner_session.execute(
        text("SELECT revoked_at, expires_at FROM shifde.api_keys WHERE tenant_id = :t"),
        {"t": b.tenant},
    ).all()
    assert len(rows) == 1 and rows[0].revoked_at is None  # B's one key, untouched
    assert rows[0].expires_at == datetime.fromisoformat(b_key["expires_at"])
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.api_keys WHERE rotated_from_id = :k"),
            {"k": b_key["id"]},
        ).scalar_one()
        == 0
    )


def test_a_key_or_service_account_of_a_never_acts_in_b(
    settings: Settings, world: World, people: People, owner_session: Session, app_db: Database
) -> None:
    """A's service-account key, and the personal key in A of a person who is also a member of B,
    address A only: in B every admin route answers like a missing org."""
    a, b = world.a, world.b
    with app_session(app_db, a.tenant, people.both_id) as s:
        _, personal = apikeys.mint(
            s,
            settings,
            tenant_id=a.tenant,
            name="mine",
            principal_type="user",
            principal_id=people.both_id,
            scopes=["workspace:read"],
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=people.both_id,
        )
        s.commit()
    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        bound = c.post(
            f"/api/v1/orgs/{a.slug}/role-bindings",
            json={
                "subject_type": "service_account",
                "subject_id": str(a.service_account),
                "role_id": str(_system_role(owner_session, "tenant_administrator")),
                "scope": {"type": "tenant", "id": str(a.tenant)},
            },
            headers=headers,
        )
        assert bound.status_code == 201, bound.text
        scopes = ["workspace:read", "apikey:manage", "apikey:read", "serviceaccount:read"]
        machine = c.post(
            f"/api/v1/orgs/{a.slug}/api-keys",
            json=_key_body(a.service_account, scopes=scopes),
            headers=headers,
        )
        assert machine.status_code == 201, machine.text
        c.cookies.clear()
        for secret in (personal, machine.json()["secret"]):
            h = {"Authorization": f"Bearer {secret}"}
            assert c.get(f"/api/v1/orgs/{a.slug}/workspaces", headers=h).status_code == 200
            for path in ("service-accounts", "api-keys", f"service-accounts/{b.service_account}"):
                _same(
                    c.get(f"/api/v1/orgs/{b.slug}/{path}", headers=h),
                    c.get(f"/api/v1/orgs/no-such-client/{path}", headers=h),
                    404,
                )
            for body in (
                _key_body(b.service_account),
                _key_body(people.both_id, principal_type="user"),
            ):
                _same(
                    c.post(f"/api/v1/orgs/{b.slug}/api-keys", json=body, headers=h),
                    c.post("/api/v1/orgs/no-such-client/api-keys", json=body, headers=h),
                    404,
                )
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.api_keys WHERE tenant_id = :t"), {"t": b.tenant}
        ).scalar_one()
        == 0
    )


def test_only_the_platform_administrator_binds_a_group_without_owning_the_client(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    viewer = _system_role(owner_session, "viewer")

    def body(group: UUID, role: UUID, scope_type: str = "tenant", scope: UUID = a.tenant) -> Any:
        return {
            "group_id": str(group),
            "role_id": str(role),
            "scope": {"type": scope_type, "id": str(scope)},
        }

    with admin_client(settings) as c:
        headers = login(c, people.owner_a)
        refused = c.post(
            f"/api/v1/platform/orgs/{a.slug}/group-bindings",
            json=body(a.group, viewer),
            headers=headers,
        )
        assert refused.status_code == 403  # even the owner of A: one answer whatever the slug
        for slug in (b.slug, "no-such-client"):
            _same(
                c.post(
                    f"/api/v1/platform/orgs/{slug}/group-bindings",
                    json=body(b.group, viewer, scope=b.tenant),
                    headers=headers,
                ),
                refused,
                403,
            )
    b_role = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.roles (id, tenant_id, key, name, is_system, status) "
            "VALUES (:i, :t, :k, 'b role', false, 'active')"
        ),
        {"i": b_role, "t": b.tenant, "k": f"b-{b_role.hex[-12:]}"},
    )
    platform_id = _uid(owner_session, people.platform)
    owner_session.execute(  # the administrator is in the group: binding it is a self-add in A
        text("INSERT INTO shifde.group_members (group_id, user_id) VALUES (:g, :u)"),
        {"g": b.group, "u": platform_id},
    )
    owner_session.commit()
    with admin_client(settings) as c:
        headers = login(c, people.platform)
        url = f"/api/v1/platform/orgs/{a.slug}/group-bindings"
        made = c.post(url, json=body(b.group, viewer), headers=headers)
        assert made.status_code == 201, made.text
        assert made.json()["subject_id"] == str(b.group)
        assert made.json()["scope"]["id"] == str(a.tenant)
        missing = c.post(
            "/api/v1/platform/orgs/no-such-client/group-bindings",
            json=body(b.group, viewer),
            headers=headers,
        )
        assert missing.status_code == 404
        # B's role or workspace in A: refused like a missing one, nothing written
        _same(
            c.post(url, json=body(a.group, b_role), headers=headers),
            c.post(url, json=body(a.group, uuid7()), headers=headers),
            422,
        )
        _same(
            c.post(url, json=body(a.group, viewer, "workspace", b.ws), headers=headers),
            c.post(url, json=body(a.group, viewer, "workspace", uuid7()), headers=headers),
            403,
        )
        # an organization-level binding naming B's id: the one generic 403 (admin review 3 item 2)
        _same(
            c.post(url, json=body(a.group, viewer, "tenant", b.tenant), headers=headers),
            c.post(url, json=body(a.group, viewer, "tenant", uuid7()), headers=headers),
            403,
        )
    self_adds = owner_session.execute(
        text(
            "SELECT tenant_id, target_id FROM shifde.audit_events "
            "WHERE action = 'tenancy.platform_admin.self_added' "
            "AND details->>'through_group_id' = :g"
        ),
        {"g": str(b.group)},
    ).all()
    assert [tuple(r) for r in self_adds] == [(a.tenant, str(platform_id))]
    rows = owner_session.execute(
        text(
            "SELECT tenant_id, subject_id FROM shifde.role_bindings "
            "WHERE subject_type = 'group' AND tenant_id IN (:a, :b)"
        ),
        {"a": a.tenant, "b": b.tenant},
    ).all()
    assert [tuple(r) for r in rows] == [(a.tenant, b.group)]
    audited = owner_session.execute(
        text(
            "SELECT tenant_id FROM shifde.audit_events WHERE action = 'authz.group.bound' "
            "AND (tenant_id IN (:a, :b) OR tenant_id IS NULL)"
        ),
        {"a": a.tenant, "b": b.tenant},
    ).all()
    assert [r[0] for r in audited] == [a.tenant]
    with admin_client(settings) as c:
        login(c, people.owner_a)
        items = c.get(f"/api/v1/orgs/{a.slug}/groups").json()["items"]
        assert [g["id"] for g in items] == [str(b.group)]
    with admin_client(settings) as c:
        login(c, people.owner_b)
        assert c.get(f"/api/v1/orgs/{b.slug}/groups").json()["items"] == []


# ---------------------------------------------------------------- identity providers (part 3a)
def _idp(issuer: str, **extra: Any) -> dict[str, Any]:
    return {
        "display_name": "corp",
        "issuer": issuer,
        "discovery_url": f"{issuer}/.well-known/openid-configuration",
        "client_id": "c",
        **extra,
    }


def test_a_cannot_list_read_change_or_test_bs_idps(
    settings: Settings, app_db: Database, world: World, people: People, owner_session: Session
) -> None:
    """Identity review 4 item 7, carry_forward 85 and 95 (B-1621, B-1623): B's IdP under A's org
    is exactly a missing id, under B's org exactly a missing org; B's IdPs are absent from A's
    list and B's issuer is not taken in A; a client's IdP holds only that client's secret."""
    a, b = world.a, world.b
    with admin_client(settings) as c:
        hb = login(c, people.owner_b)
        theirs_made = c.post(
            f"/api/v1/orgs/{b.slug}/idps",
            json=_idp("https://b-idp.test", client_secret="b-secret-value"),
            headers=hb,
        )
        assert theirs_made.status_code == 201, theirs_made.text
        b_idp = theirs_made.json()["id"]
        c.cookies.clear()
        headers = login(c, people.owner_a)
        url = f"/api/v1/orgs/{a.slug}/idps"
        made = c.post(url, json=_idp("https://a-idp.test", client_secret="a-sec"), headers=headers)
        assert made.status_code == 201, made.text
        own = made.json()["id"]
        assert made.json()["tenant_id"] == str(a.tenant)
        assert c.get(f"{url}/{own}").status_code == 200
        patched = c.patch(
            f"{url}/{own}", json={"display_name": "d", "row_version": 1}, headers=headers
        )
        assert patched.status_code == 200, patched.text
        listed = {i["id"] for i in c.get(f"{url}?limit=200").json()["items"]}
        assert listed == {own}  # B's IdP is not listed in A
        # B's IdP under A's org: exactly a missing id
        missing = uuid7()
        body = {"display_name": "taken", "status": "disabled", "row_version": 1}
        _same(c.get(f"{url}/{b_idp}"), c.get(f"{url}/{missing}"), 404)
        _same(
            c.patch(f"{url}/{b_idp}", json=body, headers=headers),
            c.patch(f"{url}/{missing}", json=body, headers=headers),
            404,
        )
        _same(
            c.post(f"{url}/{b_idp}/test", headers=headers),
            c.post(f"{url}/{missing}/test", headers=headers),
            404,
        )
        # B's issuer is not taken in A (no existence oracle); A's own duplicate is one answer
        r = c.post(url, json=_idp("https://b-idp.test"), headers=headers)
        assert r.status_code == 201, r.text
        dup = c.post(url, json=_idp("https://a-idp.test"), headers=headers)
        assert dup.status_code == 409 and dup.json()["code"] == "issuer_conflict", dup.text
        assert dup.json()["detail"] == providers.IDP_CONFLICT
        # under B's org: exactly a missing org
        gone = "/api/v1/orgs/no-such-client/idps"
        theirs = f"/api/v1/orgs/{b.slug}/idps"
        _same(c.get(theirs), c.get(gone), 404)
        _same(c.get(f"{theirs}/{b_idp}"), c.get(f"{gone}/{b_idp}"), 404)
        _same(
            c.post(theirs, json=_idp("https://x.test"), headers=headers),
            c.post(gone, json=_idp("https://x.test"), headers=headers),
            404,
        )
        _same(
            c.patch(f"{theirs}/{b_idp}", json=body, headers=headers),
            c.patch(f"{gone}/{b_idp}", json=body, headers=headers),
            404,
        )
        _same(
            c.post(f"{theirs}/{b_idp}/test", headers=headers),
            c.post(f"{gone}/{b_idp}/test", headers=headers),
            404,
        )
    row = owner_session.execute(
        text("SELECT display_name, status, row_version FROM shifde.idp_configs WHERE id = :i"),
        {"i": b_idp},
    ).one()
    assert tuple(row) == ("corp", "active", 1)
    # each IdP's secret is its own client's (B-924)
    pairs = owner_session.execute(
        text(
            "SELECT c.tenant_id, s.tenant_id FROM shifde.idp_configs c "
            "JOIN shifde.secrets s ON s.id = c.client_secret_ref_id"
        )
    ).all()
    assert sorted((str(x), str(y)) for x, y in pairs) == sorted(
        [(str(a.tenant), str(a.tenant)), (str(b.tenant), str(b.tenant))]
    )
    b_secret = owner_session.execute(
        text("SELECT client_secret_ref_id FROM shifde.idp_configs WHERE id = :i"), {"i": b_idp}
    ).scalar_one()
    owner_session.rollback()
    with app_session(app_db, a.tenant) as s:
        # pointing A's IdP at B's secret is refused by the composite key
        with pytest.raises(IntegrityError):
            s.execute(
                text("UPDATE shifde.idp_configs SET client_secret_ref_id = :s WHERE id = :i"),
                {"s": b_secret, "i": own},
            )
        s.rollback()
    with app_session(app_db, a.tenant) as s:
        # B's IdP is invisible to A's context (row-level security)
        changed = s.execute(
            text("UPDATE shifde.idp_configs SET display_name = 'x' WHERE id = :i"), {"i": b_idp}
        )
        assert getattr(changed, "rowcount", None) == 0
        s.rollback()


def test_platform_idp_routes_reach_installation_idps_only(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    """Platform IdP routes are one 403 for a client owner whatever the id (B-1011) and reach only
    installation IdPs: a client's IdP id is a missing id there (identity review 4 item 7); an
    installation IdP holds only an installation secret (B-924)."""
    b = world.b
    with admin_client(settings) as c:
        hb = login(c, people.owner_b)
        made_b = c.post(
            f"/api/v1/orgs/{b.slug}/idps",
            json=_idp("https://b.test", client_secret="bs"),
            headers=hb,
        )
        assert made_b.status_code == 201, made_b.text
        b_idp = made_b.json()["id"]
        c.cookies.clear()
        hp = login(c, people.platform)
        base = "/api/v1/platform/idps"
        made = c.post(base, json=_idp("https://inst.test", client_secret="is"), headers=hp)
        assert made.status_code == 201, made.text
        inst = made.json()["id"]
        assert made.json()["tenant_id"] is None
        assert c.get(f"{base}/{inst}").status_code == 200
        r = c.patch(f"{base}/{inst}", json={"display_name": "i2", "row_version": 1}, headers=hp)
        assert r.status_code == 200, r.text
        assert {i["id"] for i in c.get(f"{base}?limit=200").json()["items"]} == {inst}
        # past the first page (admin isolation review 4 item 4: the cursor is read in context)
        more = {inst}
        for n in (2, 3):
            r = c.post(base, json=_idp(f"https://inst{n}.test"), headers=hp)
            assert r.status_code == 201, r.text
            more.add(r.json()["id"])
        paged: list[str] = []
        cursor: str | None = None
        while True:
            page = c.get(base, params={"limit": 1, **({"cursor": cursor} if cursor else {})})
            assert page.status_code == 200, page.text
            paged += [i["id"] for i in page.json()["items"]]
            cursor = page.json()["next_cursor"]
            if not cursor:
                break
        assert len(paged) == len(set(paged)) and more <= set(paged) and b_idp not in paged
        missing = uuid7()
        body = {"display_name": "x", "row_version": 1}
        _same(c.get(f"{base}/{b_idp}"), c.get(f"{base}/{missing}"), 404)
        _same(
            c.patch(f"{base}/{b_idp}", json=body, headers=hp),
            c.patch(f"{base}/{missing}", json=body, headers=hp),
            404,
        )
        _same(
            c.post(f"{base}/{b_idp}/test", headers=hp),
            c.post(f"{base}/{missing}/test", headers=hp),
            404,
        )
        # a client owner: one 403 whatever the id
        c.cookies.clear()
        ha = login(c, people.owner_a)
        ref = c.get(base)
        for r in (
            c.post(base, json=_idp("https://y.test"), headers=ha),
            c.get(f"{base}/{inst}"),
            c.get(f"{base}/{b_idp}"),
            c.get(f"{base}/{missing}"),
            c.patch(f"{base}/{inst}", json=body, headers=ha),
            c.patch(f"{base}/{missing}", json=body, headers=ha),
            c.post(f"{base}/{inst}/test", headers=ha),
            c.post(f"{base}/{missing}/test", headers=ha),
        ):
            _same(r, ref, 403)
    tenant = owner_session.execute(
        text(
            "SELECT s.tenant_id FROM shifde.idp_configs c "
            "JOIN shifde.secrets s ON s.id = c.client_secret_ref_id WHERE c.id = :i"
        ),
        {"i": inst},
    ).scalar_one()
    assert tenant is None
    name = owner_session.execute(
        text("SELECT display_name FROM shifde.idp_configs WHERE id = :i"), {"i": b_idp}
    ).scalar_one()
    assert name == "corp"


# ---------------------------------------------------------------- secrets and audit (part 3b)
_A_VALUE, _A_ROTATED, _B_VALUE = "a-secret-value-71b3", "a-rotated-value-82c4", "b-secret-val-93d5"


def _owner_secret(owner: Session, tenant: UUID | None, name: str) -> UUID:
    """A secret row written as the owner (material is never decrypted by these routes)."""
    sid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, ciphertext, wrapped_dek, nonce, "
            "kek_id) VALUES (:i, :t, :n, '\\x00', '\\x00', '\\x00', 'k')"
        ),
        {"i": sid, "t": tenant, "n": name},
    )
    owner.commit()
    return sid


def test_a_cannot_list_create_rotate_or_disable_bs_secrets(
    settings: Settings,
    world: World,
    people: People,
    owner_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Carry_forward rows 79 and 104 (B-1641, B-1644): B's secret (and an installation secret)
    under A's org is exactly a missing id, under B's org exactly a missing org; B's secrets are
    absent from A's list and B's names are not taken in A; B's workspace or project as a scope
    is an unknown one; values never reach a response, a log line or an audit row."""
    a, b = world.a, world.b
    installation = _owner_secret(owner_session, None, f"inst-{uuid7().hex[-12:]}")
    with admin_client(settings) as c:
        hb = login(c, people.owner_b)
        theirs_made = c.post(
            f"/api/v1/orgs/{b.slug}/secrets",
            json={"name": "shared-name", "value": _B_VALUE},
            headers=hb,
        )
        assert theirs_made.status_code == 201, theirs_made.text
        b_id = theirs_made.json()["id"]
        c.cookies.clear()
        headers = login(c, people.owner_a)
        url = f"/api/v1/orgs/{a.slug}/secrets"
        made = c.post(url, json={"name": "shared-name", "value": _A_VALUE}, headers=headers)
        assert made.status_code == 201, made.text  # B's name is not taken in A
        own = made.json()["id"]
        rot = c.post(f"{url}/{own}/rotate", json={"value": _A_ROTATED}, headers=headers)
        assert rot.status_code == 200 and rot.json()["version"] == 2, rot.text
        dup = c.post(url, json={"name": "shared-name", "value": "dup-value-1"}, headers=headers)
        assert dup.status_code == 409 and dup.json()["detail"] == SECRET_NAME_CONFLICT, dup.text
        listed = {i["id"] for i in c.get(f"{url}?limit=200").json()["items"]}
        assert listed == {own}
        # B's (or the installation's) secret under A's org: exactly a missing id
        missing = uuid7()
        patch = {"status": "disabled", "row_version": 1}
        for theirs_id in (b_id, installation):
            _same(
                c.post(f"{url}/{theirs_id}/rotate", json={"value": "stolen-1"}, headers=headers),
                c.post(f"{url}/{missing}/rotate", json={"value": "stolen-1"}, headers=headers),
                404,
            )
            _same(
                c.patch(f"{url}/{theirs_id}", json=patch, headers=headers),
                c.patch(f"{url}/{missing}", json=patch, headers=headers),
                404,
            )
        # B's workspace or project as the scope: exactly an unknown one
        for scope_type, theirs_scope in (("workspace", b.ws), ("project", b.project)):

            def scoped(scope_id: UUID, t: str = scope_type) -> dict[str, Any]:
                return {"name": "n", "value": "v-1", "scope": {"type": t, "id": str(scope_id)}}

            _same(
                c.post(url, json=scoped(theirs_scope), headers=headers),
                c.post(url, json=scoped(uuid7()), headers=headers),
                422,
            )
        disabled = c.patch(
            f"{url}/{own}",
            json={"status": "disabled", "row_version": rot.json()["row_version"]},
            headers=headers,
        )
        assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
        # under B's org: exactly a missing org
        gone = "/api/v1/orgs/no-such-client/secrets"
        theirs = f"/api/v1/orgs/{b.slug}/secrets"
        body = {"name": "x", "value": "x-value-1"}
        _same(c.get(theirs), c.get(gone), 404)
        _same(
            c.post(theirs, json=body, headers=headers),
            c.post(gone, json=body, headers=headers),
            404,
        )
        _same(
            c.post(f"{theirs}/{b_id}/rotate", json={"value": "s-1"}, headers=headers),
            c.post(f"{gone}/{b_id}/rotate", json={"value": "s-1"}, headers=headers),
            404,
        )
        _same(
            c.patch(f"{theirs}/{b_id}", json=patch, headers=headers),
            c.patch(f"{gone}/{b_id}", json=patch, headers=headers),
            404,
        )
    row = owner_session.execute(
        text("SELECT status, secret_version, row_version FROM shifde.secrets WHERE id = :i"),
        {"i": b_id},
    ).one()
    assert tuple(row) == ("active", 1, 1)
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.secrets WHERE tenant_id = :t"), {"t": b.tenant}
        ).scalar_one()
        == 1
    )
    audited = owner_session.execute(
        text(
            "SELECT tenant_id, action, target_id, details::text FROM shifde.audit_events "
            "WHERE action LIKE 'secret.%' AND tenant_id IN (:a, :b) ORDER BY occurred_at"
        ),
        {"a": a.tenant, "b": b.tenant},
    ).all()
    assert [(r[0], r[1], r[2]) for r in audited] == [
        (b.tenant, "secret.created", b_id),
        (a.tenant, "secret.created", own),
        (a.tenant, "secret.rotated", own),
        (a.tenant, "secret.disabled", own),
    ]
    seen = capsys.readouterr()
    for needle in (_A_VALUE, _A_ROTATED, _B_VALUE, "dup-value-1", "stolen-1"):
        assert needle not in seen.out and needle not in seen.err
        assert all(needle not in r[3] for r in audited)


def _all_events(c: Any, url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = c.get(url, params={"limit": 200, **({"cursor": cursor} if cursor else {})}).json()
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            return items


def _chain(owner: Session, tenant: UUID) -> list[UUID]:
    return list(
        owner.execute(
            text(
                "SELECT id FROM shifde.audit_events WHERE tenant_id = :t "
                "AND chain_seq IS NOT NULL ORDER BY chain_seq"
            ),
            {"t": tenant},
        )
        .scalars()
        .all()
    )


def test_a_lists_and_verifies_only_its_own_audit_events(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    """B-1642: A's audit list holds only A's events (none of B's, whatever the filter), A's
    verify walks only A's chain (B's broken chain does not touch it), and B's org is exactly a
    missing org."""
    a, b = world.a, world.b
    with admin_client(settings) as c:
        hb = login(c, people.owner_b)
        made = c.post(
            f"/api/v1/orgs/{b.slug}/secrets", json={"name": "b-only", "value": "b-v-1"}, headers=hb
        )
        assert made.status_code == 201, made.text
        c.cookies.clear()
        headers = login(c, people.owner_a)
        made = c.post(
            f"/api/v1/orgs/{a.slug}/secrets",
            json={"name": "a-only", "value": "a-v-1"},
            headers=headers,
        )
        assert made.status_code == 201, made.text
        url = f"/api/v1/orgs/{a.slug}/audit-events"
        events = _all_events(c, url)
        a_chain, b_chain = _chain(owner_session, a.tenant), _chain(owner_session, b.tenant)
        owner_session.rollback()
        assert a_chain and b_chain
        assert {e["id"] for e in events} >= {str(i) for i in a_chain}
        assert not {e["id"] for e in events} & {str(i) for i in b_chain}
        assert str(b.tenant) not in str(events) and "b-only" not in str(events)
        created = c.get(f"{url}?action=secret.created").json()["items"]
        assert [e["target"] for e in created] == ["secret:a-only"]
        verify = c.get(f"{url}/verify").json()
        assert verify == {"ok": True, "checked": len(a_chain), "first_broken_id": None}
        # under B's org: exactly a missing org
        gone = "/api/v1/orgs/no-such-client/audit-events"
        theirs = f"/api/v1/orgs/{b.slug}/audit-events"
        _same(c.get(theirs), c.get(gone), 404)
        _same(c.get(f"{theirs}/verify"), c.get(f"{gone}/verify"), 404)
    # B's chain broken: A's verify is unaffected, B's reports its own event
    victim = b_chain[-1]
    owner_session.execute(text("ALTER TABLE shifde.audit_events DISABLE TRIGGER USER"))
    try:
        owner_session.execute(
            text(
                "UPDATE shifde.audit_events SET details = details || '{\"forged\": true}' "
                "WHERE id = :i"
            ),
            {"i": victim},
        )
    finally:
        owner_session.execute(text("ALTER TABLE shifde.audit_events ENABLE TRIGGER USER"))
    owner_session.commit()
    with admin_client(settings) as c:
        login(c, people.owner_a)
        verify = c.get(f"/api/v1/orgs/{a.slug}/audit-events/verify").json()
        assert verify["ok"] is True and verify["checked"] >= len(a_chain)
        c.cookies.clear()
        login(c, people.owner_b)
        broken = c.get(f"/api/v1/orgs/{b.slug}/audit-events/verify").json()
        assert broken["ok"] is False and broken["first_broken_id"] == str(victim)
