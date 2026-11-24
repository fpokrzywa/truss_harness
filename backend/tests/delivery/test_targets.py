"""Deployment targets through the routes, as `shifde_app` (design 3, 9; B-2501): register, list,
get, archive, address refusal before any request, validation, permissions, audit, the setting
and the seeded delivery permissions; Snowflake targets (SP6-3): their bindings, their address
refusal, `binding_in_use` on one claim and the interim plan refusal; Fabric, Palantir and Docker
targets (SP8-1): their normalized addresses, their bindings, the Palantir installation suffixes
and the same plan refusal.
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
from tests.delivery.releases import release_for_target
from tests.delivery.routes import (
    DOCKER_BINDINGS,
    FABRIC_BINDINGS,
    PALANTIR_BINDINGS,
    SNOWFLAKE_BINDINGS,
    SP8_BINDINGS,
    SP8_KINDS,
    address,
    counts,
    delivery_client,
    register,
    request_plan,
    snowflake_address,
    snowflake_body,
    sp8_address,
    sp8_body,
    target_body,
    targets,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws2_slug, ws_slug

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
    assert made["bindings"] == {"warehouse_id": "abc123def456", "serving_endpoint": None}
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


# ------------------------------------------------------------------ Snowflake targets (SP6-3)
SNOWFLAKE_REFUSED = (
    "the address is not a Snowflake account URL (https://<account>.snowflakecomputing.com)"
)


def test_a_snowflake_target_is_registered_listed_got_and_archived(
    client: TestClient, world: World, people: People
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    example = snowflake_body(ws_slug(a), "https://Acct_1.snowflakecomputing.com")
    made = register(client, h, a.slug, example)
    assert set(made) == OUT_FIELDS
    assert made["platform_kind"] == "snowflake"
    assert made["address"] == "https://acct-1.snowflakecomputing.com"
    assert made["bindings"] == SNOWFLAKE_BINDINGS
    url = snowflake_address()
    host = url.removeprefix("https://").removesuffix(".snowflakecomputing.com")
    private = register(
        client,
        h,
        a.slug,
        snowflake_body(
            ws2_slug(a),
            f"https://{host.upper()}.privatelink.snowflakecomputing.com:443/",
            bindings={**SNOWFLAKE_BINDINGS, "package": "OTHER_PKG", "application": "OTHER_APP"},
        ),
    )
    assert private["address"] == f"https://{host}.privatelink.snowflakecomputing.com"
    listed = client.get(targets(a.slug), headers=h).json()["items"]
    assert {t["id"] for t in listed} == {made["id"], private["id"]}
    got = client.get(f"{targets(a.slug)}/{made['id']}", headers=h)
    assert got.status_code == 200 and got.json() == made
    archived = client.post(f"{targets(a.slug)}/{made['id']}/archive", headers=h)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archived.json()["bindings"] == SNOWFLAKE_BINDINGS


@pytest.mark.parametrize(
    "bindings",
    [
        {**SNOWFLAKE_BINDINGS, "package": "1PKG"},
        {**SNOWFLAKE_BINDINGS, "application": "APP-1"},
        {**SNOWFLAKE_BINDINGS, "warehouse": "WH; DROP"},
        {**SNOWFLAKE_BINDINGS, "cortex_model": "Mistral-Large2"},
        {**SNOWFLAKE_BINDINGS, "cortex_model": "-model"},
        {k: v for k, v in SNOWFLAKE_BINDINGS.items() if k != "cortex_model"},
        {k: v for k, v in SNOWFLAKE_BINDINGS.items() if k != "package"},
        {**SNOWFLAKE_BINDINGS, "extra": "x"},
        {"warehouse_id": "abc123def456"},
    ],
    ids=[
        "package",
        "application",
        "warehouse",
        "cortex-upper",
        "cortex-start",
        "no-cortex",
        "no-package",
        "extra",
        "databricks-bindings",
    ],
)
def test_snowflake_bindings_are_validated(
    client: TestClient, world: World, people: People, owner_db: Database, bindings: dict[str, str]
) -> None:
    h = as_user(client, people.owner_a)
    body = snowflake_body(ws_slug(world.a), snowflake_address(), bindings=bindings)
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert all(e["loc"][:2] == ["body", "bindings"] for e in r.json()["errors"]), r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_snowflake_bindings_on_a_databricks_target_are_refused(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    body = target_body(ws_slug(world.a), address(), bindings=dict(SNOWFLAKE_BINDINGS))
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert all(e["loc"][:2] == ["body", "bindings"] for e in r.json()["errors"]), r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_an_off_pattern_snowflake_address_is_refused_with_the_fixed_message(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    for url in (
        "https://acct1.snowflakecomputing.cn",
        "http://acct1.snowflakecomputing.com",
        "https://acct1.snowflakecomputing.com/path",
        "https://acct1.snowflakecomputing.com:8443",
        "https://adb-1.cloud.databricks.com",
        "https://a.b.c.d.snowflakecomputing.com",
    ):
        body = snowflake_body(ws_slug(world.a), url)
        r = client.post(targets(world.a.slug), headers=h, json=body)
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == SNOWFLAKE_REFUSED
        assert r.json()["errors"][0]["loc"] == ["body", "address"]
        assert url not in r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_a_body_without_platform_kind_is_databricks(
    client: TestClient, world: World, people: People
) -> None:
    h = as_user(client, people.owner_a)
    body = target_body(ws_slug(world.a), address())
    assert "platform_kind" not in body
    assert register(client, h, world.a.slug, body)["platform_kind"] == "databricks"
    snowflake = target_body(ws_slug(world.a), snowflake_address(), bindings=SNOWFLAKE_BINDINGS)
    assert client.post(targets(world.a.slug), headers=h, json=snowflake).status_code == 422


@pytest.mark.parametrize("reused", ["package", "application"])
def test_a_reused_package_or_application_on_one_claim_is_binding_in_use(
    client: TestClient, world: World, people: People, owner_db: Database, reused: str
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    url = snowflake_address()
    first = register(client, h, a.slug, snowflake_body(ws_slug(a), url))
    other = {"package": "OTHER_PKG", "application": "OTHER_APP"}
    # the same name in another case is the same Snowflake object
    bindings = {**SNOWFLAKE_BINDINGS, **other, reused: SNOWFLAKE_BINDINGS[reused].lower()}
    body = snowflake_body(ws2_slug(a), url, bindings=bindings)
    r = client.post(targets(a.slug), headers=h, json=body)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "binding_in_use"
    assert first["id"] not in r.text and first["name"] not in r.text
    assert counts(owner_db, a.tenant) == (1, 1)
    # another account may bind the same names
    register(client, h, a.slug, snowflake_body(ws2_slug(a), snowflake_address()))
    # archiving frees the names on the claim
    assert client.post(f"{targets(a.slug)}/{first['id']}/archive", headers=h).is_success
    assert register(client, h, a.slug, body)["bindings"] == bindings


def test_distinct_names_on_one_claim_are_accepted(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    url = snowflake_address()
    register(client, h, world.a.slug, snowflake_body(ws_slug(world.a), url))
    bindings = {**SNOWFLAKE_BINDINGS, "package": "PKG_2", "application": "APP_2"}
    register(client, h, world.a.slug, snowflake_body(ws2_slug(world.a), url, bindings=bindings))
    assert counts(owner_db, world.a.tenant) == (2, 1)


def test_a_snowflake_target_is_audited_with_its_platform_kind(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    body = snowflake_body(ws_slug(world.a), snowflake_address())
    made = register(client, h, world.a.slug, body)
    with owner_db.session() as o:
        details = o.execute(
            text(
                "SELECT details FROM shifde.audit_events WHERE tenant_id = :t AND target_id = :i "
                "AND action = 'delivery.target.registered'"
            ),
            {"t": world.a.tenant, "i": made["id"]},
        ).scalar_one()
    assert details["platform_kind"] == "snowflake"
    assert made["address"] not in str(details)


def test_a_plan_request_on_a_snowflake_target_is_refused_without_a_write(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    body = snowflake_body(ws_slug(world.a), snowflake_address())
    made = register(client, h, world.a.slug, body)
    release = release_for_target(owner_db, made["id"]).id

    def rows() -> tuple[int, int, int]:
        with owner_db.session() as o:
            row = o.execute(
                text(
                    "SELECT (SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                    "(SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t), "
                    "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t)"
                ),
                {"t": world.a.tenant},
            ).one()
        return int(row[0]), int(row[1]), int(row[2])

    h = as_user(client, people.owner_a)
    refused = request_plan(client, h, world.a.slug, made["id"], release, status=409)
    assert refused["code"] == "release_target_mismatch"
    assert rows() == (0, 0, 0)


# ------------------------------------------------------------------ Fabric, Palantir, Docker (SP8-1)
GUID = "0A1B2C3D-0000-4000-8000-00000000ABCD"
SENT = {
    "fabric": f"https://app.fabric.microsoft.com/groups/{GUID}/lakehouses",
    "palantir": "HTTPS://Acme-Stack.PalantirFoundry.com:443/",
    "docker": "docker://0a1b2c3d-0000-4000-8000-00000000abcd",
}
NORMALIZED = {
    "fabric": "https://api.fabric.microsoft.com/v1/workspaces/0a1b2c3d-0000-4000-8000-00000000abcd",
    "palantir": "https://acme-stack.palantirfoundry.com",
    "docker": "docker://0a1b2c3d-0000-4000-8000-00000000abcd",
}
BAD_ADDRESSES = {
    "fabric": [
        "{" + GUID + "}",
        f"urn:uuid:{GUID}",
        f"https://app.fabric.microsoft.com/groups/{GUID}?x",
        "https://adb-1.cloud.databricks.com",
    ],
    "palantir": [
        "https://acme.palantirfoundry.com:8443",
        "https://acme.palantirfoundry.com/path",
        "https://10.0.0.1",
        "https://acme.cloud.databricks.com",
    ],
    "docker": [
        "docker://0A1B2C3D-0000-4000-8000-00000000ABCD",
        "tcp://127.0.0.1:2375",
        "unix:///var/run/docker.sock",
        "https://adb-1.cloud.databricks.com",
    ],
}
BAD_BINDINGS: dict[str, list[dict[str, Any]]] = {
    "fabric": [
        {**FABRIC_BINDINGS, "lakehouse_name": "1Lake"},
        {**FABRIC_BINDINGS, "notebook_name": "Sales-Notebook"},
        {**FABRIC_BINDINGS, "notebook_name": FABRIC_BINDINGS["lakehouse_name"]},
        {k: v for k, v in FABRIC_BINDINGS.items() if k != "notebook_name"},
        {**FABRIC_BINDINGS, "extra": "x"},
    ],
    "palantir": [
        {**PALANTIR_BINDINGS, "marketplace_rid": "ri.marketplace.main.folder.0a1b2c3d"},
        {**PALANTIR_BINDINGS, "ontology_rid": PALANTIR_BINDINGS["namespace_rid"]},
        {**PALANTIR_BINDINGS, "product_id": "not-a-uuid"},
        {**PALANTIR_BINDINGS, "object_type": "1Example"},
        {**PALANTIR_BINDINGS, "frontend_origin": "http://frontend.example.com"},
        {**PALANTIR_BINDINGS, "frontend_origin": "https://frontend.example.com/app"},
        {**PALANTIR_BINDINGS, "frontend_origin": "https://[frontend.example.com]"},
        {**PALANTIR_BINDINGS, "frontend_asset_sha256": "A" * 64},
        {k: v for k, v in PALANTIR_BINDINGS.items() if k != "allow_preview_apis"},
        {**PALANTIR_BINDINGS, "extra": "x"},
    ],
    "docker": [
        {**DOCKER_BINDINGS, "project": "Sales"},
        {**DOCKER_BINDINGS, "port": 80},
        {**DOCKER_BINDINGS, "port": 65536},
        {**DOCKER_BINDINGS, "workspace_host": "https://ADB-1.cloud.databricks.com"},
        {**DOCKER_BINDINGS, "workspace_host": "https://adb-1.cloud.databricks.com/path"},
        {**DOCKER_BINDINGS, "warehouse_id": "wh; drop"},
        {k: v for k, v in DOCKER_BINDINGS.items() if k != "serving_endpoint"},
        {**DOCKER_BINDINGS, "extra": "x"},
    ],
}


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_an_sp8_target_is_registered_with_its_normalized_address_and_bindings(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    h = as_user(client, people.owner_a)
    made = register(client, h, world.a.slug, sp8_body(kind, ws_slug(world.a), SENT[kind]))
    assert made["platform_kind"] == kind
    assert made["address"] == NORMALIZED[kind]
    assert made["bindings"] == SP8_BINDINGS[kind]
    assert made["credential_ref"] == f"{kind}-dev"
    got = client.get(f"{targets(world.a.slug)}/{made['id']}", headers=h)
    assert got.status_code == 200 and got.json() == made, got.text
    assert counts(owner_db, world.a.tenant) == (1, 1)
    archived = client.post(f"{targets(world.a.slug)}/{made['id']}/archive", headers=h)
    assert archived.status_code == 200 and archived.json()["status"] == "archived", archived.text
    assert counts(owner_db, world.a.tenant) == (1, 0)


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_sp8_bindings_are_validated(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    h = as_user(client, people.owner_a)
    for bindings in BAD_BINDINGS[kind]:
        body = sp8_body(kind, ws_slug(world.a), sp8_address(kind), bindings=bindings)
        r = client.post(targets(world.a.slug), headers=h, json=body)
        assert r.status_code == 422, (bindings, r.text)
        assert all(e["loc"][:2] == ["body", "bindings"] for e in r.json()["errors"]), r.text
        for value in bindings.values():
            if isinstance(value, str) and len(value) > 8:
                assert value not in r.text  # no message echoes a binding
    assert counts(owner_db, world.a.tenant) == (0, 0)


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_bindings_of_another_platform_are_refused(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    h = as_user(client, people.owner_a)
    others = [
        dict(SNOWFLAKE_BINDINGS),
        {"warehouse_id": "abc123def456"},
        *(dict(SP8_BINDINGS[k]) for k in SP8_KINDS if k != kind),
    ]
    for bindings in others:
        body = sp8_body(kind, ws_slug(world.a), sp8_address(kind), bindings=bindings)
        r = client.post(targets(world.a.slug), headers=h, json=body)
        assert r.status_code == 422, (bindings, r.text)
        assert all(e["loc"][:2] == ["body", "bindings"] for e in r.json()["errors"]), r.text
    # and the kind's own bindings on a Databricks target
    body = target_body(ws_slug(world.a), address(), bindings=dict(SP8_BINDINGS[kind]))
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_bad_sp8_address_is_refused_with_the_fixed_message(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    h = as_user(client, people.owner_a)
    for url in BAD_ADDRESSES[kind]:
        r = client.post(
            targets(world.a.slug), headers=h, json=sp8_body(kind, ws_slug(world.a), url)
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "validation_error"
        assert r.json()["errors"][0]["loc"] == ["body", "address"]
        assert url not in r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_a_palantir_installation_suffix_is_accepted_only_when_configured(
    settings: Settings, world: World, people: People
) -> None:
    """`SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES` admits a Palantir stack only; the Databricks list
    never does, and the Palantir list never admits a Databricks workspace."""
    url = "https://stack-1.foundry.corp.example"
    for update, status in (
        ({}, 422),
        ({"delivery_databricks_host_suffixes": ["foundry.corp.example"]}, 422),
        ({"delivery_palantir_host_suffixes": ["foundry.corp.example"]}, 201),
    ):
        with delivery_client(settings.model_copy(update=update)) as c:
            h = as_user(c, people.owner_a)
            register(c, h, world.a.slug, sp8_body("palantir", ws_slug(world.a), url), status)
    dbx = address("foundry.corp.example")
    cfg = settings.model_copy(update={"delivery_palantir_host_suffixes": ["foundry.corp.example"]})
    with delivery_client(cfg) as c:
        h = as_user(c, people.owner_a)
        register(c, h, world.a.slug, target_body(ws_slug(world.a), dbx), status=422)


def test_palantir_installation_suffixes_are_split_and_validated() -> None:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://x",
        "env": "test",
    }
    s = Settings(**base, delivery_palantir_host_suffixes=" Foundry.Corp.Example , a.b-c.io ")
    assert s.delivery_palantir_host_suffixes == ["foundry.corp.example", "a.b-c.io"]
    assert Settings(**base).delivery_palantir_host_suffixes == []
    with pytest.raises(ValidationError, match="SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES"):
        Settings(**base, delivery_palantir_host_suffixes="*.example.com")


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_plan_request_on_an_sp8_target_is_refused_without_a_write(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    h = as_user(client, people.owner_a)
    made = register(client, h, world.a.slug, sp8_body(kind, ws_slug(world.a), sp8_address(kind)))
    release = release_for_target(owner_db, made["id"]).id

    def rows() -> tuple[int, int, int]:
        with owner_db.session() as o:
            row = o.execute(
                text(
                    "SELECT (SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                    "(SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t), "
                    "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t)"
                ),
                {"t": world.a.tenant},
            ).one()
        return int(row[0]), int(row[1]), int(row[2])

    h = as_user(client, people.owner_a)
    refused = request_plan(client, h, world.a.slug, made["id"], release, status=409)
    assert refused["code"] == "release_target_mismatch"
    assert rows() == (0, 0, 0)
