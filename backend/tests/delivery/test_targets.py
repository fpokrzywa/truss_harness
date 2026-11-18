"""Deployment targets through the routes, as `shifde_app` (design 3, 9; B-2501): register, list,
get, archive, address refusal before any request, validation, permissions, audit, the setting
and the seeded delivery permissions.
"""

from __future__ import annotations

import socket
from typing import Any  # Any: monkeypatched callables

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from tests.connections.routes import as_user, limited_user, system_role_user
from tests.delivery.routes import (
    address,
    counts,
    delivery_client,
    register,
    target_body,
    targets,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws_slug

ALL_KEYS = {
    "delivery:target:read",
    "delivery:target:write",
    "delivery:plan:read",
    "delivery:plan:request",
    "delivery:plan:approve",
    "delivery:deployment:read",
    "delivery:deployment:resolve",
    "delivery:executor:read",
    "delivery:executor:manage",
}
DEVELOPER_KEYS = {
    "delivery:target:read",
    "delivery:target:write",
    "delivery:plan:read",
    "delivery:plan:request",
    "delivery:deployment:read",
    "delivery:executor:read",
}
OUT_FIELDS = {
    "id",
    "name",
    "workspace",
    "project",
    "platform_kind",
    "address",
    "environment",
    "bindings",
    "credential_ref",
    "status",
    "revision",
    "created_by",
    "created_at",
    "updated_at",
}


def test_register_list_get_archive(client: TestClient, world: World, people: People) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    host = address().removeprefix("https://")
    body = target_body(ws_slug(a), f"HTTPS://{host.upper()}:443/", project=project_slug(a))
    made = register(client, h, a.slug, body)
    assert set(made) == OUT_FIELDS  # never claim_id or address_key
    assert made["address"] == f"https://{host}"
    assert made["workspace"] == ws_slug(a) and made["project"] == project_slug(a)
    assert made["status"] == "active" and made["revision"] == 1
    assert made["bindings"] == {"warehouse_id": "abc123def456"}
    assert made["platform_kind"] == "databricks" and made["environment"] == "dev"
    listed = client.get(targets(a.slug), headers=h)
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()["items"]] == [made["id"]]
    got = client.get(f"{targets(a.slug)}/{made['id']}", headers=h)
    assert got.status_code == 200 and got.json() == made
    archived = client.post(f"{targets(a.slug)}/{made['id']}/archive", headers=h)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived" and archived.json()["revision"] == 2
    assert set(archived.json()) == OUT_FIELDS
    active = client.get(f"{targets(a.slug)}?status=active", headers=h).json()["items"]
    assert active == []
    again = client.post(f"{targets(a.slug)}/{made['id']}/archive", headers=h)
    assert again.status_code == 409 and again.json()["code"] == "conflict", again.text
    assert client.get(f"{targets(a.slug)}/{made['id']}", headers=h).json()["revision"] == 2


def test_the_example_address_from_the_acceptance_criteria(
    client: TestClient, world: World, people: People
) -> None:
    h = as_user(client, people.owner_a)
    made = register(
        client,
        h,
        world.a.slug,
        target_body(ws_slug(world.a), "https://ADB-1.cloud.databricks.com:443/"),
    )
    assert made["address"] == "https://adb-1.cloud.databricks.com"


def test_an_address_off_the_allow_list_is_refused_before_any_request(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = as_user(client, people.owner_a)
    attempts: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args)
        raise AssertionError("no network call may be attempted")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    for url in (
        "https://example.com",
        "http://adb-1.cloud.databricks.com",
        "https://adb-1.cloud.databricks.com/path",
        "https://169.254.169.254",
        "https://evil-cloud.databricks.com.attacker.net",
    ):
        r = client.post(targets(world.a.slug), headers=h, json=target_body(ws_slug(world.a), url))
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "validation_error"
        assert url not in r.text
    assert attempts == []
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_an_installation_suffix_is_accepted_only_when_configured(
    settings: Settings, world: World, people: People
) -> None:
    url = address("databricks.corp.example")
    for configured, status in (([], 422), (["databricks.corp.example"], 201)):
        cfg = settings.model_copy(update={"delivery_databricks_host_suffixes": configured})
        with delivery_client(cfg) as c:
            h = as_user(c, people.owner_a)
            body = target_body(ws_slug(world.a), url)
            register(c, h, world.a.slug, body, status=status)


@pytest.mark.parametrize(
    "bindings",
    [
        {},
        {"warehouse_id": "abc", "catalog": "main"},
        {"warehouse_id": "bad id!"},
        {"warehouse_id": ""},
    ],
)
def test_bindings_are_validated(
    client: TestClient, world: World, people: People, owner_db: Database, bindings: dict[str, str]
) -> None:
    h = as_user(client, people.owner_a)
    body = target_body(ws_slug(world.a), address(), bindings=bindings)
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"credential_ref": "https://token@host"},
        {"credential_ref": "dapi0123 secret"},
        {"environment": "staging"},
        {"platform_kind": "snowflake"},
        {"claim_id": "00000000-0000-0000-0000-000000000000"},
        {"name": ""},
    ],
)
def test_the_body_is_validated(
    client: TestClient, world: World, people: People, overrides: dict[str, str]
) -> None:
    h = as_user(client, people.owner_a)
    body = target_body(ws_slug(world.a), address(), **overrides)
    assert client.post(targets(world.a.slug), headers=h, json=body).status_code == 422


def test_an_archived_workspace_takes_no_target(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.workspaces SET status = 'archived' WHERE id = :w"),
            {"w": world.a.ws},
        )
        o.commit()
    h = as_user(client, people.owner_a)
    r = client.post(targets(world.a.slug), headers=h, json=target_body(ws_slug(world.a), address()))
    assert r.status_code == 409 and r.json()["code"] == "scope_archived", r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_a_developer_registers_and_archives(
    client: TestClient, world: World, owner_db: Database
) -> None:
    email = system_role_user(owner_db, world.a.tenant, "developer")
    h = as_user(client, email)
    made = register(client, h, world.a.slug, target_body(ws_slug(world.a), address()))
    archived = client.post(f"{targets(world.a.slug)}/{made['id']}/archive", headers=h)
    assert archived.status_code == 200, archived.text


def test_a_viewer_cannot_register(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.viewer_a)
    r = client.post(targets(world.a.slug), headers=h, json=target_body(ws_slug(world.a), address()))
    assert r.status_code == 403, r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_a_member_without_delivery_permission_sees_nothing(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    made = register(client, h, world.a.slug, target_body(ws_slug(world.a), address()))
    email, _ = limited_user(owner_db, world.a.tenant, ["workspace:read"])
    h = as_user(client, email)
    url = f"{targets(world.a.slug)}/{made['id']}"
    missing = client.get(f"{targets(world.a.slug)}/{made['id'][:-4]}0000", headers=h)
    got = client.get(url, headers=h)
    assert got.status_code == 404 and got.json()["detail"] == missing.json()["detail"], got.text
    archive = client.post(f"{url}/archive", headers=h)
    assert archive.status_code == 404, archive.text
    assert client.get(targets(world.a.slug), headers=h).status_code == 403


def test_a_reader_cannot_archive(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    made = register(client, h, world.a.slug, target_body(ws_slug(world.a), address()))
    email, _ = limited_user(owner_db, world.a.tenant, ["delivery:target:read"])
    h = as_user(client, email)
    r = client.post(f"{targets(world.a.slug)}/{made['id']}/archive", headers=h)
    assert r.status_code == 403, r.text


def test_register_and_archive_are_audited(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    made = register(client, h, world.a.slug, target_body(ws_slug(world.a), address()))
    assert client.post(f"{targets(world.a.slug)}/{made['id']}/archive", headers=h).is_success
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT action, target_type, workspace_id, details FROM shifde.audit_events "
                "WHERE tenant_id = :t AND target_id = :i ORDER BY chain_seq"
            ),
            {"t": world.a.tenant, "i": made["id"]},
        ).all()
    assert [r.action for r in rows] == ["delivery.target.registered", "delivery.target.archived"]
    for row in rows:
        assert row.target_type == "delivery_target" and row.workspace_id == world.a.ws
        assert row.details["name"] == made["name"] and row.details["environment"] == "dev"
        assert row.details["platform_kind"] == "databricks"
        assert row.details["workspace_id"] == str(world.a.ws)
        assert made["address"] not in str(row.details)


@pytest.mark.parametrize(
    "value",
    [
        "*.corp.example",
        ".corp.example",
        "https://corp.example",
        "corp.example:443",
        "corp",
        "a b.c",
    ],
)
def test_a_malformed_installation_suffix_is_refused(value: str) -> None:
    base = {"_env_file": None, "database_url": "postgresql+psycopg://x", "env": "test"}
    with pytest.raises(ValidationError):
        Settings(**base, delivery_databricks_host_suffixes=value)


def test_installation_suffixes_are_split_and_lowercased() -> None:
    base = {"_env_file": None, "database_url": "postgresql+psycopg://x", "env": "test"}
    s = Settings(**base, delivery_databricks_host_suffixes=" Databricks.Corp.Example , a.b-c.io ")
    assert s.delivery_databricks_host_suffixes == ["databricks.corp.example", "a.b-c.io"]
    assert Settings(**base).delivery_databricks_host_suffixes == []


def test_the_catalog_holds_the_nine_delivery_keys(owner_session: Session) -> None:
    rows = owner_session.execute(
        text("SELECT key, is_privileged FROM shifde.permissions WHERE key LIKE 'delivery:%'")
    ).all()
    assert {r.key for r in rows} == ALL_KEYS
    assert {r.key for r in rows if r.is_privileged} == {
        "delivery:plan:approve",
        "delivery:deployment:resolve",
        "delivery:executor:manage",
    }
    grants = owner_session.execute(
        text(
            "SELECT r.key, rp.permission_key FROM shifde.role_permissions rp "
            "JOIN shifde.roles r ON r.id = rp.role_id "
            "WHERE rp.tenant_id IS NULL AND rp.permission_key LIKE 'delivery:%'"
        )
    ).all()
    by_role: dict[str, set[str]] = {}
    for role, key in grants:
        by_role.setdefault(role, set()).add(key)
    assert by_role == {"tenant_administrator": ALL_KEYS, "developer": DEVELOPER_KEYS}
