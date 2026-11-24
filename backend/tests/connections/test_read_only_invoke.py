"""#267: a `read` invoke refuses a write keyword anywhere in the code, not only a write first
keyword. On a Databricks `data_platform` connection served by the in-process fake
(`dev_fake_data_platforms`): `WITH ... INSERT`, `WITH ... DELETE` and a second statement are 422
with a `capability` failure audit, `SELECT 1` still answers; client A's refusal carries nothing of
client B's connection, and B gets 404 on A's connection. #285: on a Snowflake connection a
`SYSTEM$` function is refused the same way."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any  # Any: JSON bodies, row values

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.conntests import execute_invocation, invocation_status, invoke_outcome
from tests.connections.routes import activate, as_user, connection, connections_client, org
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

TOKEN_A = "rq267-token-a-0001"  # scan-secrets: allow, accepted by the development fake
TOKEN_B = "rq267-token-b-zz-9f3c"  # scan-secrets: allow, accepted by the development fake
B_NAME = "client-b-lakehouse-q7x"
WITH_INSERT = "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a"
SF_PAT = "rq285-sf-pat-0001"  # scan-secrets: allow, any non-empty token passes the dev fake
REFUSED = [WITH_INSERT, "WITH a AS (SELECT 1) DELETE FROM t", "SELECT 1; DELETE FROM t"]


def _body(name: str, token: str) -> dict[str, Any]:
    return {
        "name": name,
        "connection_type": "data_platform",
        "adapter_key": "databricks",
        "config": {"host": "ws1.cloud.databricks.com", "warehouse_id": "wh-0001"},
        "auth_method": "bearer",
        "credential": {"value": token},
        "read_only_confirmed": True,
    }


@pytest.fixture
def dev_client(settings: Settings) -> Iterator[TestClient]:
    """The routes with `SHIFDE_DEV_FAKE_DATA_PLATFORMS` on (test settings)."""
    with connections_client(settings.model_copy(update={"dev_fake_data_platforms": True})) as c:
        yield c


def _invoked(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT outcome, details FROM shifde.audit_events "
                "WHERE action = 'connection.invoked' AND target_id = :c ORDER BY occurred_at, id"
            ),
            {"c": cid},
        ).all()
    return [tuple(r) for r in rows]


def test_a_write_keyword_anywhere_is_refused_through_invoke(
    dev_client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    c, A = dev_client, org(world.a.slug)
    h = as_user(c, people.owner_a)
    cid = connection(c, h, _body("lakehouse-a", TOKEN_A), A)["id"]
    assert activate(c, h, cid, A)["status"] == "active"
    for sql in REFUSED:
        status, body = invoke_outcome(c, h, A, cid, {"sql": sql}, tenant_id=world.a.tenant)
        assert status == 422, (sql, body)
        assert body["status"] == "failed" and body["result"] is None, body
    ok = invoke_outcome(c, h, A, cid, {"sql": "SELECT 1"}, tenant_id=world.a.tenant)
    assert ok[0] == 200 and ok[1]["result"]["rows"] == [["1"]], ok
    audits = _invoked(owner_db, cid)
    assert [(o, d.get("error_class")) for o, d in audits] == [
        *[("failure", "capability")] * len(REFUSED),
        ("success", None),
    ]


def test_a_refusal_leaks_nothing_of_another_client(
    dev_client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    c, A, B = dev_client, org(world.a.slug), org(world.b.slug)
    hb = as_user(c, people.owner_b)
    b_cid = connection(c, hb, _body(B_NAME, TOKEN_B), B)["id"]
    assert activate(c, hb, b_cid, B)["status"] == "active"
    ha = as_user(c, people.owner_a)  # signing in replaces the session
    a_cid = connection(c, ha, _body("lakehouse-a", TOKEN_A), A)["id"]
    assert activate(c, ha, a_cid, A)["status"] == "active"

    posted = c.post(f"{A}/connections/{a_cid}/invoke", headers=ha, json={"sql": WITH_INSERT})
    assert posted.status_code == 202, posted.text
    inv = posted.json()["id"]
    execute_invocation(c, A, inv, tenant_id=world.a.tenant)
    body = invocation_status(c, ha, A, a_cid, inv)
    assert (body["status"], body["error"]) == ("failed", "validation_error"), body
    audits = _invoked(owner_db, a_cid)
    assert audits and all(o == "failure" for o, _ in audits)
    for text_ in (posted.text, str(body), str(audits)):
        for secret in (b_cid, B_NAME, str(world.b.tenant), TOKEN_B):
            assert secret not in text_, secret

    hb = as_user(c, people.owner_b)
    for base in (B, A):
        r = c.post(f"{base}/connections/{a_cid}/invoke", headers=hb, json={"sql": WITH_INSERT})
        assert r.status_code == 404, (base, r.text)
    assert len(_invoked(owner_db, a_cid)) == len(audits)


def test_a_snowflake_system_function_is_refused_through_invoke(
    dev_client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    c, A = dev_client, org(world.a.slug)
    h = as_user(c, people.owner_a)
    body = {
        "name": "warehouse-a",
        "connection_type": "data_platform",
        "adapter_key": "snowflake",
        "config": {"account": "acct1", "user": "reader", "warehouse": "COMPUTE_WH"},
        "auth_method": "bearer",
        "credential": {"value": SF_PAT},
        "read_only_confirmed": True,
    }
    cid = connection(c, h, body, A)["id"]
    assert activate(c, h, cid, A)["status"] == "active"
    sql = "SELECT SYSTEM$CANCEL_ALL_QUERIES()"
    status, refused = invoke_outcome(c, h, A, cid, {"sql": sql}, tenant_id=world.a.tenant)
    assert status == 422, refused
    assert refused["status"] == "failed" and refused["result"] is None, refused
    assert "SYSTEM$" not in str(refused).upper()
    ok = invoke_outcome(c, h, A, cid, {"sql": "SELECT 1"}, tenant_id=world.a.tenant)
    assert ok[0] == 200 and ok[1]["result"]["rows"] == [["1"]], ok
    audits = _invoked(owner_db, cid)
    assert [(o, d.get("error_class")) for o, d in audits] == [
        ("failure", "capability"),
        ("success", None),
    ]
