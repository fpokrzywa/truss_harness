"""Cross-client isolation of the admin IAM routes (admin part 1), through a minimal app.

Every route of `admin.api.router` has a test here (`ROUTE_TESTS`). Each first gets the caller's
own object (200/201), then asks for client B's object under A's org (exactly the answer a missing
id gets) and under B's org (exactly the answer a missing org gets), and finds B's rows absent
from A's lists. The platform routes are refused for a client owner with one answer whatever the
id; `POST /orgs/{org}/users` is the platform administrator's.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.admin import api
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests.admin.conftest import admin_client
from tests.identity.harness import login
from tests.tenancy.conftest import World
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
