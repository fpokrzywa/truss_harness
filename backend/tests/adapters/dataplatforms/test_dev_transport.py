"""The development switch's transport `dev_transport()` (issue #230): a Snowflake connection's
test, discover and invoke succeed through the guard `build_guard` builds under
`SHIFDE_DEV_FAKE_DATA_PLATFORMS`, for key-pair and PAT auth, with sockets refused; a Databricks
host still gets its permissive workspace and the strict Snowflake fake still refuses."""

from __future__ import annotations

import socket
from typing import Any  # Any: monkeypatched callables

import httpx
import pytest

from shifde.adapters.base import ExecContext
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import SELECT_ONE, FakeSnowflakeAccount, dev_transport
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.core.egress import EgressGuard, installation_layer
from tests.adapters.dataplatforms.helpers import SF_PEM, conn, sf_conn

DEV_PAT = "sf-dev-any-pat"  # scan-secrets: allow, any non-empty token passes the dev fake
SECRETS = {"key_pair": SF_PEM, "bearer": DEV_PAT}


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:  # Any: socket call signatures
        raise AssertionError("a dev-transport test opened a socket")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def dev_ctx() -> ExecContext:
    guard = EgressGuard(
        [installation_layer(None, env="development")],
        resolver=lambda host: ["127.0.0.1"],
        inner=dev_transport(),
    )
    return ExecContext(tenant_id=None, principal_id=None, egress=guard)


@pytest.mark.parametrize("auth", ["key_pair", "bearer"])
def test_snowflake_test_discover_and_invoke_succeed(auth: str) -> None:
    adapter = SnowflakeAdapter()
    c = sf_conn(auth, SECRETS[auth])
    out = adapter.test(dev_ctx(), c)
    assert out.passed and out.error_class is None, out.checks
    found = adapter.discover(dev_ctx(), c)
    tables = {(t["catalog"], t["schema"], t["name"]) for t in found.payload["tables"]}
    assert ("ANALYTICS", "PUBLIC", "CUSTOMERS") in tables
    session = adapter.open(dev_ctx(), c)
    try:
        result = session.invoke({"sql": SELECT_ONE})
    finally:
        session.close()
    assert result.result["columns"] == [{"name": "1", "type": "text"}]
    assert result.result["rows"] == [["1"]]


def test_a_databricks_host_still_gets_a_permissive_workspace() -> None:
    c = conn(secret="any-dbx-token")  # scan-secrets: allow, any non-empty token passes
    out = DatabricksAdapter().test(dev_ctx(), c)
    assert out.passed and out.error_class is None, out.checks


def test_the_strict_snowflake_fake_still_refuses_an_arbitrary_token() -> None:
    sf = FakeSnowflakeAccount()
    request = httpx.Request(
        "POST",
        f"https://{sf.host}/api/v2/statements",
        headers={
            "Authorization": f"Bearer {DEV_PAT}",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        },
        json={"statement": SELECT_ONE, "parameters": {"MULTI_STATEMENT_COUNT": "1"}},
    )
    assert sf.handle(request).status_code == 401
