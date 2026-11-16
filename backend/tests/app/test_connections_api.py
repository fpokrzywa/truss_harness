"""P3 connections: create/test/discover/invoke, status rules, authorization, egress, secrets."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import McpTestServer, RestTestServer
from shifde.authz.service import new_pdp
from shifde.connections.service import ConnectionService
from shifde.core import db as dbmod
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import ConnectionDisabled, Forbidden, NotFound
from shifde.tenancy.scope import TenantScope
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post, system_role
from tests._support.p3util import (
    ADMIN,
    ADMIN_B,
    MCP_TOKEN,
    PLATFORM,
    REST_TOKEN,
    VIEWER,
    A,
    B,
    activate,
    connection,
    rest_body,
    secret,
)

CHECKS = ["config", "egress", "dns", "tls", "auth", "capability"]


def _row(client: TestClient, h: dict[str, str], cid: str) -> dict[str, Any]:
    out: dict[str, Any] = client.get(f"{A}/connections/{cid}", headers=h).json()
    return out


# ================================================================== acceptance 3 + 4


def test_rest_writes_and_mcp_side_effects_need_write_permission(
    client: TestClient, world: World, rest_server: RestTestServer, mcp_server: McpTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    rest = connection(client, h, rest_body(rest_server, secret(client, h, "t1", REST_TOKEN)))
    mcp = connection(
        client,
        h,
        {
            "name": "mcp-srv",
            "connection_type": "mcp",
            "adapter_key": "mcp_http",
            "config": {"url": mcp_server.url},
            "auth_method": "bearer",
            "secret_id": secret(client, h, "t2", MCP_TOKEN),
            "access_mode": "read_write",
        },
    )
    activate(client, h, rest["id"])
    activate(client, h, mcp["id"])
    post_req = {"method": "POST", "path": "/echo", "body": {"a": 1}}
    assert (
        client.post(f"{A}/connections/{rest['id']}/invoke", json=post_req, headers=h).status_code
        == 422
    )
    assert (
        client.post(
            f"{A}/connections/{rest['id']}/invoke", json={**post_req, "write": True}, headers=h
        ).status_code
        == 403
    )
    wipe = {"tool": "wipe", "arguments": {"target": "x"}}
    assert (
        client.post(f"{A}/connections/{mcp['id']}/invoke", json=wipe, headers=h).status_code == 422
    )
    assert "wipe" not in mcp_server.calls
    ok = client.post(f"{A}/connections/{mcp['id']}/invoke", json={**wipe, "write": True}, headers=h)
    assert ok.status_code == 200 and "wipe" in mcp_server.calls
    # absolute URLs / scheme-relative paths cannot escape the configured base URL
    for path in ("http://evil.test/x", "//evil.test/x"):
        r = client.post(
            f"{A}/connections/{rest['id']}/invoke", json={"method": "GET", "path": path}, headers=h
        )
        assert r.status_code == 422


def test_failed_tests_are_classified_and_never_leak_the_secret(
    client: TestClient,
    world: World,
    rest_server: RestTestServer,
    owner_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = login(client, world.passwords, ADMIN)
    wrong = "totally-wrong-token-xyz"
    bad = connection(client, h, rest_body(rest_server, secret(client, h, "bad", wrong), "bad-auth"))
    res = client.post(f"{A}/connections/{bad['id']}/test", headers=h).json()
    assert res["outcome"] == "fail" and res["error_class"] == "auth"
    by_name = {c["name"]: c["status"] for c in res["checks"]}
    assert (
        by_name["auth"] == "fail"
        and by_name["config"] == "pass"
        and by_name["capability"] == "skipped"
    )
    assert _row(client, h, bad["id"])["health_status"] == "unhealthy"
    # unreachable target -> classified network/capability failure, secret still unseen
    refused = connection(
        client,
        h,
        {**rest_body(rest_server, None, "dead"), "config": {"base_url": "http://127.0.0.1:9"}},
    )
    dead = client.post(f"{A}/connections/{refused['id']}/test", headers=h).json()
    assert dead["outcome"] == "fail" and dead["error_class"] in ("network", "timeout")
    # destinations the egress policy forbids fail at the egress check, before any connect
    meta = connection(
        client,
        h,
        {**rest_body(rest_server, None, "meta"), "config": {"base_url": "http://169.254.169.254"}},
    )
    out = client.post(f"{A}/connections/{meta['id']}/test", headers=h).json()
    by_name = {c["name"]: c["status"] for c in out["checks"]}
    assert (
        out["error_class"] == "egress" and by_name["dns"] == "fail" or by_name["egress"] == "fail"
    )
    assert out["outcome"] == "fail"
    # the secret never shows in responses, stored results, audit rows or logs
    haystack = json.dumps(res) + json.dumps(dead) + json.dumps(out) + json.dumps(bad)
    for table, column in (
        ("connection_tests", "checks::text || coalesce(redacted_message,'')"),
        ("audit_events", "details::text"),
        ("connections", "config::text"),
    ):
        haystack += "".join(
            owner_session.execute(text(f"SELECT {column} FROM shifde.{table}")).scalars()
        )
    assert wrong not in haystack and REST_TOKEN not in haystack
    assert wrong not in capsys.readouterr().out


def test_config_must_match_the_adapter_schema_and_never_hold_credentials(
    client: TestClient, world: World, rest_server: RestTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    base = rest_body(rest_server, None)
    cases: list[dict[str, Any]] = [
        {**base, "config": {"base_url": rest_server.base_url, "password": "hunter2!!"}},
        {
            **base,
            "config": {"base_url": rest_server.base_url, "default_headers": {"Authorization": "x"}},
        },
        {**base, "config": {"base_url": rest_server.base_url, "nested": {"api_key": "k"}}},
        {**base, "config": {}},  # required base_url missing
        {**base, "config": {"base_url": "ftp://x"}},
        {**base, "connection_type": "mcp"},  # adapter serves rest
        {**base, "adapter_key": "nope"},
        {
            **base,
            "auth_method": "basic",
            "config": {"base_url": rest_server.base_url, "username": "u"},
            "secret_id": None,
        }
        | {"auth_method": "key_pair"},  # method not supported by rest.http
        {**base, "auth_method": "none", "secret_id": "00000000-0000-0000-0000-000000000001"},
    ]
    for body in cases:
        r = client.post(
            f"{A}/connections", json={"scope": {}, "access_mode": "read", **body}, headers=h
        )
        assert r.status_code in (404, 422), (body, r.text)
        assert "hunter2" not in r.text
    errs = client.post(
        f"{A}/connections",
        headers=h,
        json={**base, "scope": {}, "config": {"base_url": "https://x", "password": "p-w"}},
    ).json()
    assert errs["code"] == "validation_error" and errs["errors"][0]["loc"][:2] == ["body", "config"]
    # an extra top-level field is rejected too (no smuggled tenant ids)
    assert (
        client.post(
            f"{A}/connections", headers=h, json={**base, "scope": {}, "tenant_id": "x"}
        ).status_code
        == 422
    )


def test_secrets_are_attached_by_reference_same_tenant_and_with_secret_use(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    ha = login(client, world.passwords, ADMIN)
    hb = login(client, world.passwords, ADMIN_B)
    foreign = secret(client, hb, "b-secret", "b-secret-value-1", org=B)
    ha = login(client, world.passwords, ADMIN)
    r = client.post(
        f"{A}/connections",
        headers=ha,
        json={**rest_body(rest_server, foreign), "scope": {}, "access_mode": "read"},
    )
    assert r.status_code in (403, 404)  # B's secret is not usable by A
    ha = login(client, world.passwords, ADMIN)
    mine = secret(client, ha, "a-secret", REST_TOKEN)
    conn = connection(client, ha, rest_body(rest_server, mine))
    # detaching and replacing the secret through PATCH never reveals values
    row = _row(client, ha, conn["id"])
    patched = client.patch(
        f"{A}/connections/{conn['id']}",
        headers=ha,
        json={"secret_id": None, "auth_method": "none", "row_version": row["row_version"]},
    )
    assert patched.status_code == 200 and patched.json()["secret_set"] is False
    # the database itself refuses cross-tenant references even if a service were buggy
    foreign_row = owner_session.execute(
        text("SELECT id FROM shifde.secrets WHERE name = 'b-secret'")
    ).scalar_one()
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="secret must belong"):
        owner_session.execute(
            text("UPDATE shifde.connections SET secret_ref_id = :s WHERE id = :c"),
            {"s": foreign_row, "c": conn["id"]},
        )
    owner_session.rollback()


# ================================================================== acceptance 6: status rules
def test_activation_needs_a_fresh_passing_test_and_re_enable_needs_another(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    cid = connection(client, h, rest_body(rest_server, secret(client, h, "t", REST_TOKEN)))["id"]
    url = f"{A}/connections/{cid}"

    def set_status(to: str) -> Any:
        return client.post(
            f"{url}/status",
            json={"status": to, "row_version": _row(client, h, cid)["row_version"]},
            headers=h,
        )

    assert set_status("active").status_code == 409  # never tested
    assert (
        client.post(f"{url}/invoke", json={"method": "GET", "path": "/echo"}, headers=h).status_code
        == 409
    )
    activated = activate(client, h, cid)
    assert activated["status"] == "active" and activated["last_tested_at"] is not None
    assert (
        client.post(f"{url}/invoke", json={"method": "GET", "path": "/echo"}, headers=h).status_code
        == 200
    )
    # a stale test (older than 24 h) cannot activate
    assert set_status("disabled").status_code == 200
    disabled = client.post(f"{url}/invoke", json={"method": "GET", "path": "/echo"}, headers=h)
    assert disabled.status_code == 409 and disabled.json()["code"] == "connection_disabled"
    assert set_status("active").status_code == 409  # re-enable requires a new test
    activate(client, h, cid)
    owner_session.execute(
        text(
            "UPDATE shifde.connection_tests SET finished_at = now() - interval '25 hours' WHERE connection_id = :c"
        ),
        {"c": cid},
    )
    owner_session.commit()
    assert set_status("disabled").status_code == 200
    owner_session.execute(  # even if last_tested_at were restored, the stored test is stale
        text("UPDATE shifde.connections SET last_tested_at = now() WHERE id = :c"), {"c": cid}
    )
    owner_session.commit()
    assert set_status("active").status_code == 409
    # editing the definition of an active connection sends it back to draft until re-tested
    activate(client, h, cid)
    row = _row(client, h, cid)
    edited = client.patch(
        url,
        headers=h,
        json={"config": {**row["config"], "health_path": "/"}, "row_version": row["row_version"]},
    )
    assert edited.status_code == 200 and edited.json()["status"] == "draft"
    assert edited.json()["last_tested_at"] is None
    assert (
        client.post(f"{url}/invoke", json={"method": "GET", "path": "/echo"}, headers=h).status_code
        == 409
    )
    # invalid / terminal transitions
    activate(client, h, cid)
    assert set_status("retired").status_code == 200
    assert set_status("active").status_code == 409
    assert client.patch(url, json={"name": "renamed"}, headers=h).status_code == 409
    assert client.post(f"{url}/status", json={"status": "bogus"}, headers=h).status_code == 422
    stale = client.post(f"{url}/status", json={"status": "retired", "row_version": 1}, headers=h)
    assert stale.status_code in (200, 409)  # same status is a no-op; never a 500


def test_failed_test_on_active_connection_degrades_it_and_a_pass_restores_it(
    client: TestClient, world: World, rest_server: RestTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    s = secret(client, h, "t", REST_TOKEN)
    cid = connection(client, h, rest_body(rest_server, s))["id"]
    activate(client, h, cid)
    client.post(f"{A}/secrets/{s}/rotate", json={"value": "rotated-token-777"}, headers=h)
    assert client.post(f"{A}/connections/{cid}/test", headers=h).json()["outcome"] == "fail"
    assert _row(client, h, cid)["status"] == "degraded"
    client.post(f"{A}/secrets/{s}/rotate", json={"value": REST_TOKEN}, headers=h)
    assert client.post(f"{A}/connections/{cid}/test", headers=h).json()["outcome"] == "pass"
    assert _row(client, h, cid)["status"] == "active"


# ================================================================== acceptance 7: planned adapters
def test_planned_adapters_cannot_be_tested_discovered_or_invoked(
    client: TestClient, world: World
) -> None:
    h = login(client, world.passwords, ADMIN)
    for key in ("databricks", "snowflake", "fabric"):
        cfg = {
            "databricks": {"host": "h"},
            "snowflake": {"account": "a"},
            "fabric": {"endpoint": "e"},
        }[key]
        c = connection(
            client,
            h,
            {
                "name": f"{key}-conn",
                "connection_type": "data_platform",
                "adapter_key": key,
                "config": cfg,
            },
        )
        for path, body in (("test", {}), ("discover", {}), ("invoke", {"sql": "SELECT 1"})):
            r = client.post(f"{A}/connections/{c['id']}/{path}", json=body, headers=h)
            assert r.status_code == 422 and r.json()["code"] == "adapter_not_implemented", (
                key,
                path,
            )
        # it can never be activated: activation needs a passing test
        r = client.post(f"{A}/connections/{c['id']}/status", json={"status": "active"}, headers=h)
        assert r.status_code == 409
    adapters = {a["key"]: a for a in client.get("/api/v1/adapters", headers=h).json()["items"]}
    for key in ("databricks", "snowflake", "fabric"):
        assert {c["maturity"] for c in adapters[key]["capabilities"]} == {"planned"}
    r = client.post(
        f"{A}/model-providers",
        headers=h,
        json={"name": "p", "adapter_key": "databricks", "connection_id": c["id"]},
    )
    assert r.status_code == 422


# ================================================================== acceptance 5: authorization
def _login_new(
    client: TestClient, admin_h: dict[str, str], world: World, email: str, roles: list[Any]
) -> dict[str, str]:
    new_user(client, admin_h, "demo-a", email, "Passw0rd!-test-1", [str(r) for r in roles])
    return login(client, {email: "Passw0rd!-test-1"}, email)


def test_unauthorized_use_is_prevented_and_tenants_are_isolated(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    ha = login(client, world.passwords, ADMIN)
    cid = connection(client, ha, rest_body(rest_server, secret(client, ha, "t", REST_TOKEN)))["id"]
    activate(client, ha, cid)
    other = connection(
        client, ha, rest_body(rest_server, secret(client, ha, "t-other", REST_TOKEN), "other-api")
    )["id"]
    activate(client, ha, other)
    invoke = {"method": "GET", "path": "/echo"}

    # viewer: connection:read only
    hv = login(client, world.passwords, VIEWER)
    assert client.get(f"{A}/connections/{cid}", headers=hv).status_code == 200
    assert client.post(f"{A}/connections/{cid}/invoke", json=invoke, headers=hv).status_code == 403
    assert client.post(f"{A}/connections/{cid}/test", headers=hv).status_code == 403
    assert client.post(f"{A}/connections/{cid}/discover", headers=hv).status_code == 403
    assert client.post(
        f"{A}/connections", json=rest_body(rest_server, None, "v"), headers=hv
    ).status_code in (403, 422)
    assert client.patch(f"{A}/connections/{cid}", json={"name": "x"}, headers=hv).status_code == 403
    assert (
        client.post(
            f"{A}/connections/{cid}/status", json={"status": "disabled"}, headers=hv
        ).status_code
        == 403
    )
    assert client.get(f"{A}/egress-policies", headers=hv).status_code == 200  # picker data
    assert client.post(f"{A}/egress-policies", json={"name": "n"}, headers=hv).status_code == 403

    # other tenant: 404 for every verb (no existence oracle)
    hb = login(client, world.passwords, ADMIN_B)
    for method, path, body in (
        ("get", f"{B}/connections/{cid}", None),
        ("patch", f"{B}/connections/{cid}", {"name": "n"}),
        ("post", f"{B}/connections/{cid}/invoke", invoke),
        ("post", f"{B}/connections/{cid}/test", {}),
        ("post", f"{B}/connections/{cid}/discover", {}),
        ("get", f"{B}/connections/{cid}/tests", None),
        ("post", f"{B}/connections/{cid}/status", {"status": "disabled"}),
    ):
        r = getattr(client, method)(
            path, headers=hb, **({"json": body} if body is not None else {})
        )
        assert r.status_code == 404, (method, path)
    assert client.get(f"{B}/connections", headers=hb).json()["items"] == []
    # ... and a path trick through another org slug is refused too
    assert client.get(f"{A}/connections/{cid}", headers=hb).status_code == 404

    # resource-level binding: connection:use on exactly one connection
    ha = login(client, world.passwords, ADMIN)
    role = post(
        client,
        f"{A}/roles",
        ha,
        {"key": "one-conn", "name": "one", "permissions": ["connection:use"]},
    )
    uid = new_user(client, ha, "demo-a", "binder@demo-a.local", "Passw0rd!-test-1")
    ha = login(client, world.passwords, ADMIN)
    bind = client.post(
        f"{A}/role-bindings",
        headers=ha,
        json={
            "subject_type": "user",
            "subject_id": uid,
            "role_id": role["id"],
            "scope": {"type": "resource", "id": cid, "resource_type": "connection"},
        },
    )
    assert bind.status_code == 201, bind.text
    hu = login(client, {"binder@demo-a.local": "Passw0rd!-test-1"}, "binder@demo-a.local")
    ok = client.post(f"{A}/connections/{cid}/invoke", json=invoke, headers=hu)
    assert ok.status_code == 200 and ok.json()["result"]["status"] == 200
    for target in (other,):  # a sibling connection: not granted (404: not even visible)
        assert client.post(
            f"{A}/connections/{target}/invoke", json=invoke, headers=hu
        ).status_code in (403, 404)
    assert client.post(f"{A}/connections/{cid}/test", headers=hu).status_code in (
        403,
        404,
    )  # only `use`
    assert (
        client.post(
            f"{A}/connections/{cid}/invoke", json={**invoke, "write": True}, headers=hu
        ).status_code
        == 403
    )
    assert client.get(f"{A}/connections", headers=hu).status_code == 403  # no list permission

    # developer: use + test, but no manage
    hd = _login_new(
        client,
        login(client, world.passwords, ADMIN),
        world,
        "dev@demo-a.local",
        [system_role(owner_session, "developer")],
    )
    assert client.post(f"{A}/connections/{cid}/invoke", json=invoke, headers=hd).status_code == 200
    assert client.post(f"{A}/connections/{cid}/test", headers=hd).status_code == 200
    assert client.patch(f"{A}/connections/{cid}", json={"name": "d"}, headers=hd).status_code == 403
    assert (
        client.post(
            f"{A}/connections",
            headers=hd,
            json={**rest_body(rest_server, None, "dev-conn"), "scope": {}},
        ).status_code
        == 403
    )
    assert client.post(f"{A}/egress-policies", json={"name": "n"}, headers=hd).status_code == 403


def test_workspace_scoped_connections_follow_workspace_grants(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    ha = login(client, world.passwords, ADMIN)
    ws_conn = connection(
        client, ha, {**rest_body(rest_server, None, "ws-api"), "scope": {"workspace": "main"}}
    )
    prj_conn = connection(
        client,
        ha,
        {
            **rest_body(rest_server, None, "prj-api"),
            "scope": {"workspace": "main", "project": "sample"},
        },
    )
    assert ws_conn["scope"] == {"workspace": "main", "project": None}
    assert prj_conn["scope"] == {"workspace": "main", "project": "sample"}
    tenant_wide = connection(client, ha, rest_body(rest_server, None, "wide-api"))
    by_ws = client.get(f"{A}/connections?workspace=main", headers=ha).json()["items"]
    assert {c["name"] for c in by_ws} == {"ws-api", "prj-api"}
    by_prj = client.get(f"{A}/connections?workspace=main&project=sample", headers=ha).json()[
        "items"
    ]
    assert [c["name"] for c in by_prj] == ["prj-api"]
    assert client.get(f"{A}/connections?project=sample", headers=ha).status_code == 422
    assert (
        client.post(
            f"{A}/connections",
            headers=ha,
            json={**rest_body(rest_server, None, "bad-scope"), "scope": {"workspace": "nope"}},
        ).status_code
        == 404
    )
    # a project-only grant sees only that project's connections
    role = post(
        client,
        f"{A}/roles",
        ha,
        {
            "key": "prj-reader",
            "name": "p",
            "permissions": ["connection:read", "workspace:read", "project:read"],
        },
    )
    uid = new_user(client, ha, "demo-a", "prj@demo-a.local", "Passw0rd!-test-1")
    ha = login(client, world.passwords, ADMIN)
    pid = owner_session.execute(text("SELECT id FROM shifde.projects LIMIT 1")).scalar_one()
    assert (
        client.post(
            f"{A}/role-bindings",
            headers=ha,
            json={
                "subject_type": "user",
                "subject_id": uid,
                "role_id": role["id"],
                "scope": {"type": "project", "id": str(pid)},
            },
        ).status_code
        == 201
    )
    hp = login(client, {"prj@demo-a.local": "Passw0rd!-test-1"}, "prj@demo-a.local")
    seen = client.get(f"{A}/connections", headers=hp).json()["items"]
    assert {c["name"] for c in seen} == {"prj-api"}
    assert client.get(f"{A}/connections/{tenant_wide['id']}", headers=hp).status_code in (403, 404)
    assert client.get(f"{A}/connections/{prj_conn['id']}", headers=hp).status_code == 200


# ================================================================== egress policies
def test_egress_policy_api_shapes_and_enforcement(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    pol = post(
        client,
        f"{A}/egress-policies",
        h,
        {
            "name": "https-only",
            "allowed_schemes": ["https"],
            "allowed_ports": [443],
            "allowed_hosts": ["*.example.com"],
            "allowed_cidrs": ["10.0.0.0/8"],
            "allow_private": False,
            "allow_redirects": False,
            "max_response_bytes": 1000,
            "timeout_seconds": 3,
        },
    )
    assert set(pol) == {
        "id",
        "name",
        "is_default",
        "allowed_schemes",
        "allowed_ports",
        "allowed_hosts",
        "allowed_cidrs",
        "allow_private",
        "allow_redirects",
        "max_response_bytes",
        "timeout_seconds",
        "row_version",
    }
    assert pol["allowed_cidrs"] == ["10.0.0.0/8"] and pol["timeout_seconds"] == 3
    assert client.get(f"{A}/egress-policies", headers=h).json()["items"][0]["id"] == pol["id"]
    assert client.get(f"{A}/egress-policies/{pol['id']}", headers=h).status_code == 200
    for bad in (
        {"allowed_schemes": ["gopher"]},
        {"allowed_ports": [0]},
        {"allowed_cidrs": ["nope"]},
        {"allowed_hosts": ["bad host!"]},
        {"timeout_seconds": 0},
        {"max_response_bytes": -1},
    ):
        assert (
            client.post(f"{A}/egress-policies", json={"name": "b", **bad}, headers=h).status_code
            == 422
        ), bad
    assert (
        client.post(f"{A}/egress-policies", json={"name": "https-only"}, headers=h).status_code
        == 409
    )
    patched = client.patch(
        f"{A}/egress-policies/{pol['id']}",
        headers=h,
        json={"allow_private": True, "row_version": pol["row_version"]},
    )
    assert patched.status_code == 200 and patched.json()["allow_private"] is True
    assert (
        client.patch(
            f"{A}/egress-policies/{pol['id']}",
            json={"timeout_seconds": 5, "row_version": 1},
            headers=h,
        ).status_code
        == 409
    )
    hb = login(client, world.passwords, ADMIN_B)
    assert client.get(f"{B}/egress-policies/{pol['id']}", headers=hb).status_code == 404
    assert client.get(f"{B}/egress-policies", headers=hb).json()["items"] == []
    h = login(client, world.passwords, ADMIN)

    # a connection policy narrows what the installation default (loopback in test) allows
    cid = connection(client, h, rest_body(rest_server, secret(client, h, "t", REST_TOKEN)))["id"]
    activate(client, h, cid)
    invoke = {"method": "GET", "path": "/echo"}
    assert client.post(f"{A}/connections/{cid}/invoke", json=invoke, headers=h).status_code == 200
    narrow = post(
        client,
        f"{A}/egress-policies",
        h,
        {
            "name": "no-loopback",
            "allowed_schemes": ["http"],
            "allowed_ports": [],
            "allowed_cidrs": [],
        },
    )
    row = _row(client, h, cid)
    assert (
        client.patch(
            f"{A}/connections/{cid}",
            headers=h,
            json={"egress_policy_id": narrow["id"], "row_version": row["row_version"]},
        ).status_code
        == 200
    )
    test = client.post(f"{A}/connections/{cid}/test", headers=h).json()
    assert test["outcome"] == "fail" and test["error_class"] == "egress"
    # tenant default layer: loopback allowed again by CIDR, but a tenant default can only narrow
    loop = post(
        client,
        f"{A}/egress-policies",
        h,
        {
            "name": "loop",
            "allowed_schemes": ["http"],
            "allowed_ports": [],
            "allowed_cidrs": ["127.0.0.0/8"],
        },
    )
    row = _row(client, h, cid)
    client.patch(
        f"{A}/connections/{cid}",
        headers=h,
        json={"egress_policy_id": loop["id"], "row_version": row["row_version"]},
    )
    activate(client, h, cid)
    assert client.post(f"{A}/connections/{cid}/invoke", json=invoke, headers=h).status_code == 200
    deny_default = post(
        client,
        f"{A}/egress-policies",
        h,
        {"name": "tenant-default", "is_default": True, "allowed_schemes": ["https"]},
    )
    assert deny_default["is_default"] is True
    blocked = client.post(f"{A}/connections/{cid}/invoke", json=invoke, headers=h)
    assert blocked.status_code == 422 and blocked.json()["code"] == "egress_denied"
    other_default = post(client, f"{A}/egress-policies", h, {"name": "d2", "is_default": True})
    assert (
        other_default["is_default"]
        and not client.get(f"{A}/egress-policies/{deny_default['id']}", headers=h).json()[
            "is_default"
        ]
    )
    audit = owner_session.execute(
        text("SELECT count(*) FROM shifde.audit_events WHERE action LIKE 'egress.policy.%'")
    ).scalar_one()
    assert audit >= 5
    # dangling references never widen anything
    assert (
        client.post(
            f"{A}/connections",
            headers=h,
            json={
                **rest_body(rest_server, None, "dangling"),
                "scope": {},
                "egress_policy_id": "00000000-0000-0000-0000-000000000009",
            },
        ).status_code
        == 404
    )


def test_platform_egress_default_is_platform_admin_only(
    client: TestClient, world: World, owner_session: Session
) -> None:
    ha = login(client, world.passwords, ADMIN)
    assert client.get("/api/v1/platform/egress-default", headers=ha).status_code == 403
    assert (
        client.put(
            "/api/v1/platform/egress-default", json={"allowed_ports": [443]}, headers=ha
        ).status_code
        == 403
    )
    hp = login(client, world.passwords, PLATFORM)
    cur = client.get("/api/v1/platform/egress-default", headers=hp)
    assert cur.status_code == 200
    body = cur.json()
    assert body["allowed_schemes"] == ["https"] and body["allowed_ports"] == [443]
    assert body["allow_private"] is False and body["allow_redirects"] is False
    assert set(body) >= {
        "id",
        "name",
        "allowed_schemes",
        "allowed_ports",
        "allowed_hosts",
        "allowed_cidrs",
        "allow_private",
        "allow_redirects",
        "max_response_bytes",
        "timeout_seconds",
        "row_version",
    }
    put = client.put(
        "/api/v1/platform/egress-default",
        headers=hp,
        json={
            "allowed_ports": [443, 5432],
            "allowed_schemes": ["https", "postgresql"],
            "row_version": body["row_version"],
        },
    )
    assert put.status_code == 200 and put.json()["allowed_ports"] == [443, 5432]
    assert (
        client.put(
            "/api/v1/platform/egress-default", json={"allowed_ports": [1]}, headers=hp
        ).status_code
        == 200
    )
    stale = client.put(
        "/api/v1/platform/egress-default", json={"allowed_ports": [2], "row_version": 1}, headers=hp
    )
    assert stale.status_code == 409
    assert (
        client.put(
            "/api/v1/platform/egress-default", json={"allowed_schemes": ["ftp"]}, headers=hp
        ).status_code
        == 422
    )
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = 'egress.default.updated'")
        ).scalar_one()
        == 2
    )
    # restore the strict seed for other tests
    owner_session.execute(
        text(
            "UPDATE shifde.egress_policies SET allowed_ports='{443}', allowed_schemes='{https}' WHERE tenant_id IS NULL"
        )
    )
    owner_session.commit()


# ================================================================== server-side use (open_for_use)
def _service(client: TestClient, owner: Session, email: str) -> tuple[ConnectionService, Any]:
    app_db = client.app.state.db  # type: ignore[attr-defined]
    session = app_db.session()
    row = owner.execute(
        text(
            "SELECT t.id, u.id FROM shifde.tenants t, shifde.users u "
            "WHERE t.slug = 'demo-a' AND u.primary_email = :e"
        ),
        {"e": email},
    ).one()
    tenant_id, user_id = row
    dbmod.set_tenant(session, tenant_id)
    principal = Principal(user_id=user_id, email=email, display_name=email)
    ctx = RequestContext(request_id="t-open-for-use", principal=principal, tenant_id=tenant_id)
    scope = TenantScope(
        session,
        ctx,
        principal,
        tenant_id,
        "demo-a",
        "Demo A",
        "active",
        new_pdp(session, principal, tenant_id),
    )
    return ConnectionService(scope, client.app.state.integration), session  # type: ignore[attr-defined]


def test_open_for_use_enforces_pdp_status_and_audits(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    ha = login(client, world.passwords, ADMIN)
    cid = connection(client, ha, rest_body(rest_server, secret(client, ha, "t", REST_TOKEN)))["id"]
    svc, session = _service(client, owner_session, ADMIN)
    with pytest.raises(ConnectionDisabled), svc.open_for_use(UUID(cid), "pipeline:test"):  # draft
        pass
    activate(client, ha, cid)
    with svc.open_for_use(UUID(cid), "pipeline:test") as s:
        r = s.invoke({"method": "GET", "path": "/echo", "query": {"x": "1"}})
        assert r.result["status"] == 200  # the secret was resolved server-side
    with (
        pytest.raises(Forbidden),  # write needs read_write access
        svc.open_for_use(UUID(cid), "pipeline:test", write=True),
    ):
        pass
    session.close()

    vsvc, vsession = _service(client, owner_session, VIEWER)  # read-only principal
    with pytest.raises(Forbidden), vsvc.open_for_use(UUID(cid), "pipeline:test"):
        pass
    vsession.close()
    with (
        pytest.raises(NotFound),
        svc.open_for_use(UUID("00000000-0000-0000-0000-000000000042"), "x"),
    ):
        pass
    used = (
        owner_session.execute(
            text("SELECT details FROM shifde.audit_events WHERE action = 'connection.used'")
        )
        .scalars()
        .all()
    )
    assert [u["purpose"] for u in used] == ["pipeline:test"]
    log = (
        owner_session.execute(text("SELECT purpose FROM shifde.secret_access_log")).scalars().all()
    )
    assert any(p.endswith(":pipeline:test") for p in log)
    # disabling stops server-side use immediately
    row = _row(client, ha, cid)
    client.post(
        f"{A}/connections/{cid}/status",
        json={"status": "disabled", "row_version": row["row_version"]},
        headers=ha,
    )
    svc2, session2 = _service(client, owner_session, ADMIN)
    with pytest.raises(ConnectionDisabled), svc2.open_for_use(UUID(cid), "pipeline:test"):
        pass
    session2.close()


def test_audit_records_cover_the_connection_lifecycle(
    client: TestClient, world: World, rest_server: RestTestServer, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    cid = connection(client, h, rest_body(rest_server, secret(client, h, "t", REST_TOKEN)))["id"]
    activate(client, h, cid)
    client.post(
        f"{A}/connections/{cid}/invoke",
        json={"method": "GET", "path": "/echo", "query": {"k": "private-value-1"}},
        headers=h,
    )
    client.post(
        f"{A}/connections/{cid}/invoke",
        json={"method": "GET", "path": "http://evil.test/"},
        headers=h,
    )
    rows = owner_session.execute(
        text(
            "SELECT action, outcome, details::text FROM shifde.audit_events WHERE action LIKE 'connection.%' ORDER BY id"
        )
    ).all()
    actions = [(r[0], r[1]) for r in rows]
    assert ("connection.created", "success") in actions and (
        "connection.tested",
        "success",
    ) in actions
    assert ("connection.status_changed", "success") in actions and (
        "connection.invoked",
        "success",
    ) in actions
    assert ("connection.invoked", "failure") in actions
    blob = " ".join(r[2] for r in rows)
    assert "private-value-1" not in blob and REST_TOKEN not in blob  # parameters are redacted
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = 'authz.denied'")
        ).scalar_one()
        == 0
    )
    hv = login(client, world.passwords, VIEWER)
    client.post(f"{A}/connections/{cid}/invoke", json={"method": "GET", "path": "/"}, headers=hv)
    owner_session.rollback()
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE action = 'authz.denied'")
        ).scalar_one()
        >= 1
    )
    _ = PLATFORM


# ================================================================== vendor status gates new use
def _set_vendor_status(owner_session: Session, key: str, status: str) -> None:
    owner_session.rollback()
    owner_session.execute(
        text("UPDATE shifde.vendors SET status = :s WHERE tenant_id IS NULL AND key = :k"),
        {"s": status, "k": key},
    )
    owner_session.commit()
