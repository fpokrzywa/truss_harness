"""Snowflake `key_pair` connections, client A against client B (SP6-2, design section 8): every
connection route A can name B's Snowflake connection on answers exactly like an unknown id (404,
same body), B's slug is an unknown organization to A, and A cannot bind B's secret. B's connection
and secret rows stay byte-identical and B's fake account receives no request from A's attempts.
Each side's connection is created through the routes by its own owner, on its own fake account."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies, database rows
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.dataplatforms.fakes import FakeSnowflakeAccount
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.conntests import discovery_of, execute_invocation
from tests.connections.routes import (
    activate,
    as_user,
    connection,
    connections_client,
    org,
    secret,
)
from tests.connections.test_routes_cross_client import _same_404
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


KEY_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM_A, PEM_B = _pem(KEY_A), _pem(KEY_B)


def _body(account: str, name: str, **extra: Any) -> dict[str, Any]:  # Any: JSON values
    return {
        "name": name,
        "connection_type": "data_platform",
        "adapter_key": "snowflake",
        "config": {"account": account, "user": "reader", "warehouse": "COMPUTE_WH"},
        "auth_method": "key_pair",
        **extra,
    }


@dataclass(frozen=True)
class Side:
    base: str
    email: str
    tenant: UUID
    connection: str
    fake: FakeSnowflakeAccount


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def fakes() -> tuple[FakeSnowflakeAccount, FakeSnowflakeAccount]:
    return (
        FakeSnowflakeAccount(account="acct1", user="reader", public_key=KEY_A.public_key()),
        FakeSnowflakeAccount(account="acct2", user="reader", public_key=KEY_B.public_key()),
    )


@pytest.fixture
def sf_client(
    settings: Settings, fakes: tuple[FakeSnowflakeAccount, FakeSnowflakeAccount]
) -> Iterator[TestClient]:
    """The routes with the runtime's network hooks pointed at each side's own fake account."""
    by_host = {fake.host: fake for fake in fakes}

    def route(request: httpx.Request) -> httpx.Response:
        # the connection is pinned to the resolved address: the account is in the Host header
        return by_host[request.headers["host"].split(":")[0].lower()].handle(request)

    with connections_client(settings) as c:
        app: Any = c.app  # Any: the FastAPI app behind the test client
        app.state.integration = dataclasses.replace(
            app.state.integration,
            resolver=lambda host: ["127.0.0.1"],
            inner=httpx.MockTransport(route),
        )
        yield c


def _side(
    c: TestClient, tenant: UUID, slug: str, email: str, account: str, pem: str, fake: Any
) -> Side:
    base, h = org(slug), as_user(c, email)
    made = connection(
        c,
        h,
        _body(account, f"sf-{slug}", credential={"value": pem}, read_only_confirmed=True),
        base,
    )
    activate(c, h, made["id"], base)  # the worker's test job against the side's own fake
    discovery_of(c, h, base, str(made["id"]), tenant_id=tenant)
    return Side(base, email, tenant, str(made["id"]), fake)


@pytest.fixture
def two(
    sf_client: TestClient,
    world: World,
    people: People,
    fakes: tuple[FakeSnowflakeAccount, FakeSnowflakeAccount],
) -> Two:
    fa, fb = fakes
    b = _side(sf_client, world.b.tenant, world.b.slug, people.owner_b, "acct2", PEM_B, fb)
    a = _side(sf_client, world.a.tenant, world.a.slug, people.owner_a, "acct1", PEM_A, fa)
    return Two(a, b)


def _b_state(owner_db: Database, two: Two, ids: list[str]) -> tuple[Any, ...]:
    """B's connection rows and every secret row of B, all columns (ciphertext included), and
    B's tests, discoveries and jobs."""
    with owner_db.session() as o:
        set_tenant(o, two.b.tenant)
        conns = o.execute(
            text("SELECT * FROM shifde.connections WHERE id = ANY(CAST(:i AS uuid[])) ORDER BY id"),
            {"i": ids},
        ).all()
        secrets = o.execute(
            text("SELECT * FROM shifde.secrets WHERE tenant_id = :t ORDER BY id"),
            {"t": two.b.tenant},
        ).all()
        tests = o.execute(
            text("SELECT count(*) FROM shifde.connection_tests WHERE tenant_id = :t"),
            {"t": two.b.tenant},
        ).scalar_one()
        found = o.execute(
            text("SELECT count(*) FROM shifde.connection_discoveries WHERE tenant_id = :t"),
            {"t": two.b.tenant},
        ).scalar_one()
        jobs = o.execute(
            text("SELECT id, kind, status, payload FROM shifde.jobs WHERE tenant_id = :t"),
            {"t": two.b.tenant},
        ).all()
    return (
        [tuple(c) for c in conns],
        [tuple(s) for s in secrets],
        tests,
        found,
        sorted(tuple(map(str, j)) for j in jobs),
    )


def test_a_cannot_reach_bs_snowflake_connection_cross_client(
    sf_client: TestClient, owner_db: Database, two: Two
) -> None:
    c, A, h = sf_client, two.a.base, as_user(sf_client, two.a.email)
    before = _b_state(owner_db, two, [two.b.connection])
    b_before = len(two.b.fake.requests)
    a_before = len(two.a.fake.requests)
    own, theirs, nobody = two.a.connection, two.b.connection, str(uuid7())
    cases: list[tuple[str, str, Any, int]] = [  # Any: a JSON body
        ("GET", "", None, 200),
        ("GET", "/tests", None, 200),
        ("GET", "/discoveries/latest", None, 200),
        ("POST", "/test", None, 202),
        ("POST", "/discover", None, 202),
        ("POST", "/invoke", {"sql": "SELECT 1"}, 202),
        ("PATCH", "", {"name": "renamed"}, 200),
        ("POST", "/status", {"status": "disabled"}, 200),
    ]
    for method, suffix, payload, ok in cases:
        r = c.request(method, f"{A}/connections/{own}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        if suffix == "/invoke":  # A's own invoke runs on the worker (issue #190)
            assert execute_invocation(c, A, r.json()["id"], tenant_id=two.a.tenant)
        missing = c.request(method, f"{A}/connections/{nobody}{suffix}", headers=h, json=payload)
        _same_404(
            c.request(method, f"{A}/connections/{theirs}{suffix}", headers=h, json=payload),
            missing,
        )
    assert _b_state(owner_db, two, [two.b.connection]) == before
    assert len(two.a.fake.requests) > a_before  # A's own invoke reached A's account
    assert len(two.b.fake.requests) == b_before  # B's account never heard from A


def test_a_cannot_set_or_rotate_bs_snowflake_credential_cross_client(
    sf_client: TestClient, owner_db: Database, two: Two
) -> None:
    """A's `PUT .../credential` on B's credential-less connection (set) and on B's connection
    with a key (rotate) answer exactly like an unknown id, in A's organization and under B's."""
    c = sf_client
    B, hb = two.b.base, as_user(c, two.b.email)
    unbound = connection(c, hb, _body("acct2", "sf-b-unbound"), B)
    ids = [two.b.connection, unbound["id"]]
    before = _b_state(owner_db, two, ids)
    b_before = len(two.b.fake.requests)
    A, h = two.a.base, as_user(c, two.a.email)
    own = c.get(f"{A}/connections/{two.a.connection}", headers=h).json()
    rotated = c.put(
        f"{A}/connections/{two.a.connection}/credential",
        headers=h,
        json={"value": PEM_A, "row_version": own["row_version"], "read_only_confirmed": True},
    )
    assert rotated.status_code == 200, rotated.text  # A's own connection first
    put = {"value": PEM_A, "row_version": 1, "read_only_confirmed": True}
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    for cid in (two.b.connection, unbound["id"]):  # rotate, set
        _same_404(
            c.put(f"{A}/connections/{cid}/credential", headers=h, json=put),
            c.put(f"{A}/connections/{uuid7()}/credential", headers=h, json=put),
        )
        _same_404(  # under B's organization: A is no member of it
            c.put(f"{B}/connections/{cid}/credential", headers=h, json=put),
            c.put(f"{unknown}/connections/{cid}/credential", headers=h, json=put),
        )
    assert _b_state(owner_db, two, ids) == before
    assert len(two.b.fake.requests) == b_before


def test_bs_organization_is_unknown_to_a_cross_client(sf_client: TestClient, two: Two) -> None:
    c, h = sf_client, as_user(sf_client, two.a.email)
    assert c.get(f"{two.a.base}/connections/{two.a.connection}", headers=h).status_code == 200
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    for method, path, payload in (
        ("GET", "/connections", None),
        ("GET", f"/connections/{two.b.connection}", None),
        ("POST", f"/connections/{two.b.connection}/test", None),
        ("POST", "/connections", _body("acct2", "sf-planted", credential={"value": PEM_A})),
    ):
        _same_404(
            c.request(method, f"{two.b.base}{path}", headers=h, json=payload),
            c.request(method, f"{unknown}{path}", headers=h, json=payload),
        )


def test_a_cannot_bind_bs_secret_to_its_snowflake_connection_cross_client(
    sf_client: TestClient,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    two: Two,
) -> None:
    c, A, h = sf_client, two.a.base, as_user(sf_client, two.a.email)
    theirs = secret(app_db, settings, two.b.tenant, PEM_B)
    before = _b_state(owner_db, two, [two.b.connection])
    nobody = str(uuid7())
    row = c.get(f"{A}/connections/{two.a.connection}", headers=h).json()
    assert row["secret_set"] is True
    patch_missing = c.patch(
        f"{A}/connections/{two.a.connection}",
        headers=h,
        json={"secret_id": nobody, "row_version": row["row_version"], "read_only_confirmed": True},
    )
    _same_404(
        c.patch(
            f"{A}/connections/{two.a.connection}",
            headers=h,
            json={
                "secret_id": theirs,
                "row_version": row["row_version"],
                "read_only_confirmed": True,
            },
        ),
        patch_missing,
    )
    after = c.get(f"{A}/connections/{two.a.connection}", headers=h).json()
    assert after["row_version"] == row["row_version"]
    assert _b_state(owner_db, two, [two.b.connection]) == before
