"""A `snowflake` connection through the connection routes (CM-4, issue #162; design 2026-11-22
sections 7.3 and 7.5): created with an inline `key_pair` credential, then tested (on the worker's
job), discovered and invoked against the in-process fake, injected through the runtime's
`resolver`/`inner` hooks. An invalid PEM is 422 on create and on `PUT .../credential`, without
echo. Another client gets 404. Fakes only: no request leaves the process."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any  # Any: JSON bodies

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from shifde.adapters.dataplatforms.fakes import FakeSnowflakeAccount
from shifde.core.settings import Settings
from tests._support.conntests import discovery_of, invoke_outcome
from tests.connections.routes import activate, as_user, connection, connections_client, org
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PKCS1 = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
KEY_BODY = PEM.splitlines()[1]  # a line of the key material


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "name": "warehouse",
        "connection_type": "data_platform",
        "adapter_key": "snowflake",
        "config": {"account": "acct1", "user": "reader", "warehouse": "COMPUTE_WH"},
        "auth_method": "key_pair",
        **extra,
    }


@pytest.fixture
def fake() -> FakeSnowflakeAccount:
    return FakeSnowflakeAccount(account="acct1", user="reader", public_key=KEY.public_key())


@pytest.fixture
def sf_client(settings: Settings, fake: FakeSnowflakeAccount) -> Iterator[TestClient]:
    """The routes with the runtime's network hooks pointed at the fake."""
    with connections_client(settings) as c:
        app: Any = c.app  # Any: the FastAPI app behind the test client
        app.state.integration = dataclasses.replace(
            app.state.integration, resolver=lambda host: ["127.0.0.1"], inner=fake.transport()
        )
        yield c


def _locs(r: Any) -> list[list[str]]:  # Any: httpx.Response
    return [e["loc"] for e in r.json().get("errors") or []]


def test_create_test_discover_and_invoke_against_the_fake(
    sf_client: TestClient, fake: FakeSnowflakeAccount, world: World, people: People
) -> None:
    c, A, B = sf_client, org(world.a.slug), org(world.b.slug)
    h = as_user(c, people.owner_a)
    made = connection(c, h, _body(credential={"value": PEM}, read_only_confirmed=True), A)
    cid = made["id"]
    assert PEM not in str(made) and KEY_BODY not in str(made)
    activated = activate(c, h, cid, A)  # the worker's run_test_job against the fake
    assert activated["status"] == "active"
    assert fake.requests[0].headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"
    discovered = discovery_of(c, h, A, cid, tenant_id=world.a.tenant)  # the worker's job
    tables = discovered["payload"]["tables"]
    assert ("ANALYTICS", "PUBLIC", "CUSTOMERS") in {
        (t["catalog"], t["schema"], t["name"]) for t in tables
    }
    # the invoke is queued (issue #190): its job runs, the outcome is read on the status route
    invoked = invoke_outcome(c, h, A, cid, {"sql": "SELECT 1"}, tenant_id=world.a.tenant)
    assert invoked[0] == 200, invoked
    assert invoked[1]["result"] == {"columns": ["1"], "rows": [["1"]], "row_count": 1}
    sent = len(fake.requests)
    refused = invoke_outcome(c, h, A, cid, {"sql": "DROP TABLE t"}, tenant_id=world.a.tenant)
    assert refused[0] == 422, refused
    assert len(fake.requests) == sent  # refused before any request
    for text in (activated, discovered, invoked, refused):
        assert KEY_BODY not in str(text)

    hb = as_user(c, people.owner_b)
    for path, body in (("test", {}), ("discover", {}), ("invoke", {"sql": "SELECT 1"})):
        r = c.post(f"{B}/connections/{cid}/{path}", headers=hb, json=body)
        assert r.status_code == 404, (path, r.text)
    assert c.get(f"{B}/connections/{cid}", headers=hb).status_code == 404


@pytest.mark.parametrize("value", [PKCS1, "not a pem", PEM.replace(KEY_BODY, "AAAA")])
def test_an_invalid_pem_on_create_is_422_without_echo(
    sf_client: TestClient, fake: FakeSnowflakeAccount, world: World, people: People, value: str
) -> None:
    A = org(world.a.slug)
    h = as_user(sf_client, people.owner_a)
    body = {**_body(), "scope": {}, "credential": {"value": value}, "read_only_confirmed": True}
    r = sf_client.post(f"{A}/connections", headers=h, json=body)
    assert r.status_code == 422 and _locs(r) == [["body", "credential", "value"]], r.text
    for line in value.splitlines():
        if "-----" not in line:
            assert line not in r.text
    assert fake.requests == []


def test_an_invalid_pem_on_put_credential_is_422_without_echo(
    sf_client: TestClient, world: World, people: People
) -> None:
    A = org(world.a.slug)
    h = as_user(sf_client, people.owner_a)
    made = connection(sf_client, h, _body(credential={"value": PEM}, read_only_confirmed=True), A)
    r = sf_client.put(
        f"{A}/connections/{made['id']}/credential",
        headers=h,
        json={"value": PKCS1, "row_version": made["row_version"], "read_only_confirmed": True},
    )
    assert r.status_code == 422 and _locs(r) == [["body", "value"]], r.text
    assert PKCS1.splitlines()[1] not in r.text
    ok = sf_client.put(
        f"{A}/connections/{made['id']}/credential",
        headers=h,
        json={"value": PEM, "row_version": made["row_version"], "read_only_confirmed": True},
    )
    assert ok.status_code == 200, ok.text
    assert KEY_BODY not in ok.text


DEV_PAT = "sf-dev-route-pat-0001"  # scan-secrets: allow, any non-empty token passes the dev fake


@pytest.fixture
def dev_client(settings: Settings) -> Iterator[TestClient]:
    """The routes with `SHIFDE_DEV_FAKE_DATA_PLATFORMS` on (test settings), no injected hooks."""
    with connections_client(settings.model_copy(update={"dev_fake_data_platforms": True})) as c:
        yield c


def test_the_dev_switch_serves_a_snowflake_connection(
    dev_client: TestClient, world: World, people: People
) -> None:
    c, A = dev_client, org(world.a.slug)
    h = as_user(c, people.owner_a)
    body = _body(auth_method="bearer", credential={"value": DEV_PAT}, read_only_confirmed=True)
    made = connection(c, h, body, A)
    cid = made["id"]
    activated = activate(c, h, cid, A)  # the worker's run_test_job against the dev fake
    assert activated["status"] == "active"
    discovered = discovery_of(c, h, A, cid, tenant_id=world.a.tenant)  # the worker's job
    tables = discovered["payload"]["tables"]
    assert ("ANALYTICS", "PUBLIC", "CUSTOMERS") in {
        (t["catalog"], t["schema"], t["name"]) for t in tables
    }
    invoked = invoke_outcome(c, h, A, cid, {"sql": "SELECT 1"}, tenant_id=world.a.tenant)
    assert invoked[0] == 200, invoked
    assert invoked[1]["result"] == {"columns": ["1"], "rows": [["1"]], "row_count": 1}
    for text in (made, activated, discovered, invoked):
        assert DEV_PAT not in str(text)
