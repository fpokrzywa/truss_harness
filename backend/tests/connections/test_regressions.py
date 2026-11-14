"""Connections part 2 regressions: read-only credential confirmation (design 5.3, B-1507), archived
scopes take no new connection (findings Item 12, B-1509), the execution context carries the
organization and a row of another client is never driven (carry_forward, B-1505)."""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.runtime import IntegrationRuntime
from shifde.adapters.testservers import RestTestServer
from shifde.authz.service import new_pdp
from shifde.connections.service import ConnectionService
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database, set_tenant
from shifde.core.errors import NotFound
from shifde.core.settings import Settings
from shifde.tenancy.scope import TenantScope
from tests.connections.conftest import owner_vendor
from tests.connections.routes import (
    REGISTRY,
    REST_TOKEN,
    as_user,
    connection,
    org,
    rest_body,
    secret,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, project_slug, ws_slug


def _created_details(owner_db: Database, cid: str, action: str) -> list[dict[str, Any]]:
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT details FROM shifde.audit_events WHERE action = :a AND target_id = :i "
                "ORDER BY occurred_at"
            ),
            {"a": action, "i": cid},
        ).scalars()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ design 5.3
def test_a_read_connection_with_a_credential_needs_the_read_only_confirmation(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    sid = secret(app_db, settings, world.a.tenant, REST_TOKEN)
    body = rest_body(rest_server, sid, name="unconfirmed")
    for confirmation in ({}, {"read_only_confirmed": False}, {"read_only_confirmed": None}):
        r = client.post(f"{A}/connections", headers=h, json={**body, **confirmation})
        assert r.status_code == 422, r.text
        assert "read_only_confirmed" in r.text
    assert client.get(f"{A}/connections", headers=h).json()["items"] == []
    made = connection(client, h, {**body, "read_only_confirmed": True}, A)
    details = _created_details(owner_db, made["id"], "connection.created")
    assert details and details[0]["read_only_confirmed"] is True
    # no credential, or a read_write connection (writes stay behind connection:write and the
    # approval gate): nothing to confirm
    anon = connection(client, h, rest_body(rest_server, None, name="anon"), A)
    assert (
        "read_only_confirmed" not in _created_details(owner_db, anon["id"], "connection.created")[0]
    )
    rw = {**rest_body(rest_server, sid, name="rw"), "access_mode": "read_write"}
    assert client.post(f"{A}/connections", headers=h, json=rw).status_code == 201


def test_a_new_credential_or_read_mode_is_confirmed_again(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    first = secret(app_db, settings, world.a.tenant, REST_TOKEN)
    second = secret(app_db, settings, world.a.tenant, REST_TOKEN)
    made = connection(client, h, rest_body(rest_server, first, name="reconfirm"), A)
    url = f"{A}/connections/{made['id']}"

    def patch(body: dict[str, Any]) -> Any:  # Any: a response
        version = client.get(url, headers=h).json()["row_version"]
        return client.patch(url, headers=h, json={**body, "row_version": version})

    assert patch({"name": "renamed"}).status_code == 200  # same credential: no new confirmation
    assert patch({"secret_id": second}).status_code == 422
    assert patch({"secret_id": second, "read_only_confirmed": True}).status_code == 200
    assert patch({"access_mode": "read_write"}).status_code == 200
    assert patch({"access_mode": "read"}).status_code == 422  # relied on as read-only again
    back = patch({"access_mode": "read", "read_only_confirmed": True})
    assert back.status_code == 200 and back.json()["access_mode"] == "read"
    updates = _created_details(owner_db, made["id"], "connection.updated")
    assert [d.get("read_only_confirmed") for d in updates] == [None, True, None, True]


# ------------------------------------------------------------------ findings Item 12
def test_archived_workspaces_and_projects_take_no_new_connection(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    A, h = org(a.slug), as_user(client, people.owner_a)

    def create(name: str, scope: dict[str, str]) -> Any:  # Any: a response
        body = {
            "name": name,
            "scope": scope,
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "config": {"base_url": "https://api.example.com"},
        }
        return client.post(f"{A}/connections", headers=h, json=body)

    in_ws = {"workspace": ws_slug(a)}
    in_project = {"workspace": ws_slug(a), "project": project_slug(a)}
    assert create("ws-open", in_ws).status_code == 201  # active scopes first
    assert create("project-open", in_project).status_code == 201

    def archive(table: str, row: UUID) -> None:
        with owner_db.session() as o:
            set_tenant(o, a.tenant)
            o.execute(
                text(f"UPDATE shifde.{table} SET status = 'archived' WHERE id = :i"), {"i": row}
            )
            o.commit()

    archive("projects", a.project)
    r = create("project-archived", in_project)
    assert r.status_code == 409 and r.json()["code"] == "scope_archived", r.text
    assert create("ws-still-open", in_ws).status_code == 201
    archive("workspaces", a.ws)
    r = create("ws-archived", in_ws)
    assert r.status_code == 409 and r.json()["code"] == "scope_archived", r.text


# ------------------------------------------------------------------ execution context
def test_the_execution_context_carries_the_organization_and_refuses_another_clients_row(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    rows: dict[str, Any] = {}
    for label, c, email in (("a", world.a, people.owner_a), ("b", world.b, people.owner_b)):
        h = as_user(client, email)
        rows[label] = connection(
            client, h, rest_body(rest_server, None, name=f"x-{label}"), org(c.slug)
        )
    with owner_db.session() as o:
        owner_a = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": people.owner_a}
        ).scalar_one()
        b_row = dict(
            o.execute(
                text("SELECT * FROM shifde.connections WHERE id = :i"), {"i": rows["b"]["id"]}
            )
            .mappings()
            .one()
        )
    principal = Principal(user_id=owner_a, email=people.owner_a, display_name=people.owner_a)
    with app_session(app_db, world.a.tenant, owner_a) as s:
        scope = TenantScope(
            session=s,
            ctx=RequestContext(request_id="r-exec", principal=principal, tenant_id=world.a.tenant),
            principal=principal,
            tenant_id=world.a.tenant,
            tenant_slug=world.a.slug,
            tenant_name=world.a.slug,
            tenant_status="active",
            pdp=new_pdp(s, principal, world.a.tenant),
        )
        svc = ConnectionService(scope, IntegrationRuntime(settings=settings, registry=REGISTRY))
        own = svc.get_row(UUID(rows["a"]["id"]))
        _, ctx, _ = svc.prepare_call(own, "probe", require_usable=False)
        assert ctx.tenant_id == world.a.tenant and ctx.principal_id == owner_a
        with pytest.raises(NotFound):
            svc.get_row(UUID(rows["b"]["id"]))
        with pytest.raises(NotFound):  # B's row handed in by another module: never driven
            svc.prepare_call(b_row, "probe", require_usable=False)


# ------------------------------------------------------------------ vendor gate (B-1301)
def test_a_new_connection_naming_a_vendor_that_is_not_active_is_refused(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    rest_server: RestTestServer,
) -> None:
    """carry_forward (vendors): `require_active_vendor` gates new use; a connection that already
    names the vendor keeps working."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    with owner_db.session() as o:
        vendor = owner_vendor(o, world.a.tenant)
        o.commit()
    named = {**rest_body(rest_server, None, "has-vendor"), "vendor_id": str(vendor)}
    existing = connection(client, h, named, A)
    with owner_db.session() as o:
        set_tenant(o, world.a.tenant)
        o.execute(
            text("UPDATE shifde.vendors SET status = 'disabled' WHERE id = :i"), {"i": vendor}
        )
        o.commit()
    refused = client.post(f"{A}/connections", headers=h, json={**named, "name": "new-one"})
    assert refused.status_code == 422 and "disabled" in refused.text, refused.text
    url = f"{A}/connections/{existing['id']}"
    version = client.get(url, headers=h).json()["row_version"]
    kept = client.patch(url, headers=h, json={"name": "still-named", "row_version": version})
    assert kept.status_code == 200 and kept.json()["vendor_id"] == str(vendor), kept.text
