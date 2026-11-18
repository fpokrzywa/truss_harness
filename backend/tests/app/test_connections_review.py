"""Regression tests for the P3 critic findings (each test names the finding it pins)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.conformance import connection as conf_connection
from shifde.adapters.conformance import exec_ctx, loopback_guard
from shifde.adapters.rest_http import RestHttpAdapter
from shifde.adapters.testservers import McpTestServer, RestTestServer
from shifde.core.egress import (
    LOOPBACK_NETWORKS,
    EgressGuard,
    EgressPolicy,
)
from tests._support.conntests import result_of, run_test
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, system_role
from tests._support.p3util import (
    ADMIN,
    REST_TOKEN,
    A,
    activate,
    connection,
    fake_model_stack,
    invoke_body,
    pg_body,
    rest_body,
    secret,
)

PUBLIC = "93.184.216.34"


def _login_user(
    client: TestClient, world: World, email: str, role_key: str, owner: Session
) -> dict[str, str]:
    admin = login(client, world.passwords, ADMIN)
    new_user(
        client, admin, "demo-a", email, "Passw0rd!-test-1", [str(system_role(owner, role_key))]
    )
    return login(client, {email: "Passw0rd!-test-1"}, email)


# ------------------------------------------------------------------ 1. model_api bypass
def test_model_connections_cannot_be_invoked_as_raw_connections(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    ids = fake_model_stack(client, h)
    body = {"model": "fake-chat", "messages": [{"role": "user", "content": "spend credits"}]}
    refused = client.post(f"{A}/connections/{ids['connection']}/invoke", json=body, headers=h)
    assert refused.status_code == 422 and refused.json()["code"] == "use_model_invoke_endpoint"
    # the seeded operator holds connection:use but not model:invoke: still refused
    ho = _login_user(client, world, "ops@demo-a.local", "operator", owner_session)
    r = client.post(f"{A}/connections/{ids['connection']}/invoke", json=body, headers=ho)
    assert r.status_code == 422 and r.json()["code"] == "use_model_invoke_endpoint"
    assert (
        client.post(f"{A}/models/{ids['model']}/invoke", json=invoke_body(), headers=ho).status_code
        == 403
    )
    # the refusal is audited, and testing a model connection stays possible
    ha = login(client, world.passwords, ADMIN)
    assert run_test(client, ha, A, ids["connection"])["status"] == "done"
    failures = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.audit_events WHERE action = 'connection.invoked' "
            "AND outcome = 'failure'"
        )
    ).scalar_one()
    assert failures == 2


def test_open_for_use_refuses_model_connections(
    client: TestClient, world: World, owner_session: Session
) -> None:
    from uuid import UUID

    from tests.app.test_connections_api import _service

    h = login(client, world.passwords, ADMIN)
    ids = fake_model_stack(client, h)
    svc, session = _service(client, owner_session, ADMIN)
    from shifde.core.errors import UseModelsEndpoint

    with pytest.raises(UseModelsEndpoint), svc.open_for_use(UUID(ids["connection"]), "x"):
        pass
    session.close()


# ------------------------------------------------------------------ 2. compressed bodies
class _Rec(httpx.BaseTransport):
    def __init__(self, responder: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)  # type: ignore[no-any-return]


def _guard(inner: httpx.BaseTransport, **policy: Any) -> EgressGuard:
    layer = EgressPolicy(allowed_schemes=frozenset({"http"}), **policy)
    return EgressGuard([layer], resolver=lambda h: [PUBLIC], inner=inner)


# ------------------------------------------------------------------ 3. server-side cursor
def _pg_conn(url: str, **extra: Any) -> Any:
    from sqlalchemy.engine import make_url

    u = make_url(url)
    cfg = {"host": u.host, "port": u.port, "database": u.database, "user": u.username, **extra}
    return conf_connection("postgres", cfg, auth_method="basic", secret=u.password)


# ------------------------------------------------------------------ 4. no lock across the call


# ------------------------------------------------------------------ 5. relative redirects


# ------------------------------------------------------------------ 6. request bodies
def test_malformed_invoke_bodies_are_422_and_audited(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    cid = connection(client, h, rest_body(rest_server, secret(client, h, "t", REST_TOKEN)))["id"]
    activate(client, h, cid)
    url = f"{A}/connections/{cid}/invoke"
    for bad in (
        {"headers": "x", "path": "/echo"},
        {"path": 7},
        {"path": "/echo", "method": "TRACE"},
        {"path": "/echo", "query": {"a": [1]}},
        {"path": "/echo", "surprise": 1},
        {},
    ):
        r = client.post(url, json=bad, headers=h)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", bad
        assert "errors" in r.json()
    ok = client.post(url, json={"method": "get", "path": "/echo"}, headers=h)
    assert ok.status_code == 200
    failed = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.audit_events WHERE action = 'connection.invoked' "
            "AND outcome = 'failure'"
        )
    ).scalar_one()
    assert failed == 6
    # adapters stay robust when used server-side with a malformed request
    adapter = RestHttpAdapter()
    conn = conf_connection("rest.http", {"base_url": rest_server.base_url})
    with pytest.raises(AdapterFailure) as exc:
        adapter.open(exec_ctx(loopback_guard()), conn).invoke({"path": "/echo", "headers": "x"})
    assert exc.value.error_class is ErrorClass.CONFIG


# ------------------------------------------------------------------ 7. tool allowlist


# ------------------------------------------------------------------ 8. more hard ranges


# ------------------------------------------------------------------ 9. no PG* fallback


# ------------------------------------------------------------------ 10. default policy PATCH


# ------------------------------------------------------------------ 11. degraded is denied
def test_degraded_connections_are_denied(
    client: TestClient, world: World, rest_server: RestTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    s = secret(client, h, "t", REST_TOKEN)
    cid = connection(client, h, rest_body(rest_server, s))["id"]
    activate(client, h, cid)
    client.post(f"{A}/secrets/{s}/rotate", json={"value": "rotated-token-888"}, headers=h)
    assert result_of(client, h, A, cid)["outcome"] == "fail"
    r = client.post(f"{A}/connections/{cid}/invoke", json={"path": "/echo"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "connection_disabled"


# ------------------------------------------------------------------ 13. stale test vs definition
def test_activation_requires_the_test_to_cover_the_current_definition(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    cid = connection(client, h, rest_body(rest_server, secret(client, h, "t", REST_TOKEN)))["id"]
    assert result_of(client, h, A, cid)["outcome"] == "pass"
    # simulate a PATCH that raced the test: the definition changed but last_tested_at survived
    owner_session.execute(
        text(
            "UPDATE shifde.connections SET config = jsonb_set(config, '{health_path}', '\"/other\"') "
            "WHERE id = :c"
        ),
        {"c": cid},
    )
    owner_session.commit()
    row = client.get(f"{A}/connections/{cid}", headers=h).json()
    assert row["last_tested_at"] is not None
    r = client.post(
        f"{A}/connections/{cid}/status",
        json={"status": "active", "row_version": row["row_version"]},
        headers=h,
    )
    assert r.status_code == 409
    assert result_of(client, h, A, cid)["outcome"] == "pass"
    row = client.get(f"{A}/connections/{cid}", headers=h).json()
    ok = client.post(
        f"{A}/connections/{cid}/status",
        json={"status": "active", "row_version": row["row_version"]},
        headers=h,
    )
    assert ok.status_code == 200
    _ = LOOPBACK_NETWORKS


# ================================================================== round 2


# Ported in app-2 part 2c over `sample_db_url` (B-2420, B-2423; SETH test_p3_review.py).
def test_database_and_mcp_bodies_are_validated(
    client: TestClient, world: World, sample_db_url: str, mcp_server: McpTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    pw = sample_db_url.split(":")[2].split("@")[0]
    pg = connection(client, h, pg_body(sample_db_url, secret(client, h, "pg", pw)))["id"]
    activate(client, h, pg)
    for bad in ({}, {"sql": ""}, {"sql": "SELECT 1", "params": "x"}, {"sql": 5}):
        assert client.post(f"{A}/connections/{pg}/invoke", json=bad, headers=h).status_code == 422
