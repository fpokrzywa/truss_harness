"""A connection's own credential (CM-2, issue #160; design 2026-11-22 rule 4.7, section 6,
B-2747..B-2752): inline on create, set or rotated through `PUT .../credential`, disabled on
archive, refused by the admin secrets routes.

The credential is the secret `connection-<connection id>` in the connection's scope, of the kind
its auth method maps to. Creating, setting and rotating it needs `secret:manage` at the
connection's scope as well as `connection:manage` (B-2748). No answer and no audit row contains the
value. Fake adapters and in-process test servers only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any  # Any: JSON bodies, database rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters import oauth2
from shifde.adapters.base import AdapterFailure, ConnectionConfig, ErrorClass, ExecContext
from shifde.adapters.rest_http import RestHttpAdapter
from shifde.adapters.testservers import RestTestServer
from shifde.connections.service import CREDENTIAL_KINDS
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.redaction import REDACTED
from shifde.core.settings import Settings
from shifde.secrets.service import MAX_SECRET_BYTES
from tests._support.conntests import result_of, run_test
from tests.adapters.test_f09_oauth2_rest import OAuth2FakeServer
from tests.admin.conftest import admin_client
from tests.connections.routes import (
    as_user,
    connection,
    grant,
    limited_user,
    org,
    secret,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws_slug

TOKEN = "cm2-credential-token-0001"  # matches no redaction pattern: only the registry masks it
NEW_TOKEN = "cm2-credential-token-0002"
BYTES_MSG = f"secret value must be 1..{MAX_SECRET_BYTES} bytes"
MANAGE_PERMS = [
    "workspace:read",
    "project:read",
    "connection:read",
    "connection:manage",
    "connection:test",
]


@pytest.fixture
def admin(settings: Settings) -> Iterator[TestClient]:
    with admin_client(settings) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_token_cache() -> Iterator[None]:
    oauth2.TOKEN_CACHE.clear()
    yield
    oauth2.TOKEN_CACHE.clear()


# ------------------------------------------------------------------ helpers
def _rest(
    url: str,
    auth: str = "bearer",
    *,
    name: str | None = None,
    mode: str = "read_write",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "name": name or f"rest-{uuid7().hex[-10:]}",
        "connection_type": "rest",
        "adapter_key": "rest.http",
        "config": {"base_url": url, "health_path": "/health"},
        "auth_method": auth,
        "access_mode": mode,
        **extra,
    }


def _put(
    client: TestClient,
    base: str,
    h: dict[str, str],
    cid: str,
    value: str,
    row_version: int | None = None,
    **extra: Any,
) -> Any:  # Any: httpx.Response
    if row_version is None:
        row_version = client.get(f"{base}/connections/{cid}", headers=h).json()["row_version"]
    return client.put(
        f"{base}/connections/{cid}/credential",
        headers=h,
        json={"value": value, "row_version": row_version, **extra},
    )


def _retire(client: TestClient, base: str, h: dict[str, str], cid: str) -> Any:
    row = client.get(f"{base}/connections/{cid}", headers=h).json()
    return client.post(
        f"{base}/connections/{cid}/status",
        headers=h,
        json={"status": "retired", "row_version": row["row_version"]},
    )


def _secret_row(owner_db: Database, tenant: UUID, secret_id: Any) -> dict[str, Any]:
    with owner_db.session() as o:
        set_tenant(o, tenant)
        row = o.execute(
            text("SELECT * FROM shifde.secrets WHERE id = :i"), {"i": secret_id}
        ).mappings()
        return dict(row.one())


def _owned(owner_db: Database, tenant: UUID) -> list[dict[str, Any]]:
    with owner_db.session() as o:
        set_tenant(o, tenant)
        rows = o.execute(
            text(
                "SELECT * FROM shifde.secrets WHERE tenant_id = :t AND name LIKE 'connection-%' "
                "ORDER BY created_at"
            ),
            {"t": tenant},
        ).mappings()
        return [dict(r) for r in rows]


def _connection_row(owner_db: Database, tenant: UUID, cid: Any) -> dict[str, Any]:
    with owner_db.session() as o:
        set_tenant(o, tenant)
        row = o.execute(text("SELECT * FROM shifde.connections WHERE id = :i"), {"i": cid})
        return dict(row.mappings().one())


def _events(owner_db: Database, tenant: UUID, action: str | None = None) -> list[dict[str, Any]]:
    with owner_db.session() as o:
        set_tenant(o, tenant)
        rows = o.execute(
            text(
                "SELECT action, target_type, target_id, workspace_id, project_id, details "
                "FROM shifde.audit_events WHERE tenant_id = :t "
                "AND (CAST(:a AS text) IS NULL OR action = :a) ORDER BY chain_seq"
            ),
            {"t": tenant, "a": action},
        ).mappings()
        return [dict(r) for r in rows]


def _project_scope(world: World) -> dict[str, str]:
    return {"workspace": ws_slug(world.a), "project": project_slug(world.a)}


def _member(owner_db: Database, world: World, perms: list[str], label: str) -> str:
    """A member of A whose only grants are `perms` at A's project."""
    email, uid = limited_user(owner_db, world.a.tenant, [], label=label)
    grant(owner_db, world.a.tenant, uid, perms, scope_type="project", scope_id=world.a.project)
    return email


# ------------------------------------------------------------------ inline create
def test_inline_create_binds_an_owned_secret_of_the_connections_scope(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    url = "https://api.example.com"
    cases = [
        ("bearer", _project_scope(world), "token", "project", a.project),
        ("basic", {"workspace": ws_slug(a)}, "password", "workspace", a.ws),
        ("api_key", {}, "api_key", "tenant", None),
    ]
    for auth, scope, kind, scope_type, scope_id in cases:
        made = connection(client, h, _rest(url, auth, scope=scope, credential={"value": TOKEN}), A)
        assert made["secret_set"] is True and made["secret_id"], made
        assert TOKEN not in json.dumps(made)
        sec = _secret_row(owner_db, a.tenant, made["secret_id"])
        assert sec["name"] == f"connection-{made['id']}"
        assert (sec["kind"], sec["scope_type"], sec["scope_id"]) == (kind, scope_type, scope_id)
        assert (sec["status"], sec["secret_version"]) == ("active", 1)


def test_the_kind_follows_the_auth_method() -> None:
    assert CREDENTIAL_KINDS == {
        "bearer": "token",
        "oauth2_client_credentials": "token",
        "key_pair": "pem",
        "mtls": "pem",
        "api_key": "api_key",
        "basic": "password",
    }


def test_inline_credential_and_secret_id_together_are_refused(
    client: TestClient, world: World, people: People, owner_db: Database, app_db: Database,
    settings: Settings,
) -> None:  # fmt: skip
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    sid = secret(app_db, settings, world.a.tenant, TOKEN)
    body = _rest("https://api.example.com", secret_id=sid, credential={"value": NEW_TOKEN})
    r = client.post(f"{A}/connections", headers=h, json=body)
    assert r.status_code == 422, r.text
    assert NEW_TOKEN not in r.text
    assert _owned(owner_db, world.a.tenant) == []


def test_inline_credential_with_auth_none_is_refused(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    body = _rest("https://api.example.com", "none", credential={"value": TOKEN})
    r = client.post(f"{A}/connections", headers=h, json=body)
    assert r.status_code == 422 and "auth_method is 'none'" in r.text, r.text
    assert _owned(owner_db, world.a.tenant) == []


def test_inline_credential_on_a_read_connection_needs_the_confirmation(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    body = _rest("https://api.example.com", mode="read", credential={"value": TOKEN})
    r = client.post(f"{A}/connections", headers=h, json=body)
    assert r.status_code == 422 and "read_only_confirmed" in r.text, r.text
    assert _owned(owner_db, world.a.tenant) == []
    ok = client.post(f"{A}/connections", headers=h, json={**body, "read_only_confirmed": True})
    assert ok.status_code == 201, ok.text


def test_a_duplicate_name_leaves_no_owned_secret(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    connection(client, h, _rest("https://api.example.com", name="taken"), A)
    r = client.post(
        f"{A}/connections",
        headers=h,
        json=_rest("https://api.example.com", name="taken", credential={"value": TOKEN}),
    )
    assert r.status_code == 409, r.text
    assert _owned(owner_db, world.a.tenant) == []


# ------------------------------------------------------------------ set, rotate
def test_set_then_rotate_and_the_new_token_passes_the_test(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    with RestTestServer(token=NEW_TOKEN) as server:
        made = connection(client, h, _rest(server.base_url), A)
        assert made["secret_set"] is False
        put = _put(client, A, h, made["id"], TOKEN, made["row_version"])
        assert put.status_code == 200, put.text
        first = put.json()
        sid = first["secret_id"]
        assert first["secret_set"] is True and first["health_status"] == "unknown"
        assert first["row_version"] > made["row_version"] and first["status"] == "draft"
        sec = _secret_row(owner_db, a.tenant, sid)
        assert (sec["name"], sec["secret_version"], sec["kind"]) == (
            f"connection-{made['id']}",
            1,
            "token",
        )
        assert result_of(client, h, A, made["id"])["outcome"] == "fail"  # the old token
        tested = client.get(f"{A}/connections/{made['id']}", headers=h).json()
        assert tested["health_status"] == "unhealthy"

        rotated = _put(client, A, h, made["id"], NEW_TOKEN, tested["row_version"])
        assert rotated.status_code == 200, rotated.text
        second = rotated.json()
        assert second["secret_id"] == sid and second["health_status"] == "unknown"
        assert second["row_version"] > tested["row_version"]
        assert second["last_tested_at"] == tested["last_tested_at"]
        assert _secret_row(owner_db, a.tenant, sid)["secret_version"] == 2
        assert len(_owned(owner_db, a.tenant)) == 1
        assert result_of(client, h, A, made["id"])["outcome"] == "pass"  # only the new token
        assert [r["authenticated"] for r in server.requests] == [False, True]


def test_rotating_an_oauth2_credential_does_not_reuse_the_cached_token(
    client: TestClient, world: World, people: People
) -> None:
    """The `OAuth2Tokens` cache key holds a fingerprint of the client secret
    (`adapters/oauth2.py`), so the rotated secret fetches a new token (B-3031)."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    with OAuth2FakeServer({"c1": TOKEN}) as fake:
        body = {
            **_rest(f"{fake.base_url}/api", "oauth2_client_credentials"),
            "config": {
                "base_url": f"{fake.base_url}/api",
                "health_path": "/health",
                "oauth2_url": f"{fake.base_url}/token",
                "client_id": "c1",
            },
            "credential": {"value": TOKEN},
        }
        made = connection(client, h, body, A)
        assert result_of(client, h, A, made["id"])["outcome"] == "pass"
        assert result_of(client, h, A, made["id"])["outcome"] == "pass"
        assert len(fake.issued) == 1  # the second test reused the cached token
        old = fake.issued[0]
        fake.clients["c1"] = NEW_TOKEN
        assert _put(client, A, h, made["id"], NEW_TOKEN).status_code == 200
        assert result_of(client, h, A, made["id"])["outcome"] == "pass"
        assert len(fake.issued) == 2 and fake.api_tokens[-1] == fake.issued[1] != old


# ------------------------------------------------------------------ permissions (B-2748)
def test_secret_manage_at_the_project_is_needed_for_the_credential(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    scope = _project_scope(world)
    owner = as_user(client, people.owner_a)
    plain = connection(client, owner, _rest("https://api.example.com", scope=scope), A)
    bound = connection(
        client, owner, _rest("https://api.example.com", scope=scope, credential={"value": TOKEN}), A
    )
    before = _secret_row(owner_db, a.tenant, bound["secret_id"])

    without = _member(owner_db, world, MANAGE_PERMS, "no-secret-manage")
    h = as_user(client, without)
    body = _rest(
        "https://api.example.com", scope=scope, name="refused", credential={"value": TOKEN}
    )
    created = client.post(f"{A}/connections", headers=h, json=body)
    assert created.status_code == 403 and "secret:manage" in created.text, created.text
    for cid in (plain["id"], bound["id"]):
        r = _put(client, A, h, cid, NEW_TOKEN)
        assert r.status_code == 403 and "secret:manage" in r.text, r.text
    names = [c["name"] for c in client.get(f"{A}/connections", headers=h).json()["items"]]
    assert "refused" not in names
    assert client.get(f"{A}/connections/{plain['id']}", headers=h).json()["secret_set"] is False
    assert _secret_row(owner_db, a.tenant, bound["secret_id"]) == before
    assert len(_owned(owner_db, a.tenant)) == 1

    allowed = _member(owner_db, world, [*MANAGE_PERMS, "secret:manage"], "secret-manage")
    h = as_user(client, allowed)
    assert client.post(f"{A}/connections", headers=h, json=body).status_code == 201
    assert _put(client, A, h, plain["id"], TOKEN).status_code == 200  # set
    assert _put(client, A, h, bound["id"], NEW_TOKEN).status_code == 200  # rotate
    assert _secret_row(owner_db, a.tenant, bound["secret_id"])["secret_version"] == 2


# ------------------------------------------------------------------ check order (Requirement 4)
@pytest.fixture
def order(client: TestClient, world: World, people: People, owner_db: Database) -> dict[str, Any]:
    """Connections and members for the check-order cases: each case fails one step only."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    scope = _project_scope(world)
    url = "https://api.example.com"
    out: dict[str, Any] = {
        "A": A,
        "owner": h,
        "ok": connection(client, h, _rest(url, scope=scope), A)["id"],
        "none": connection(client, h, _rest(url, "none", scope=scope), A)["id"],
        "read": connection(client, h, _rest(url, scope=scope, mode="read"), A)["id"],
        "retired": connection(client, h, _rest(url, scope=scope), A)["id"],
    }
    assert _retire(client, A, h, out["retired"]).status_code == 200
    out["no_manage"] = _member(
        owner_db, world, ["workspace:read", "project:read", "connection:read", "secret:manage"],
        "no-conn-manage",
    )  # fmt: skip
    out["no_secret"] = _member(owner_db, world, MANAGE_PERMS, "no-secret")
    return out


def test_check_1_an_unknown_connection_is_not_found(client: TestClient, order: Any) -> None:
    r = _put(client, order["A"], order["owner"], str(uuid7()), TOKEN, 1)
    assert r.status_code == 404 and r.json()["detail"] == "connection not found", r.text


def test_check_1_an_unreadable_connection_is_not_found(
    client: TestClient, world: World, owner_db: Database, order: Any
) -> None:
    """A member of A with grants at another workspace only cannot read the connection."""
    email, uid = limited_user(owner_db, world.a.tenant, [], label="other-ws")
    grant(
        owner_db,
        world.a.tenant,
        uid,
        ["workspace:read", "connection:read", "connection:manage", "secret:manage"],
        scope_type="workspace",
        scope_id=world.a.ws2,
    )
    h = as_user(client, email)
    r = _put(client, order["A"], h, order["ok"], TOKEN, 1)
    missing = _put(client, order["A"], h, str(uuid7()), TOKEN, 1)
    assert r.status_code == missing.status_code == 404
    assert r.json()["detail"] == missing.json()["detail"] == "connection not found"


def test_check_2_connection_manage(client: TestClient, order: Any) -> None:
    h = as_user(client, order["no_manage"])
    row = client.get(f"{order['A']}/connections/{order['ok']}", headers=h).json()
    r = _put(client, order["A"], h, order["ok"], TOKEN, row["row_version"])
    assert r.status_code == 403 and "connection:manage" in r.text, r.text


def test_check_3_secret_manage(client: TestClient, order: Any) -> None:
    h = as_user(client, order["no_secret"])
    r = _put(client, order["A"], h, order["ok"], TOKEN)
    assert r.status_code == 403 and "secret:manage" in r.text, r.text


def test_check_4_a_retired_connection(client: TestClient, order: Any) -> None:
    r = _put(client, order["A"], order["owner"], order["retired"], TOKEN)
    assert r.status_code == 409 and "retired" in r.text, r.text


def test_check_5_auth_method_none(client: TestClient, order: Any) -> None:
    r = _put(client, order["A"], order["owner"], order["none"], TOKEN)
    assert r.status_code == 422 and "auth_method is 'none'" in r.text, r.text


def test_check_6_an_invalid_value(client: TestClient, order: Any) -> None:
    r = _put(client, order["A"], order["owner"], order["ok"], "")
    assert r.status_code == 422 and r.json()["detail"] == BYTES_MSG, r.text


def test_check_7_a_stale_row_version(client: TestClient, order: Any) -> None:
    row = client.get(f"{order['A']}/connections/{order['ok']}", headers=order["owner"]).json()
    r = _put(client, order["A"], order["owner"], order["ok"], TOKEN, row["row_version"] - 1)
    assert r.status_code == 409 and "row_version mismatch" in r.text, r.text


def test_check_8_a_read_connection_needs_the_confirmation(
    client: TestClient, owner_db: Database, world: World, order: Any
) -> None:
    r = _put(client, order["A"], order["owner"], order["read"], TOKEN)
    assert r.status_code == 422 and "read_only_confirmed" in r.text, r.text
    assert _owned(owner_db, world.a.tenant) == []
    ok = _put(client, order["A"], order["owner"], order["read"], TOKEN, read_only_confirmed=True)
    assert ok.status_code == 200, ok.text


def test_check_4_answers_before_check_7(client: TestClient, order: Any) -> None:
    r = _put(client, order["A"], order["owner"], order["retired"], TOKEN, 1)
    assert r.status_code == 409 and "retired" in r.text and "row_version" not in r.text, r.text


# ------------------------------------------------------------------ the value is never echoed
def test_a_refused_value_is_never_echoed(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    made = connection(client, h, _rest("https://api.example.com"), A)
    big = "v" * 17000
    for value in (big, ""):
        r = _put(client, A, h, made["id"], value)
        assert r.status_code == 422 and r.json()["detail"] == BYTES_MSG, r.text
        assert value == "" or value not in r.text
        r = client.post(
            f"{A}/connections",
            headers=h,
            json=_rest("https://x.example", credential={"value": value}),
        )
        assert r.status_code == 422 and r.json()["detail"] == BYTES_MSG, r.text
        assert value == "" or value not in r.text
    assert _owned(owner_db, world.a.tenant) == []


# ------------------------------------------------------------------ admin prefix (B-2750)
def test_the_admin_routes_refuse_owned_secrets(
    client: TestClient, admin: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    made = connection(
        client,
        as_user(client, people.owner_a),
        _rest("https://api.example.com", credential={"value": TOKEN}),
        A,
    )
    sid = made["secret_id"]
    before = _secret_row(owner_db, a.tenant, sid)
    h = as_user(admin, people.owner_a)
    created = admin.post(
        f"{A}/secrets", headers=h, json={"name": "connection-x", "value": NEW_TOKEN}
    )
    assert created.status_code == 422 and "reserved" in created.text, created.text
    assert NEW_TOKEN not in created.text
    rotate = admin.post(f"{A}/secrets/{sid}/rotate", headers=h, json={"value": NEW_TOKEN})
    assert rotate.status_code == 409 and "belongs to a connection" in rotate.text, rotate.text
    patch = admin.patch(
        f"{A}/secrets/{sid}",
        headers=h,
        json={"status": "disabled", "row_version": before["row_version"]},
    )
    assert patch.status_code == 409 and "belongs to a connection" in patch.text, patch.text
    after = _secret_row(owner_db, a.tenant, sid)
    for col in ("secret_version", "status", "row_version", "ciphertext"):
        assert after[col] == before[col], col
    nobody = uuid7()
    assert admin.post(
        f"{A}/secrets/{nobody}/rotate", headers=h, json={"value": NEW_TOKEN}
    ).status_code == 404  # fmt: skip
    assert admin.patch(
        f"{A}/secrets/{nobody}", headers=h, json={"status": "disabled", "row_version": 1}
    ).status_code == 404  # fmt: skip
    names = [s["name"] for s in admin.get(f"{A}/secrets", headers=h).json()["items"]]
    assert "connection-x" not in names

    viewer = as_user(admin, people.viewer_a)  # no secret:manage: the 403 still comes first
    rotate = admin.post(f"{A}/secrets/{sid}/rotate", headers=viewer, json={"value": NEW_TOKEN})
    assert rotate.status_code == 403, rotate.text
    patch = admin.patch(
        f"{A}/secrets/{sid}",
        headers=viewer,
        json={"status": "disabled", "row_version": before["row_version"]},
    )
    assert patch.status_code == 403, patch.text
    assert _secret_row(owner_db, a.tenant, sid) == after


# ------------------------------------------------------------------ archive (B-2751)
def test_archive_with_a_stale_row_version_changes_nothing(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    made = connection(client, h, _rest("https://api.example.com", credential={"value": TOKEN}), A)
    conn_before = _connection_row(owner_db, a.tenant, made["id"])
    sec_before = _secret_row(owner_db, a.tenant, made["secret_id"])
    r = client.post(
        f"{A}/connections/{made['id']}/status",
        headers=h,
        json={"status": "retired", "row_version": made["row_version"] - 1},
    )
    assert r.status_code == 409, r.text
    assert _connection_row(owner_db, a.tenant, made["id"]) == conn_before
    assert _secret_row(owner_db, a.tenant, made["secret_id"]) == sec_before


def test_archive_disables_the_owned_secret(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    made = connection(client, h, _rest("https://api.example.com", credential={"value": TOKEN}), A)
    assert _retire(client, A, h, made["id"]).status_code == 200
    assert _secret_row(owner_db, a.tenant, made["secret_id"])["status"] == "disabled"
    disabled = _events(owner_db, a.tenant, "secret.disabled")
    assert [e["target_id"] for e in disabled] == [made["secret_id"]]


def test_archive_keeps_a_secret_another_live_connection_binds(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    made = connection(client, h, _rest("https://api.example.com", credential={"value": TOKEN}), A)
    other = connection(client, h, _rest("https://api.example.com"), A)
    bound = client.patch(
        f"{A}/connections/{other['id']}",
        headers=h,
        json={"secret_id": made["secret_id"], "row_version": other["row_version"]},
    )
    assert bound.status_code == 200, bound.text
    assert _retire(client, A, h, made["id"]).status_code == 200
    assert _secret_row(owner_db, a.tenant, made["secret_id"])["status"] == "active"
    assert _events(owner_db, a.tenant, "secret.disabled") == []


def test_archive_keeps_a_secret_the_connection_does_not_own(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    sid = secret(app_db, settings, a.tenant, TOKEN)
    made = connection(client, h, _rest("https://api.example.com", secret_id=sid), A)
    assert _retire(client, A, h, made["id"]).status_code == 200
    assert _secret_row(owner_db, a.tenant, sid)["status"] == "active"
    assert _events(owner_db, a.tenant, "secret.disabled") == []


# ------------------------------------------------------------------ audit (B-2752)
def test_the_credential_audit_events(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a, A = world.a, org(world.a.slug)
    h = as_user(client, people.owner_a)
    scope = _project_scope(world)
    made = connection(client, h, _rest("https://api.example.com", scope=scope), A)
    assert _put(client, A, h, made["id"], TOKEN).status_code == 200
    sid = client.get(f"{A}/connections/{made['id']}", headers=h).json()["secret_id"]
    name = f"connection-{made['id']}"
    other = connection(client, h, _rest("https://api.example.com", scope=scope), A)
    bound = client.patch(
        f"{A}/connections/{other['id']}",
        headers=h,
        json={"secret_id": sid, "row_version": other["row_version"]},
    )
    assert bound.status_code == 200, bound.text
    assert _put(client, A, h, made["id"], NEW_TOKEN).status_code == 200
    assert _retire(client, A, h, other["id"]).status_code == 200
    assert _retire(client, A, h, made["id"]).status_code == 200

    placed = (a.ws, a.project)
    (cset,) = _events(owner_db, a.tenant, "connection.credential_set")
    assert (cset["target_type"], cset["target_id"]) == ("connection", made["id"])
    assert {k: cset["details"][k] for k in ("secret_id", "version")} == {
        "secret_id": sid,
        "version": 1,
    }
    (crot,) = _events(owner_db, a.tenant, "connection.credential_rotated")
    assert {k: crot["details"][k] for k in ("secret_id", "version", "shared_with")} == {
        "secret_id": sid,
        "version": 2,
        "shared_with": 1,
    }
    for action, details in (
        ("secret.created", {"name": name, "kind": "token", "scope_type": "project"}),
        ("secret.rotated", {"version": 2}),
        ("secret.disabled", {}),
    ):
        (event,) = _events(owner_db, a.tenant, action)
        assert (event["target_type"], event["target_id"]) == ("secret", sid), action
        assert (event["workspace_id"], event["project_id"]) == placed, action
        assert event["details"]["target"] == f"secret:{name}", action
        got = {k: v for k, v in event["details"].items() if k not in ("actor", "target")}
        assert got == details, action
    every = json.dumps([e["details"] for e in _events(owner_db, a.tenant)], default=str)
    assert TOKEN not in every and NEW_TOKEN not in every


def test_a_failing_test_masks_the_value(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter that put the credential in its failure message: the redaction registry (the
    value was registered when it was stored) masks it in the result and the problem details."""

    def leaky(self: RestHttpAdapter, ctx: ExecContext, conn: ConnectionConfig) -> Any:
        assert conn.credential is not None
        raise AdapterFailure(ErrorClass.AUTH, f"upstream refused {conn.credential.reveal()}")

    monkeypatch.setattr(RestHttpAdapter, "test", leaky)
    monkeypatch.setattr(RestHttpAdapter, "discover", leaky)
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    value = f"cm2-leak-{uuid7().hex}"
    made = connection(client, h, _rest("https://api.example.com", credential={"value": value}), A)
    out = run_test(client, h, A, made["id"])
    assert out["status"] == "done" and out["result"]["outcome"] == "fail", out
    assert value not in json.dumps(out) and REDACTED in json.dumps(out)
    found = client.post(f"{A}/connections/{made['id']}/discover", headers=h)
    assert found.status_code >= 400 and value not in found.text and REDACTED in found.text
    every = json.dumps([e["details"] for e in _events(owner_db, world.a.tenant)], default=str)
    assert value not in every
