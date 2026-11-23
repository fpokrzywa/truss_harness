"""The adapter hooks through the connection routes (CM-3, issue #161; design 2026-11-22 sections
7.1 and 7.5, B-2753, B-2755): `normalize_config` on create and on a PATCH with `config` (never on
the worker's test or on discover / invoke), `validate_credential` on the inline credential and
`PUT .../credential`, and the development switch that sends a `databricks` connection to the
in-process fake. Another client gets 404 throughout. Fakes only: no request leaves the process."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any  # Any: JSON bodies

import pytest
from fastapi.testclient import TestClient

from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.core.settings import Settings
from tests._support.conntests import discovery_of, invoke_outcome
from tests.connections.routes import activate, as_user, connection, connections_client, org
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

TOKEN = "cm3-hooks-token-0001"  # scan-secrets: allow, accepted by the development fake
HOST_LOC = ["body", "config", "host"]


def _body(host: str = "ws1.cloud.databricks.com", **extra: Any) -> dict[str, Any]:
    return {
        "name": f"lakehouse-{host.split('.')[0].lower()}",
        "connection_type": "data_platform",
        "adapter_key": "databricks",
        "config": {"host": host, "warehouse_id": "wh-0001"},
        "auth_method": "bearer",
        **extra,
    }


@pytest.fixture
def dev_client(settings: Settings) -> Iterator[TestClient]:
    """The routes with `SHIFDE_DEV_FAKE_DATA_PLATFORMS` on (test settings)."""
    with connections_client(settings.model_copy(update={"dev_fake_data_platforms": True})) as c:
        yield c


def _locs(r: Any) -> list[list[str]]:  # Any: httpx.Response
    return [e["loc"] for e in r.json().get("errors") or []]


# ------------------------------------------------------------------ normalize_config
def test_create_stores_the_normalized_host(
    client: TestClient, world: World, people: People
) -> None:
    A = org(world.a.slug)
    h = as_user(client, people.owner_a)
    made = connection(client, h, _body("WS1.cloud.databricks.com"), A)
    assert made["config"] == {"host": "https://ws1.cloud.databricks.com", "warehouse_id": "wh-0001"}
    got = client.get(f"{A}/connections/{made['id']}", headers=h).json()
    assert got["config"]["host"] == "https://ws1.cloud.databricks.com"


def test_a_patch_with_config_normalizes_too(
    client: TestClient, world: World, people: People
) -> None:
    A = org(world.a.slug)
    h = as_user(client, people.owner_a)
    made = connection(client, h, _body(), A)
    r = client.patch(
        f"{A}/connections/{made['id']}",
        headers=h,
        json={"config": {"host": "ws2.cloud.databricks.com"}, "row_version": made["row_version"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"] == {"host": "https://ws2.cloud.databricks.com"}


@pytest.mark.parametrize(
    "host",
    [
        "ws1.refused-suffix.example",
        "https://203.0.113.9",
        "http://ws1.cloud.databricks.com",
        "https://ws1.cloud.databricks.com:8443",
        "https://reader@ws1.cloud.databricks.com",
    ],
)
def test_a_refused_host_is_422_without_echo(
    client: TestClient, world: World, people: People, host: str
) -> None:
    A = org(world.a.slug)
    h = as_user(client, people.owner_a)
    r = client.post(f"{A}/connections", headers=h, json={**_body(host), "scope": {}})
    assert r.status_code == 422 and _locs(r) == [HOST_LOC], r.text
    bare = host.split("://")[-1]
    assert bare not in r.text and "203.0.113.9" not in r.text
    made = connection(client, h, _body(), A)
    r = client.patch(
        f"{A}/connections/{made['id']}",
        headers=h,
        json={"config": {"host": host}, "row_version": made["row_version"]},
    )
    assert r.status_code == 422 and _locs(r) == [HOST_LOC], r.text
    assert bare not in r.text
    got = client.get(f"{A}/connections/{made['id']}", headers=h).json()
    assert got["config"]["host"] == "https://ws1.cloud.databricks.com"  # nothing was stored


def test_an_extra_suffix_from_settings_is_accepted(
    settings: Settings, client: TestClient, world: World, people: People
) -> None:
    A = org(world.a.slug)
    body = _body("ws9.databricks.example.org")
    h = as_user(client, people.owner_a)
    refused = client.post(f"{A}/connections", headers=h, json={**body, "scope": {}})
    assert refused.status_code == 422 and _locs(refused) == [HOST_LOC]
    custom = settings.model_copy(
        update={"delivery_databricks_host_suffixes": ["databricks.example.org"]}
    )
    with connections_client(custom) as c:
        h = as_user(c, people.owner_a)
        made = connection(c, h, body, A)
    assert made["config"]["host"] == "https://ws9.databricks.example.org"


# ------------------------------------------------------------------ development switch
def test_the_fake_runs_test_discover_and_invoke_without_normalizing(
    dev_client: TestClient, world: World, people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, A, B = dev_client, org(world.a.slug), org(world.b.slug)
    h = as_user(c, people.owner_a)
    made = connection(c, h, _body(credential={"value": TOKEN}, read_only_confirmed=True), A)
    cid = made["id"]
    calls: list[Any] = []
    monkeypatch.setattr(
        DatabricksAdapter, "normalize_config", lambda self, *a: calls.append(a) or a[0]
    )
    activated = activate(c, h, cid, A)  # the worker's run_test_job against the fake
    assert activated["status"] == "active"
    discovered = discovery_of(c, h, A, cid, tenant_id=world.a.tenant)  # the worker's job
    tables = discovered["payload"]["tables"]
    assert ("main", "default", "customers") in {
        (t["catalog"], t["schema"], t["name"]) for t in tables
    }
    # the invoke is queued (issue #190): its job runs, the outcome is read on the status route
    invoked = invoke_outcome(c, h, A, cid, {"sql": "SELECT 1"}, tenant_id=world.a.tenant)
    assert invoked[0] == 200, invoked
    assert invoked[1]["result"] == {"columns": ["1"], "rows": [["1"]], "row_count": 1}
    refused = invoke_outcome(c, h, A, cid, {"sql": "DROP TABLE t"}, tenant_id=world.a.tenant)
    assert refused[0] == 422, refused
    assert calls == []  # neither the worker's test nor discover / invoke normalized
    for text in (activated, discovered, invoked, refused):
        assert TOKEN not in str(text)

    hb = as_user(c, people.owner_b)
    for path, body in (("test", {}), ("discover", {}), ("invoke", {"sql": "SELECT 1"})):
        r = c.post(f"{B}/connections/{cid}/{path}", headers=hb, json=body)
        assert r.status_code == 404, (path, r.text)
    assert c.get(f"{B}/connections/{cid}", headers=hb).status_code == 404
    r = c.patch(
        f"{B}/connections/{cid}",
        headers=hb,
        json={"config": {"host": "ws3.cloud.databricks.com"}, "row_version": 1},
    )
    assert r.status_code == 404


# ------------------------------------------------------------------ validate_credential
@pytest.mark.parametrize("value", ["two words", " padded", "tab\tvalue"])
def test_an_invalid_inline_credential_is_422_without_echo(
    client: TestClient, world: World, people: People, value: str
) -> None:
    A = org(world.a.slug)
    h = as_user(client, people.owner_a)
    body = {**_body(), "scope": {}, "credential": {"value": value}, "read_only_confirmed": True}
    r = client.post(f"{A}/connections", headers=h, json=body)
    assert r.status_code == 422 and _locs(r) == [["body", "credential", "value"]], r.text
    assert value not in r.text and value.strip() not in r.text


def test_an_invalid_put_credential_is_422_without_echo(
    client: TestClient, world: World, people: People
) -> None:
    A = org(world.a.slug)
    h = as_user(client, people.owner_a)
    made = connection(client, h, _body(), A)
    value = "has a space"
    r = client.put(
        f"{A}/connections/{made['id']}/credential",
        headers=h,
        # a stale row_version too: the value check (step 6) answers before it (step 7)
        json={"value": value, "row_version": made["row_version"] + 5, "read_only_confirmed": True},
    )
    assert r.status_code == 422 and _locs(r) == [["body", "value"]], r.text
    assert value not in r.text
    ok = client.put(
        f"{A}/connections/{made['id']}/credential",
        headers=h,
        json={"value": TOKEN, "row_version": made["row_version"], "read_only_confirmed": True},
    )
    assert ok.status_code == 200, ok.text
    assert TOKEN not in ok.text
