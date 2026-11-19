"""Executor authentication (design 5, section 9 items 12 and 13; B-2664): `GET
/api/executor/v1/whoami` with an `shx_` token, every refusal one identical 401, user and executor
tokens kept apart, the resolver definer called directly as `shifde_app`, and pepper rotation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import apikeys
from tests.connections.routes import as_user
from tests.delivery.routes import (
    WHOAMI,
    bearer,
    create_executor,
    delivery_client,
    executors,
    issue,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People

RESOLVE = text("SELECT * FROM shifde.delivery_resolve_executor(:p, :h)")
_VARYING = {"instance", "request_id"}
RETIRED_PEPPER = "retired-pepper-" + "r" * 32


def _body(r: httpx.Response) -> dict[str, Any]:
    return {k: v for k, v in r.json().items() if k not in _VARYING}


def _setup(client: TestClient, world: World, people: People) -> tuple[dict[str, Any], str]:
    """A's executor and one live token."""
    h = as_user(client, people.owner_a)
    made = create_executor(client, h, world.a.slug)
    token = issue(client, h, world.a.slug, made["id"])["secret"]
    client.cookies.clear()
    return made, token


def _owner_sql(owner_db: Database, sql: str, **params: Any) -> None:
    with owner_db.session() as o:
        o.execute(text(sql), params)
        o.commit()


def test_whoami_with_a_live_token(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    made, token = _setup(client, world, people)
    before = utcnow()
    r = client.get(WHOAMI, headers={**bearer(token), "X-Executor-Version": "1.2.3+fake"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"executor_id", "name", "status", "version", "last_seen_at"}
    assert body["executor_id"] == made["id"] and body["name"] == made["name"]
    assert body["status"] == "active" and body["version"] == "1.2.3+fake"
    with owner_db.session() as o:
        row = o.execute(
            text("SELECT last_seen_at, version FROM shifde.delivery_executors WHERE id = :i"),
            {"i": made["id"]},
        ).one()
    assert row.last_seen_at >= before - timedelta(seconds=5) and row.version == "1.2.3+fake"
    # without the header the version stays; last_seen_at moves on every call
    again = client.get(WHOAMI, headers=bearer(token))
    assert again.status_code == 200 and again.json()["version"] == "1.2.3+fake"
    assert again.json()["last_seen_at"] >= body["last_seen_at"]


def test_a_bad_executor_version_is_a_422(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    made, token = _setup(client, world, people)
    for version in ("1.0 beta", "x" * 65, "v1;drop", "v1/x"):
        r = client.get(WHOAMI, headers={**bearer(token), "X-Executor-Version": version})
        assert r.status_code == 422 and r.json()["code"] == "validation_error", (version, r.text)
    with owner_db.session() as o:
        version = o.execute(
            text("SELECT version FROM shifde.delivery_executors WHERE id = :i"), {"i": made["id"]}
        ).scalar_one()
    assert version is None


def test_every_refusal_is_the_same_401(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    made, token = _setup(client, world, people)
    prefix = token.split("_")[1]
    other = "".join("A" if ch != "A" else "B" for ch in token.split("_")[2])
    assert client.get(WHOAMI, headers=bearer(token)).status_code == 200
    reference = client.get(WHOAMI)  # no header
    assert reference.status_code == 401 and reference.json()["code"] == "invalid_credentials"
    for name, headers in (
        ("malformed", bearer("shx_short")),
        ("not bearer", {"Authorization": f"Token {token}"}),
        ("unknown prefix", bearer(f"shx_ZZZZZZZZ_{token.split('_')[2]}")),
        ("wrong secret", bearer(f"shx_{prefix}_{other}")),
        ("empty bearer", {"Authorization": "Bearer "}),
    ):
        r = client.get(WHOAMI, headers=headers)
        assert r.status_code == 401 and _body(r) == _body(reference), (name, r.text)

    cred = "UPDATE shifde.delivery_executor_credentials SET {} WHERE executor_id = :e"
    states: list[tuple[str, Callable[[], None], Callable[[], None]]] = [
        (
            "revoked",
            lambda: _owner_sql(owner_db, cred.format("revoked_at = now()"), e=made["id"]),
            lambda: _owner_sql(owner_db, cred.format("revoked_at = NULL"), e=made["id"]),
        ),
        (
            "expired",
            lambda: _owner_sql(
                owner_db, cred.format("expires_at = now() - interval '1 s'"), e=made["id"]
            ),
            lambda: _owner_sql(
                owner_db, cred.format("expires_at = now() + interval '1 day'"), e=made["id"]
            ),
        ),
        (
            "disabled executor",
            lambda: _owner_sql(
                owner_db,
                "UPDATE shifde.delivery_executors SET status = 'disabled' WHERE id = :e",
                e=made["id"],
            ),
            lambda: _owner_sql(
                owner_db,
                "UPDATE shifde.delivery_executors SET status = 'active' WHERE id = :e",
                e=made["id"],
            ),
        ),
        (
            "suspended organization",
            lambda: _owner_sql(
                owner_db,
                "UPDATE shifde.tenants SET status = 'suspended' WHERE id = :t",
                t=world.a.tenant,
            ),
            lambda: _owner_sql(
                owner_db,
                "UPDATE shifde.tenants SET status = 'active' WHERE id = :t",
                t=world.a.tenant,
            ),
        ),
    ]
    for name, refuse, restore in states:
        refuse()
        r = client.get(WHOAMI, headers=bearer(token))
        assert r.status_code == 401 and _body(r) == _body(reference), (name, r.text)
        restore()
        assert client.get(WHOAMI, headers=bearer(token)).status_code == 200, name


def _owner_id(owner_db: Database, email: str) -> UUID:
    with owner_db.session() as o:
        uid: UUID = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    return uid


def test_user_credentials_are_refused_on_the_gateway(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
) -> None:
    _setup(client, world, people)
    reference = client.get(WHOAMI)
    owner = _owner_id(owner_db, people.owner_a)
    with app_session(app_db, world.a.tenant, owner) as s:
        _, key = apikeys.mint(
            s,
            settings,
            tenant_id=world.a.tenant,
            name="owner key",
            principal_type="user",
            principal_id=owner,
            scopes=["delivery:executor:read"],
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=owner,
        )
        s.commit()
    # the key is a working user credential on the user routes
    assert client.get(executors(world.a.slug), headers=bearer(key)).status_code == 200
    r = client.get(WHOAMI, headers=bearer(key))
    assert r.status_code == 401 and _body(r) == _body(reference), r.text
    # a signed-in session cookie is never read by the gateway
    h = as_user(client, people.owner_a)
    assert client.get(executors(world.a.slug), headers=h).status_code == 200
    r = client.get(WHOAMI, headers=h)
    assert r.status_code == 401 and _body(r) == _body(reference), r.text


def test_an_executor_token_is_refused_on_user_routes(
    client: TestClient, world: World, people: People
) -> None:
    _, token = _setup(client, world, people)
    assert client.get(WHOAMI, headers=bearer(token)).status_code == 200
    for url in (executors(world.a.slug), targets(world.a.slug)):
        r = client.get(url, headers=bearer(token))
        assert r.status_code == 401 and r.json()["code"] == "invalid_credentials", (url, r.text)


def test_the_resolver_answers_tenant_and_executor_only(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
) -> None:
    made, token = _setup(client, world, people)
    prefix = token.split("_")[1]
    good = apikeys.hash_key(settings.pepper, token)

    def resolve(p: str = prefix, h: bytes = good) -> list[tuple[Any, ...]]:
        with app_session(app_db, None) as s:
            result = s.execute(RESOLVE, {"p": p, "h": h})
            assert list(result.keys()) == ["tenant_id", "executor_id"]
            return [tuple(r) for r in result.all()]

    assert resolve() == [(world.a.tenant, UUID(made["id"]))]
    assert resolve(h=apikeys.hash_key(settings.pepper, token + "x")) == []
    assert resolve(p="ZZZZZZZZ") == []
    cred = "UPDATE shifde.delivery_executor_credentials SET {} WHERE executor_id = :e"
    for refuse, restore in (
        (cred.format("revoked_at = now()"), cred.format("revoked_at = NULL")),
        (
            cred.format("expires_at = now() - interval '1 s'"),
            cred.format("expires_at = now() + interval '1 day'"),
        ),
        (
            "UPDATE shifde.delivery_executors SET status = 'disabled' WHERE id = :e",
            "UPDATE shifde.delivery_executors SET status = 'active' WHERE id = :e",
        ),
    ):
        _owner_sql(owner_db, refuse, e=made["id"])
        assert resolve() == [], refuse
        _owner_sql(owner_db, restore, e=made["id"])
    _owner_sql(
        owner_db, "UPDATE shifde.tenants SET status = 'suspended' WHERE id = :t", t=world.a.tenant
    )
    assert resolve() == []
    _owner_sql(
        owner_db, "UPDATE shifde.tenants SET status = 'active' WHERE id = :t", t=world.a.tenant
    )
    assert resolve() == [(world.a.tenant, UUID(made["id"]))]


def test_a_token_of_a_retired_pepper_still_authenticates(
    settings: Settings, world: World, people: People
) -> None:
    old = settings.model_copy(update={"api_key_pepper": SecretStr(RETIRED_PEPPER)})
    with delivery_client(old) as c:
        made, token = _setup(c, world, people)
        assert c.get(WHOAMI, headers=bearer(token)).status_code == 200
    current = "current-pepper-" + uuid7().hex + "c" * 16
    rotated = settings.model_copy(
        update={
            "api_key_pepper": SecretStr(current),
            "api_key_pepper_id": "p2",
            "api_key_retired_peppers": SecretStr(f"p1:{RETIRED_PEPPER}"),
        }
    )
    with delivery_client(rotated) as c:
        r = c.get(WHOAMI, headers=bearer(token))
        assert r.status_code == 200 and r.json()["executor_id"] == made["id"], r.text
    dropped = settings.model_copy(
        update={"api_key_pepper": SecretStr(current), "api_key_pepper_id": "p2"}
    )
    with delivery_client(dropped) as c:
        assert c.get(WHOAMI, headers=bearer(token)).status_code == 401
