"""Connection scope isolation in the service (#156 CM-1, design 2026-11-22 sections 4, 6 and 10,
DECISIONS B-2745, B-2746, B-2754).

* Names are unique within a scope: the same name in two projects, in a workspace and at client
  level is allowed; twice in one scope is 409, on create and on rename.
* A connection's secret has exactly the connection's scope: an unreadable secret is still the 404
  of an unknown id; a readable one of another scope is 422 `secret_scope_mismatch`, on create and
  on PATCH. As `shifde_app` the database refuses the same (23514), and the scope columns cannot be
  updated at all.
* `include_shared` lists a project's own rows plus the workspace-wide and client-wide ones it
  shares, intersected with what the caller can read, across one cursor.
* A model provider needs a client-wide connection (422 from the service, 23514 in the database).
* `last_test_outcome` is the outcome of the latest test, on get and list.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.adapters.testservers import RestTestServer
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.conntests import run_test
from tests._support.tenants import add_project
from tests.connections.routes import (
    REST_TOKEN,
    as_user,
    connection,
    grant,
    limited_user,
    org,
    rest_body,
    secret,
)
from tests.models_admin.routes import models_client
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, project_slug, ws2_slug, ws_slug

MISMATCH = "the secret must have the connection's scope"
DUPLICATE = "a connection with this name already exists in this scope"
PROVIDER_SCOPE = "a model provider needs a client-wide model_api connection"


def _project2(owner_db: Database, world: World) -> tuple[UUID, str]:
    """A second project of A's first workspace: its id and slug."""
    with owner_db.session() as o:
        pid = add_project(o, world.a.tenant, world.a.ws)
        o.commit()
    return pid, f"p-{pid.hex}"


def _p1(world: World) -> dict[str, str]:
    return {"workspace": ws_slug(world.a), "project": project_slug(world.a)}


def _mismatch(r: Any) -> None:
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "secret_scope_mismatch", r.text
    assert r.json()["detail"] == MISMATCH, r.text


def _confirmed(body: dict[str, Any]) -> dict[str, Any]:
    return {"access_mode": "read", "read_only_confirmed": True, **body}


# ------------------------------------------------------------------ names
def test_names_are_unique_per_scope_on_create_and_rename(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    _, p2 = _project2(owner_db, world)
    h = as_user(client, people.owner_a)
    scopes = [
        _p1(world),
        {"workspace": ws_slug(a), "project": p2},
        {"workspace": ws_slug(a)},
        {"workspace": ws2_slug(a)},
        {},
    ]
    server = {"base_url": "https://api.example.com"}
    body = {"name": "same", "connection_type": "rest", "adapter_key": "rest.http", "config": server}
    made = [connection(client, h, {**body, "scope": scope}, A)["id"] for scope in scopes]
    assert len(set(made)) == len(scopes)

    for scope in scopes:  # a second "same" in any one of these scopes
        r = client.post(f"{A}/connections", headers=h, json={**body, "scope": scope})
        assert r.status_code == 409 and r.json()["detail"] == DUPLICATE, r.text

    other = connection(client, h, {**body, "name": "other", "scope": _p1(world)}, A)
    r = client.patch(
        f"{A}/connections/{other['id']}",
        headers=h,
        json={"name": "same", "row_version": other["row_version"]},
    )
    assert r.status_code == 409 and r.json()["detail"] == DUPLICATE, r.text
    kept = client.get(f"{A}/connections/{other['id']}", headers=h).json()
    assert kept["name"] == "other" and kept["row_version"] == other["row_version"]


# ------------------------------------------------------------------ secret scope
def test_a_p2_connection_with_a_p1_secret(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """A P2 manager who cannot read the P1 secret gets the 404 of an unknown secret; the tenant
    administrator, who can, gets 422 `secret_scope_mismatch`. The same on PATCH."""
    a, A = world.a, org(world.a.slug)
    p2_id, p2 = _project2(owner_db, world)
    p1_secret = secret(
        app_db, settings, a.tenant, REST_TOKEN, scope_type="project", scope_id=a.project
    )
    p2_secret = secret(app_db, settings, a.tenant, REST_TOKEN, scope_type="project", scope_id=p2_id)
    manager, manager_id = limited_user(owner_db, a.tenant, [], label="p2-manager")
    grant(
        owner_db,
        a.tenant,
        manager_id,
        [
            "workspace:read",
            "project:read",
            "connection:read",
            "connection:manage",
            "secret:read",
            "secret:use",
        ],
        scope_type="project",
        scope_id=p2_id,
    )
    scope = {"workspace": ws_slug(a), "project": p2}

    def create(h: dict[str, str], sid: str) -> Any:
        body = _confirmed({**rest_body(rest_server, sid, name=f"c-{uuid7().hex}"), "scope": scope})
        return client.post(f"{A}/connections", headers=h, json=body)

    hm = as_user(client, manager)
    r = create(hm, p1_secret)
    assert r.status_code == 404 and r.json()["detail"] == "secret not found", r.text
    own = create(hm, p2_secret)
    assert own.status_code == 201, own.text
    cid, version = own.json()["id"], own.json()["row_version"]
    patch = {"secret_id": p1_secret, "row_version": version, "read_only_confirmed": True}
    r = client.patch(f"{A}/connections/{cid}", headers=hm, json=patch)
    assert r.status_code == 404 and r.json()["detail"] == "secret not found", r.text

    ho = as_user(client, people.owner_a)
    _mismatch(create(ho, p1_secret))
    _mismatch(client.patch(f"{A}/connections/{cid}", headers=ho, json=patch))
    kept = client.get(f"{A}/connections/{cid}", headers=ho).json()
    assert kept["secret_id"] == p2_secret and kept["row_version"] == version


def test_a_tenant_secret_on_a_project_connection_and_a_project_secret_on_a_client_one(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A = world.a, org(world.a.slug)
    tenant_secret = secret(app_db, settings, a.tenant, REST_TOKEN)
    project_secret = secret(
        app_db, settings, a.tenant, REST_TOKEN, scope_type="project", scope_id=a.project
    )
    h = as_user(client, people.owner_a)
    for sid, scope in ((tenant_secret, _p1(world)), (project_secret, {})):
        body = _confirmed({**rest_body(rest_server, sid, name=f"c-{uuid7().hex}"), "scope": scope})
        _mismatch(client.post(f"{A}/connections", headers=h, json=body))
    # each in its own scope is accepted
    connection(client, h, {**rest_body(rest_server, tenant_secret, name="client"), "scope": {}}, A)
    connection(
        client, h, {**rest_body(rest_server, project_secret, name="p1"), "scope": _p1(world)}, A
    )


def _sqlstate(err: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    return getattr(err.value.orig, "sqlstate", None)


_INSERT = text(
    "INSERT INTO shifde.connections (id, tenant_id, workspace_id, project_id, name, "
    "connection_type, adapter_key, config, auth_method, secret_ref_id, access_mode) "
    "VALUES (:i, :t, :w, :p, :n, :ct, :ak, CAST('{}' AS jsonb), 'bearer', :s, 'read')"
)


def _raw(
    s: Any,
    tenant: UUID,
    workspace: UUID | None,
    project: UUID | None,
    secret_id: str | None,
    connection_type: str = "rest",
    adapter_key: str = "rest.http",
) -> UUID:
    cid = uuid7()
    s.execute(
        _INSERT,
        {
            "i": cid,
            "t": tenant,
            "w": workspace,
            "p": project,
            "n": f"raw-{cid.hex}",
            "ct": connection_type,
            "ak": adapter_key,
            "s": secret_id,
        },
    )
    return cid


def test_the_database_refuses_the_wrong_scope_as_the_app_role(
    world: World, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    a = world.a
    p2_id, _ = _project2(owner_db, world)
    p1_secret = secret(
        app_db, settings, a.tenant, REST_TOKEN, scope_type="project", scope_id=a.project
    )
    p2_secret = secret(app_db, settings, a.tenant, REST_TOKEN, scope_type="project", scope_id=p2_id)
    with app_session(app_db, a.tenant) as s, pytest.raises(DBAPIError) as err:
        _raw(s, a.tenant, a.ws, p2_id, p1_secret)
    assert _sqlstate(err) == "23514"
    assert "secret scope must match the connection scope" in str(err.value.orig)

    with app_session(app_db, a.tenant) as s:
        cid = _raw(s, a.tenant, a.ws, p2_id, p2_secret)
        s.commit()
    with app_session(app_db, a.tenant) as s, pytest.raises(DBAPIError) as err:
        s.execute(
            text("UPDATE shifde.connections SET secret_ref_id = :x WHERE id = :i"),
            {"x": p1_secret, "i": cid},
        )
    assert _sqlstate(err) == "23514"

    for column, value in (("workspace_id", a.ws2), ("project_id", a.project)):
        with app_session(app_db, a.tenant) as s, pytest.raises(DBAPIError) as err:
            s.execute(
                text(f"UPDATE shifde.connections SET {column} = :x WHERE id = :i"),  # nosec B608
                {"x": value, "i": cid},
            )
        assert _sqlstate(err) == "42501", column  # insufficient_privilege


# ------------------------------------------------------------------ include_shared
def _ids(client: TestClient, url: str, h: dict[str, str]) -> list[str]:
    r = client.get(url, headers=h)
    assert r.status_code == 200, r.text
    return [c["id"] for c in r.json()["items"]]


def _paged(client: TestClient, url: str, h: dict[str, str]) -> list[str]:
    got: list[str] = []
    cursor: str | None = None
    while True:
        r = client.get(url + "&limit=1" + (f"&cursor={cursor}" if cursor else ""), headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) <= 1
        got += [c["id"] for c in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return got


def test_include_shared_lists_the_projects_own_and_shared_rows(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    _, p2 = _project2(owner_db, world)
    h = as_user(client, people.owner_a)
    server = {"base_url": "https://api.example.com"}
    base = {"connection_type": "rest", "adapter_key": "rest.http", "config": server}

    def made(name: str, scope: dict[str, str]) -> str:
        return str(connection(client, h, {**base, "name": name, "scope": scope}, A)["id"])

    p1_row = made("p1", _p1(world))
    p2_row = made("p2", {"workspace": ws_slug(a), "project": p2})
    ws_row = made("ws", {"workspace": ws_slug(a)})
    ws2_row = made("ws2", {"workspace": ws2_slug(a)})
    client_row = made("client", {})
    assert len({p1_row, p2_row, ws_row, ws2_row, client_row}) == 5

    q = f"{A}/connections?workspace={ws_slug(a)}&project={project_slug(a)}"
    shared = sorted([p1_row, ws_row, client_row])
    assert _ids(client, q + "&include_shared=true&limit=200", h) == shared
    assert _paged(client, q + "&include_shared=true", h) == shared
    assert _ids(client, q + "&limit=200", h) == [p1_row]  # without it: exactly as before
    assert _ids(client, q + "&include_shared=false&limit=200", h) == [p1_row]
    ws_q = f"{A}/connections?workspace={ws_slug(a)}&include_shared=true&limit=200"
    assert _ids(client, ws_q, h) == sorted([ws_row, client_row])

    # a member who reads connections of P1 and of the workspace, not client-wide ones
    member, member_id = limited_user(owner_db, a.tenant, [], label="p1-reader")
    grant(
        owner_db,
        a.tenant,
        member_id,
        ["workspace:read", "project:read", "connection:read"],
        scope_type="project",
        scope_id=a.project,
    )
    hm = as_user(client, member)
    assert _ids(client, q + "&include_shared=true&limit=200", hm) == [p1_row]
    grant(
        owner_db,
        a.tenant,
        member_id,
        ["workspace:read", "connection:read"],
        scope_type="workspace",
        scope_id=a.ws,
    )
    hm = as_user(client, member)
    assert _ids(client, q + "&include_shared=true&limit=200", hm) == sorted([p1_row, ws_row])
    assert _paged(client, q + "&include_shared=true", hm) == sorted([p1_row, ws_row])


def test_include_shared_requires_a_workspace(
    client: TestClient, world: World, people: People
) -> None:
    h = as_user(client, people.owner_a)
    r = client.get(f"{org(world.a.slug)}/connections?include_shared=true", headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "`include_shared` requires `workspace`", r.text


# ------------------------------------------------------------------ model providers
def test_a_model_provider_over_a_project_connection_is_refused(
    world: World, people: People, app_db: Database, settings: Settings
) -> None:
    a, A = world.a, org(world.a.slug)
    with models_client(settings) as mc:
        h = as_user(mc, people.owner_a)
        body = {"connection_type": "model_api", "adapter_key": "fake", "config": {}}
        conn = connection(mc, h, {**body, "name": "project-model", "scope": _p1(world)}, A)
        r = mc.post(
            f"{A}/model-providers",
            headers=h,
            json={"name": "prov", "adapter_key": "fake", "connection_id": conn["id"]},
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "validation_error" and r.json()["detail"] == PROVIDER_SCOPE
        listed = mc.get(f"{A}/model-providers?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        assert all(p["connection_id"] != conn["id"] for p in listed.json()["items"])

    with app_session(app_db, a.tenant) as s, pytest.raises(DBAPIError) as err:
        s.execute(
            text(
                "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, "
                "connection_id) VALUES (:i, :t, 'raw', 'fake', :c)"
            ),
            {"i": uuid7(), "t": a.tenant, "c": conn["id"]},
        )
    assert _sqlstate(err) == "23514"
    assert "a model provider needs a client-wide connection" in str(err.value.orig)


# ------------------------------------------------------------------ last_test_outcome
def test_last_test_outcome_follows_the_latest_test(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A = world.a, org(world.a.slug)
    sid = secret(app_db, settings, a.tenant, REST_TOKEN)
    h = as_user(client, people.owner_a)
    made = connection(client, h, {**rest_body(rest_server, sid), "scope": {}}, A)
    cid = made["id"]

    def outcomes() -> tuple[Any, Any]:
        got = client.get(f"{A}/connections/{cid}", headers=h)
        assert got.status_code == 200, got.text
        listed = client.get(f"{A}/connections?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        (row,) = [c for c in listed.json()["items"] if c["id"] == cid]
        return got.json()["last_test_outcome"], row["last_test_outcome"]

    assert made["last_test_outcome"] is None
    assert outcomes() == (None, None)
    passed = run_test(client, h, A, cid)
    assert passed["result"]["outcome"] == "pass", passed
    assert outcomes() == ("pass", "pass")
    rest_server.token = "another-token"  # the bound credential is refused from now on
    failed = run_test(client, h, A, cid)
    assert failed["result"]["outcome"] == "fail", failed
    assert outcomes() == ("fail", "fail")

    # the write routes return it too, read after their UPDATE ... RETURNING
    row = client.get(f"{A}/connections/{cid}", headers=h).json()
    renamed = client.patch(
        f"{A}/connections/{cid}",
        headers=h,
        json={"name": "renamed", "row_version": row["row_version"]},
    )
    assert renamed.status_code == 200 and renamed.json()["last_test_outcome"] == "fail", (
        renamed.text
    )
    disabled = client.post(
        f"{A}/connections/{cid}/status",
        headers=h,
        json={"status": "disabled", "row_version": renamed.json()["row_version"]},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["last_test_outcome"] == "fail"
