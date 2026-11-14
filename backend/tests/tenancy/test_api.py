"""Tenancy routes: SETH 3ba0e06 `tests/test_api_tenancy.py`, moved (tenancy part 2b).

SETH's seeded `demo-a`/`demo-b` world becomes the tenancy test world (fresh slugs per test) and
its people (`tests/tenancy/routes.py`); the assertions are SETH's. The tests that need the full
app factory or the seeded SETH world (audit, validation, archive, members paging, dashboard
activity, platform administrator listing) move to `app`; `test_platform_status` moves to
runtime with the `workers` table (carry_forward, DECISIONS B-1020).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests.identity.harness import login
from tests.tenancy.conftest import World
from tests.tenancy.routes import (
    People,
    make_people,
    project_slug,
    tenancy_client,
    ws2_slug,
    ws_slug,
)


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with tenancy_client(settings) as c:
        yield c


def _ws(world: World) -> str:
    return f"/api/v1/orgs/{world.a.slug}/workspaces"


def test_member_reads_own_org_workspace_and_project(
    client: TestClient, world: World, people: People
) -> None:
    login(client, people.viewer_a)
    orgs = client.get("/api/v1/orgs").json()
    assert [o["slug"] for o in orgs["items"]] == [world.a.slug] and orgs["next_cursor"] is None
    assert client.get(f"/api/v1/orgs/{world.a.slug}").json()["name"] == world.a.slug
    ws = client.get(_ws(world)).json()["items"]
    assert {w["slug"] for w in ws} == {ws_slug(world.a), ws2_slug(world.a)}
    assert set(ws[0]) >= {"id", "slug", "name", "status", "row_version", "created_at", "updated_at"}
    main = ws_slug(world.a)
    assert client.get(f"{_ws(world)}/{main}").status_code == 200
    projects = client.get(f"{_ws(world)}/{main}/projects").json()["items"]
    assert projects[0]["slug"] == project_slug(world.a) and projects[0]["workspace_slug"] == main
    assert client.get(f"{_ws(world)}/{main}/projects/{project_slug(world.a)}").status_code == 200


def test_cross_tenant_access_is_404_everywhere(
    client: TestClient, world: World, people: People
) -> None:
    headers = login(client, people.owner_a)
    b, main, sample = world.b.slug, ws_slug(world.b), project_slug(world.b)
    for path in (
        f"/api/v1/orgs/{b}",
        f"/api/v1/orgs/{b}/workspaces",
        f"/api/v1/orgs/{b}/workspaces/{main}",
        f"/api/v1/orgs/{b}/workspaces/{main}/projects",
        f"/api/v1/orgs/{b}/workspaces/{main}/projects/{sample}",
        f"/api/v1/orgs/{b}/members",
        f"/api/v1/orgs/{b}/dashboard",
        "/api/v1/orgs/does-not-exist",
    ):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["code"] == "not_found", path
    w = client.post(
        f"/api/v1/orgs/{b}/workspaces", json={"slug": "x", "name": "X"}, headers=headers
    )
    assert w.status_code == 404
    p = client.patch(f"/api/v1/orgs/{b}", json={"name": "pwn", "row_version": 1}, headers=headers)
    assert p.status_code == 404
    # the listing never leaks the other org either
    assert [o["slug"] for o in client.get("/api/v1/orgs").json()["items"]] == [world.a.slug]


def test_viewer_cannot_create_or_manage(client: TestClient, world: World, people: People) -> None:
    headers = login(client, people.viewer_a)
    ws, main = _ws(world), ws_slug(world.a)
    r = client.post(ws, json={"slug": "nope", "name": "Nope"}, headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    patch = {"name": "x", "row_version": 1}
    assert client.patch(f"{ws}/{main}", json=patch, headers=headers).status_code == 403
    new = {"slug": "p", "name": "P"}
    assert client.post(f"{ws}/{main}/projects", json=new, headers=headers).status_code == 403
    assert client.get(f"/api/v1/orgs/{world.a.slug}/members").status_code == 403
    org = f"/api/v1/orgs/{world.a.slug}"
    assert client.patch(org, json=patch, headers=headers).status_code == 403


def _user_id(owner: Session, email: str) -> UUID:
    uid: UUID = owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
    ).scalar_one()
    return uid


def test_workspace_scoped_role_sees_only_its_workspace(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    ws, main, second = _ws(world), ws_slug(world.a), ws2_slug(world.a)
    # demote the viewer to a workspace-scoped Workspace Administrator on `second` only
    with owner_db.session() as owner:
        viewer = _user_id(owner, people.viewer_a)
        owner.execute(text("DELETE FROM shifde.role_bindings WHERE subject_id = :u"), {"u": viewer})
        owner.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) SELECT :i, :t, 'user', :u, r.id, 'workspace', :w "
                "FROM shifde.roles r WHERE r.tenant_id IS NULL "
                "AND r.key = 'workspace_administrator'"
            ),
            {"i": uuid7(), "t": world.a.tenant, "u": viewer, "w": world.a.ws2},
        )
        owner.commit()
    headers = login(client, people.viewer_a)
    assert [w["slug"] for w in client.get(ws).json()["items"]] == [second]
    assert client.get(f"{ws}/{second}").status_code == 200
    assert client.get(f"{ws}/{main}").status_code == 404  # invisible, not 403
    assert client.get(f"{ws}/{main}/projects").status_code == 404
    new = {"slug": "x", "name": "X"}
    assert client.post(f"{ws}/{second}/projects", json=new, headers=headers).status_code == 201
    assert client.post(f"{ws}/{main}/projects", json=new, headers=headers).status_code == 404
    assert client.post(ws, json={"slug": "n", "name": "N"}, headers=headers).status_code == 403
    dash = client.get(f"/api/v1/orgs/{world.a.slug}/dashboard").json()
    assert dash["counts"]["workspaces"] == 1
    # the organization itself stays readable through the workspace grant (B-1019)
    assert client.get(f"/api/v1/orgs/{world.a.slug}").status_code == 200


def test_org_creation_and_scope_all_are_platform_admin_only(
    client: TestClient, people: People
) -> None:
    headers = login(client, people.owner_a)
    r = client.post("/api/v1/orgs", json={"slug": "x", "name": "x"}, headers=headers)
    assert r.status_code == 403
    r = client.get("/api/v1/orgs?scope=all")
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert client.get("/api/v1/orgs?scope=bogus").status_code == 422
