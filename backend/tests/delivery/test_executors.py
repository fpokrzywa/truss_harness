"""Executor identities and credentials through the routes, as `shifde_app` (design 5, section 9
items 4, 12 and 15; B-2664): create, list, get, one active executor per organization, disable
and re-enable, issue with a one-time reveal, at most two live credentials, rotate, revoke,
permissions, audit, and no plaintext secret in the database.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any  # Any: JSON bodies, audit details
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.identity.apikeys import hash_key
from tests.connections.routes import as_user, limited_user, system_role_user
from tests.delivery.routes import create_executor, executors, issue
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People

TOKEN = re.compile(r"^shx_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}$")
EXECUTOR_FIELDS = {"id", "name", "status", "last_seen_at", "version", "created_at", "updated_at"}
CREDENTIAL_FIELDS = {
    "id",
    "executor_id",
    "prefix",
    "status",
    "created_by",
    "created_at",
    "expires_at",
    "revoked_at",
}


def _audit(owner_db: Database, tenant: UUID) -> list[tuple[str, str, dict[str, Any]]]:
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT action, target_type, details FROM shifde.audit_events WHERE tenant_id = :t "
                "AND starts_with(action, 'delivery.executor') ORDER BY chain_seq"
            ),
            {"t": tenant},
        ).all()
    return [(r.action, r.target_type, r.details) for r in rows]


def _credentials(client: TestClient, h: dict[str, str], slug: str, executor_id: str) -> list[Any]:
    r = client.get(f"{executors(slug)}/{executor_id}/credentials", headers=h)
    assert r.status_code == 200, r.text
    return list(r.json())


def test_create_list_get(client: TestClient, world: World, people: People) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    assert set(made) == EXECUTOR_FIELDS
    assert made["status"] == "active" and made["last_seen_at"] is None and made["version"] is None
    listed = client.get(executors(a.slug), headers=h)
    assert listed.status_code == 200, listed.text
    assert [e["id"] for e in listed.json()["items"]] == [made["id"]]
    got = client.get(f"{executors(a.slug)}/{made['id']}", headers=h)
    assert got.status_code == 200 and got.json() == made
    assert client.get(f"{executors(a.slug)}?status=disabled", headers=h).json()["items"] == []
    for name in ("", "x" * 201):
        r = client.post(executors(a.slug), json={"name": name}, headers=h)
        assert r.status_code == 422, r.text
    r = client.post(executors(a.slug), json={"name": "n", "status": "disabled"}, headers=h)
    assert r.status_code == 422, r.text


def test_a_second_active_executor_is_a_409(
    client: TestClient, world: World, people: People
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    first = create_executor(client, h, a.slug)
    second = client.post(executors(a.slug), json={"name": "second"}, headers=h)
    assert second.status_code == 409 and second.json()["code"] == "conflict", second.text
    assert "uq_delivery" not in second.text and "unique" not in second.text.lower()
    url = f"{executors(a.slug)}/{first['id']}"
    assert client.patch(url, json={"status": "disabled"}, headers=h).json()["status"] == "disabled"
    replacement = create_executor(client, h, a.slug)
    again = client.patch(url, json={"status": "active"}, headers=h)
    assert again.status_code == 409 and again.json()["code"] == "conflict", again.text
    assert "uq_delivery" not in again.text
    assert client.get(url, headers=h).json()["status"] == "disabled"
    disabled = client.patch(
        f"{executors(a.slug)}/{replacement['id']}", json={"status": "disabled"}, headers=h
    )
    assert disabled.status_code == 200, disabled.text
    enabled = client.patch(url, json={"status": "active"}, headers=h)
    assert enabled.status_code == 200 and enabled.json()["status"] == "active", enabled.text
    bad = client.patch(url, json={"status": "deleted"}, headers=h)
    assert bad.status_code == 422, bad.text


def test_setting_the_same_status_is_a_no_op(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, world.a.slug)
    r = client.patch(
        f"{executors(world.a.slug)}/{made['id']}", json={"status": "active"}, headers=h
    )
    assert r.status_code == 200 and r.json() == made, r.text
    assert [a for a, _, _ in _audit(owner_db, world.a.tenant)] == ["delivery.executor.created"]


def test_disable_revokes_every_credential_and_reenable_restores_none(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    first = issue(client, h, a.slug, made["id"])["credential"]
    second = issue(client, h, a.slug, made["id"])["credential"]
    url = f"{executors(a.slug)}/{made['id']}"
    r = client.patch(url, json={"status": "disabled"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "disabled", r.text
    creds = _credentials(client, h, a.slug, made["id"])
    assert {c["status"] for c in creds} == {"revoked"}
    assert all(c["revoked_at"] for c in creds)
    issued = client.post(f"{url}/credentials", headers=h)
    assert issued.status_code == 409, issued.text
    assert client.patch(url, json={"status": "active"}, headers=h).status_code == 200
    assert {c["status"] for c in _credentials(client, h, a.slug, made["id"])} == {"revoked"}
    disabled = [
        d for act, _, d in _audit(owner_db, a.tenant) if act == "delivery.executor.disabled"
    ]
    assert len(disabled) == 1
    assert sorted(disabled[0]["revoked_credential_ids"]) == sorted([first["id"], second["id"]])


def test_issue_reveals_the_secret_once(
    client: TestClient, world: World, people: People, settings: Settings
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    before = utcnow()
    out = issue(client, h, a.slug, made["id"])
    assert set(out) == {"credential", "secret"}
    assert TOKEN.match(out["secret"]), out["secret"]
    cred = out["credential"]
    assert set(cred) == CREDENTIAL_FIELDS
    assert cred["status"] == "live" and cred["revoked_at"] is None
    assert cred["executor_id"] == made["id"]
    assert out["secret"].split("_")[1] == cred["prefix"]
    expires = datetime.fromisoformat(cred["expires_at"])
    assert before + timedelta(days=90) - timedelta(minutes=1) <= expires
    assert expires <= utcnow() + timedelta(days=90)
    listed = client.get(f"{executors(a.slug)}/{made['id']}/credentials", headers=h)
    assert listed.status_code == 200, listed.text
    assert [set(c) for c in listed.json()] == [CREDENTIAL_FIELDS]
    assert out["secret"] not in listed.text and "secret" not in listed.text
    assert "hash" not in listed.text


def test_a_third_live_credential_is_a_409(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    first = issue(client, h, a.slug, made["id"])["credential"]
    issue(client, h, a.slug, made["id"])
    third = client.post(f"{executors(a.slug)}/{made['id']}/credentials", headers=h)
    assert third.status_code == 409 and third.json()["code"] == "conflict", third.text
    # an expired credential is not live
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.delivery_executor_credentials SET expires_at = now() - "
                "interval '1 second' WHERE id = :i"
            ),
            {"i": first["id"]},
        )
        o.commit()
    statuses = {c["id"]: c["status"] for c in _credentials(client, h, a.slug, made["id"])}
    assert statuses[first["id"]] == "expired"
    issue(client, h, a.slug, made["id"])


def test_rotate_keeps_the_old_credential_live(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    old = issue(client, h, a.slug, made["id"])
    base = f"{executors(a.slug)}/{made['id']}/credentials"
    r = client.post(f"{base}/{old['credential']['id']}/rotate", headers=h)
    assert r.status_code == 201, r.text
    new = r.json()
    assert TOKEN.match(new["secret"]) and new["secret"] != old["secret"]
    assert new["credential"]["id"] != old["credential"]["id"]
    statuses = {c["id"]: c["status"] for c in _credentials(client, h, a.slug, made["id"])}
    assert statuses == {old["credential"]["id"]: "live", new["credential"]["id"]: "live"}
    # two live: rotating again would make a third
    again = client.post(f"{base}/{new['credential']['id']}/rotate", headers=h)
    assert again.status_code == 409, again.text
    revoked = client.post(f"{base}/{old['credential']['id']}/revoke", headers=h)
    assert revoked.status_code == 200, revoked.text
    stale = client.post(f"{base}/{old['credential']['id']}/rotate", headers=h)
    assert stale.status_code == 409, stale.text
    rotated = [d for act, _, d in _audit(owner_db, a.tenant) if act.endswith("rotated")]
    expected = {
        "executor_id": made["id"],
        "name": made["name"],
        "old_credential_id": old["credential"]["id"],
        "old_prefix": old["credential"]["prefix"],
        "new_credential_id": new["credential"]["id"],
        "new_prefix": new["credential"]["prefix"],
    }
    assert len(rotated) == 1 and rotated[0].items() >= expected.items()


def test_revoke(client: TestClient, world: World, people: People) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    cred = issue(client, h, a.slug, made["id"])["credential"]
    url = f"{executors(a.slug)}/{made['id']}/credentials/{cred['id']}/revoke"
    r = client.post(url, headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == CREDENTIAL_FIELDS
    assert r.json()["status"] == "revoked" and r.json()["revoked_at"]
    again = client.post(url, headers=h)
    assert again.status_code == 409 and again.json()["code"] == "conflict", again.text


def test_viewer_and_reader_cannot_manage(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    cred = issue(client, h, a.slug, made["id"])["credential"]
    url = f"{executors(a.slug)}/{made['id']}"
    mutations = (
        ("PATCH", url, {"status": "disabled"}),
        ("POST", f"{url}/credentials", None),
        ("POST", f"{url}/credentials/{cred['id']}/rotate", None),
        ("POST", f"{url}/credentials/{cred['id']}/revoke", None),
    )
    # the viewer holds no executor permission: 403 at the organization, and an executor it cannot
    # read is hidden like an unknown id
    h = as_user(client, people.viewer_a)
    r = client.post(executors(a.slug), json={"name": "x"}, headers=h)
    assert r.status_code == 403, r.text
    assert client.get(executors(a.slug), headers=h).status_code == 403
    for method, path, body in mutations:
        r = client.request(method, path, json=body, headers=h)
        assert r.status_code == 404, (method, path, r.text)
    # a reader (developer: delivery:executor:read only) sees the executor and gets 403 on writes
    h = as_user(client, system_role_user(owner_db, a.tenant, "developer"))
    assert client.get(url, headers=h).status_code == 200
    assert client.get(f"{url}/credentials", headers=h).status_code == 200
    assert client.post(executors(a.slug), json={"name": "x"}, headers=h).status_code == 403
    for method, path, body in mutations:
        r = client.request(method, path, json=body, headers=h)
        assert r.status_code == 403, (method, path, r.text)
    assert [c["status"] for c in _credentials(client, h, a.slug, made["id"])] == ["live"]
    # a member with only executor:manage at the organization manages it
    email, _ = limited_user(
        owner_db, a.tenant, ["delivery:executor:read", "delivery:executor:manage"]
    )
    h = as_user(client, email)
    assert client.post(f"{url}/credentials", headers=h).status_code == 201


def test_the_audit_actions(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    out = issue(client, h, a.slug, made["id"])
    base = f"{executors(a.slug)}/{made['id']}"
    rotated = client.post(f"{base}/credentials/{out['credential']['id']}/rotate", headers=h).json()
    client.post(f"{base}/credentials/{out['credential']['id']}/revoke", headers=h)
    client.patch(base, json={"status": "disabled"}, headers=h)
    client.patch(base, json={"status": "active"}, headers=h)
    rows = _audit(owner_db, a.tenant)
    assert [(act, kind) for act, kind, _ in rows] == [
        ("delivery.executor.created", "delivery_executor"),
        ("delivery.executor.credential.issued", "delivery_executor_credential"),
        ("delivery.executor.credential.rotated", "delivery_executor_credential"),
        ("delivery.executor.credential.revoked", "delivery_executor_credential"),
        ("delivery.executor.disabled", "delivery_executor"),
        ("delivery.executor.enabled", "delivery_executor"),
    ]
    assert rows[1][2]["prefix"] == out["credential"]["prefix"]
    assert rows[4][2]["revoked_credential_ids"] == [rotated["credential"]["id"]]
    with owner_db.session() as o:
        dumped = o.execute(
            text("SELECT string_agg(e::text, ' ') FROM shifde.audit_events e WHERE tenant_id = :t"),
            {"t": a.tenant},
        ).scalar_one()
        hashes = o.execute(
            text(
                "SELECT secret_hash FROM shifde.delivery_executor_credentials "
                "WHERE executor_id = :e"
            ),
            {"e": made["id"]},
        ).scalars()
        for value in hashes:
            assert value.hex() not in dumped
    for secret in (out["secret"], rotated["secret"]):
        assert secret not in dumped and secret.split("_")[2] not in dumped


def test_the_database_holds_no_plaintext_secret(
    client: TestClient, world: World, people: People, owner_db: Database, settings: Settings
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, a.slug)
    out = issue(client, h, a.slug, made["id"])
    token = out["secret"]
    with owner_db.session() as o:
        stored = o.execute(
            text("SELECT secret_hash FROM shifde.delivery_executor_credentials WHERE id = :i"),
            {"i": out["credential"]["id"]},
        ).scalar_one()
        row_text = o.execute(
            text("SELECT c::text FROM shifde.delivery_executor_credentials c WHERE id = :i"),
            {"i": out["credential"]["id"]},
        ).scalar_one()
        executor_text = o.execute(
            text("SELECT e::text FROM shifde.delivery_executors e WHERE id = :i"),
            {"i": made["id"]},
        ).scalar_one()
    assert stored != token.encode() and len(stored) == 32
    assert stored == hash_key(settings.pepper, token)
    secret = token.split("_")[2]
    for blob in (row_text, executor_text):
        assert token not in blob and secret not in blob


def test_the_app_role_cannot_read_the_hash(
    client: TestClient, world: World, people: People, app_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, world.a.slug)
    issue(client, h, world.a.slug, made["id"])
    with app_session(app_db, world.a.tenant) as s:
        assert s.execute(
            text("SELECT prefix FROM shifde.delivery_executor_credentials")
        ).scalar_one()
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(text("SELECT secret_hash FROM shifde.delivery_executor_credentials"))
