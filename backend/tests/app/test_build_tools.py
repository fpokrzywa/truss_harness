"""P4 tools (contract section 3, verification item 4): bindings, publish-time validation, test
invocation through P3 connections, and the side-effect guard."""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import McpTestServer, RestTestServer
from tests._support.conntests import discovery_of
from tests._support.fullapp import World
from tests._support.p2util import login, post
from tests._support.p3util import (
    ADMIN,
    ADMIN_B,
    MCP_TOKEN,
    REST_TOKEN,
    VIEWER,
    A,
    B,
    activate,
    connection,
    pg_body,
    rest_body,
    secret,
)
from tests._support.p4util import (
    art,
    as_user,
    builtin_tool_spec,
    codes,
    edit,
    make_user,
    publish,
    tenant_scope,
)

ECHO_OUT = {
    "type": "object",
    "required": ["status", "body"],
    "properties": {"status": {"type": "integer"}, "body": {"type": "object"}},
}


def rest_conn(
    client: TestClient,
    h: dict[str, str],
    server: RestTestServer,
    name: str = "rest-api",
    access: str = "read",
    org: str = A,
    activate_it: bool = True,
) -> str:
    sid = secret(client, h, f"tok-{name}", REST_TOKEN, org)
    body = {**rest_body(server, sid, name), "access_mode": access}
    conn = connection(client, h, body, org)
    if activate_it:
        activate(client, h, conn["id"], org)
    return str(conn["id"])


def rest_spec(cid: str, method: str = "GET", side: str = "read", **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "executor": "rest.http",
        "binding": {
            "connection_id": cid,
            "method": method,
            "path_template": "/echo",
            "query_template": {"name": "{name}"},
        },
        "input_schema": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        },
        "output_schema": ECHO_OUT,
        "side_effect_class": side,
        "required_permissions": ["connection:use"],
        "invocation_policy": {"max_calls_per_run": 3},
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    spec.update(over)
    return spec


def validate(client: TestClient, h: dict[str, str], tid: str) -> dict[str, Any]:
    r = client.post(f"{A}/tools/{tid}/draft/validate", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def run_tool(
    client: TestClient,
    h: dict[str, str],
    tid: str,
    args: dict[str, Any],
    version: str = "draft",
    ack: bool = False,
    status: int = 200,
) -> Any:
    body: dict[str, Any] = {"version_id": version, "args": args}
    if ack:
        body["acknowledge_side_effects"] = True
    r = client.post(f"{A}/tools/{tid}/test", json=body, headers=h)
    assert r.status_code == status, f"{r.status_code} {r.text}"
    return r.json()


# ================================================================== catalog + builtin
def test_executor_catalog_and_builtin_tool(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    cat = client.get("/api/v1/tool-executors", headers=h)
    assert cat.status_code == 200
    items = {i["key"]: i for i in cat.json()["items"]}
    assert set(items) == {"builtin.function", "rest.http", "mcp.tool", "connection.sql"}
    assert items["builtin.function"]["maturity"] == "validated"
    assert items["rest.http"]["maturity"] in ("experimental", "validated")
    fns = {f["key"]: f for f in items["builtin.function"]["functions"]}
    assert {"util.echo", "text.word_count", "math.sum", "json.pick", "time.now_utc"} <= set(fns)
    assert fns["util.echo"]["input_schema"]["required"] == ["text"]
    assert items["rest.http"]["binding_schema"]["required"] == [
        "connection_id",
        "method",
        "path_template",
    ]
    assert "executors" not in cat.json() and all("function_key" not in f for f in fns.values())
    client.cookies.clear()
    assert client.get("/api/v1/tool-executors").status_code == 401  # authenticated only
    h = login(client, world.passwords, ADMIN)

    scope = tenant_scope(owner_session)
    t = art(client, h, "tools", "echo-tool", builtin_tool_spec(), scope)
    assert validate(client, h, t["id"])["valid"] is True
    out = run_tool(client, h, t["id"], {"text": "hi"})
    assert out == {**out, "output": {"text": "hi"}, "valid_output": True, "attempts": 1}
    assert out["duration_ms"] >= 0
    v1 = publish(client, h, "tools", t["id"])
    assert run_tool(client, h, t["id"], {"text": "x"}, version=v1["id"])["output"] == {"text": "x"}

    edit(client, h, "tools", t["id"], builtin_tool_spec(), 0)  # a new draft after publishing
    bad = client.post(
        f"{A}/tools/{t['id']}/test",
        json={"version_id": "draft", "args": {"text": 5, "extra": 1}},
        headers=h,
    )
    assert bad.status_code == 422
    assert {tuple(e["loc"]) for e in bad.json()["errors"]} >= {("args", "text"), ("args",)}

    other = art(client, h, "tools", "counter", builtin_tool_spec("text.word_count"), scope)
    assert run_tool(client, h, other["id"], {"text": "a b c"})["output"] == {
        "words": 3,
        "characters": 5,
    }
    ssum = art(client, h, "tools", "summer", builtin_tool_spec("math.sum"), scope)
    assert run_tool(client, h, ssum["id"], {"values": [1, 2.5]})["output"] == {"sum": 3.5}
    # the output is validated against the declared schema
    strict = builtin_tool_spec("util.echo", output_schema={"type": "object", "required": ["nope"]})
    s = art(client, h, "tools", "strict-out", strict, scope)
    assert run_tool(client, h, s["id"], {"text": "x"})["valid_output"] is False


def test_tool_spec_validation_codes(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    t = art(client, h, "tools", "validate-me", builtin_tool_spec(), scope)
    pid = t["id"]

    def check(spec: dict[str, Any], rv: int) -> set[str]:
        edit(client, h, "tools", pid, spec, rv)
        return {e["code"] for e in validate(client, h, pid)["errors"]}

    base = builtin_tool_spec()
    assert check({**base, "binding": {"function_key": "nope.fn"}}, 1) == {"unknown_function"}
    assert check({**base, "executor": "wat"}, 2) >= {"invalid_value"}
    assert check({**base, "input_schema": {"type": "array"}}, 3) == {"schema_must_be_object"}
    assert check({**base, "input_schema": {"type": "object", "properties": 5}}, 4) == {
        "invalid_schema"
    }
    assert check({**base, "output_schema": {"$ref": "http://169.254.169.254/x"}}, 5) == {
        "remote_ref_forbidden"
    }
    assert check({**base, "timeout_s": 0}, 6) == {"invalid_value"}
    assert check({**base, "timeout_s": 10_000}, 7) == {"invalid_value"}
    assert check({**base, "audit_level": "all"}, 8) == {"invalid_value"}
    assert check({**base, "side_effect_class": "writes"}, 9) >= {"invalid_value"}
    assert check({**base, "required_permissions": ["Not A Key"]}, 10) == {"invalid_value"}
    assert check({**base, "retry": {"max_attempts": 99, "backoff_s": 0}}, 11) == {"invalid_value"}
    assert check({**base, "invocation_policy": {"allowed_agent_ids": ["x"]}}, 12) == {
        "invalid_value"
    }
    # C1 side-effect rules
    bad_retry = {
        **base,
        "side_effect_class": "write_non_idempotent",
        "retry": {"max_attempts": 2, "backoff_s": 0},
    }
    assert check(bad_retry, 13) == {"retry_forbidden"}
    destructive = {
        **base,
        "side_effect_class": "destructive",
        "retry": {"max_attempts": 3, "backoff_s": 1},
    }
    assert check(destructive, 14) == {"retry_forbidden"}
    assert check({**base, "side_effect_class": "write_idempotent"}, 15) == {"idempotency_required"}
    ok = {
        **base,
        "side_effect_class": "write_idempotent",
        "idempotency": "key_supported",
        "retry": {"max_attempts": 3, "backoff_s": 0},
    }
    assert check(ok, 16) == set()
    w = validate(client, h, pid)["warnings"]
    assert w == []
    edit(client, h, "tools", pid, {**base, "side_effect_class": "destructive"}, 17)
    assert [x["code"] for x in validate(client, h, pid)["warnings"]] == ["destructive_tool"]


# ================================================================== rest.http
def test_rest_tool_runs_through_the_connection_and_guards_side_effects(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    ro = rest_conn(client, h, rest_server, "ro-api", "read")
    rw = rest_conn(client, h, rest_server, "rw-api", "read_write")

    t = art(client, h, "tools", "lookup", rest_spec(ro), scope)
    assert validate(client, h, t["id"])["valid"] is True
    before = len(rest_server.requests)
    out = run_tool(client, h, t["id"], {"name": "a b&c"})
    assert out["valid_output"] is True and out["output"]["status"] == 200
    assert out["output"]["body"]["query"] == {"name": "a b&c"}  # a value, not a query fragment
    assert len(rest_server.requests) == before + 1 and rest_server.requests[-1]["authenticated"]

    # path placeholders are percent-encoded values
    spec = rest_spec(ro)
    spec["binding"]["path_template"] = "/{segment}"
    spec["input_schema"]["properties"]["segment"] = {"type": "string"}
    spec["input_schema"]["required"] = ["name", "segment"]
    edit(client, h, "tools", t["id"], spec, 1)
    assert validate(client, h, t["id"])["valid"] is True
    res = run_tool(client, h, t["id"], {"name": "x", "segment": "a b/c?d"})
    assert res["output"]["status"] == 404
    assert rest_server.requests[-1]["path"] == "/a%20b%2Fc%3Fd"  # exactly one encoded segment

    dots = client.post(
        f"{A}/tools/{t['id']}/test",
        json={"version_id": "draft", "args": {"name": "x", "segment": ".."}},
        headers=h,
    )
    assert dots.status_code == 422 and "relative path segment" in dots.json()["detail"]

    # ---- validation against the binding
    def codes_of(spec: dict[str, Any], rv: int) -> set[str]:
        edit(client, h, "tools", t["id"], spec, rv)
        return {e["code"] for e in validate(client, h, t["id"])["errors"]}

    post_read = rest_spec(ro, "POST", "read")
    assert codes_of(post_read, 2) == {"write_method_requires_write_class"}
    post_ro = rest_spec(ro, "POST", "write_non_idempotent")
    assert codes_of(post_ro, 3) == {"connection_read_only"}
    unknown_arg = rest_spec(ro)
    unknown_arg["binding"]["query_template"] = {"x": "{ghost}"}
    assert codes_of(unknown_arg, 4) == {"unknown_argument"}
    bad_path = rest_spec(ro)
    bad_path["binding"]["path_template"] = "http://evil.example/x"
    assert codes_of(bad_path, 5) == {"invalid_path"}
    no_conn = rest_spec(ro)
    no_conn["binding"]["connection_id"] = "00000000-0000-0000-0000-000000000000"
    assert codes_of(no_conn, 6) == {"connection_not_found"}
    inactive = rest_conn(client, h, rest_server, "idle-api", activate_it=False)
    assert codes_of(rest_spec(inactive), 7) == {"connection_not_active"}
    wrong_type = rest_spec(ro)
    wrong_type["executor"] = "mcp.tool"
    wrong_type["binding"] = {"connection_id": ro, "tool_name": "x"}
    assert codes_of(wrong_type, 8) == {"wrong_connection_type"}

    # cross-tenant: another organization's connection does not exist for us
    hb = login(client, world.passwords, ADMIN_B)
    other = rest_conn(client, hb, rest_server, "b-api", org=B)
    h = login(client, world.passwords, ADMIN)
    assert codes_of(rest_spec(other), 9) == {"connection_not_found"}
    edit(client, h, "tools", t["id"], rest_spec(ro), 10)
    relative = rest_spec(ro)
    relative["binding"]["path_template"] = "echo"
    assert codes_of(relative, 11) == {"invalid_path"}  # must start with '/'
    edit(client, h, "tools", t["id"], rest_spec(ro), 12)

    # ---- the side-effect guard (C1): a write needs the acknowledgement AND tool:test_side_effects
    w = art(
        client,
        h,
        "tools",
        "create-note",
        rest_spec(
            rw,
            "POST",
            "write_non_idempotent",
            binding={
                "connection_id": rw,
                "method": "POST",
                "path_template": "/echo",
                "body_template": {"note": "{name}", "n": 1},
            },
        ),
        scope,
    )
    assert validate(client, h, w["id"])["valid"] is True
    posts = lambda: [r for r in rest_server.requests if r["method"] == "POST"]  # noqa: E731
    n0 = len(posts())
    refused = run_tool(client, h, w["id"], {"name": "hello"}, status=422)
    assert refused["code"] == "side_effects_not_acknowledged" and len(posts()) == n0
    done = run_tool(client, h, w["id"], {"name": "hello"}, ack=True)
    assert done["output"]["body"]["body"] == {"note": "hello", "n": 1} and len(posts()) == n0 + 1

    make_user(client, owner_session, h, "dev@demo-a.local", "developer", world.passwords)
    dev = as_user(client, world.passwords, "dev@demo-a.local")
    forbidden = run_tool(client, dev, w["id"], {"name": "x"}, ack=True, status=403)
    assert "tool:test_side_effects" in forbidden["detail"] and len(posts()) == n0 + 1
    # the developer may test read tools, and a write tool is still refused without the permission
    assert run_tool(client, dev, t["id"], {"name": "x"}, status=200)
    viewer = login(client, world.passwords, VIEWER)
    assert run_tool(client, viewer, t["id"], {"name": "x"}, status=403)

    # publish, then test the published version; audit_level=full records arguments
    h = login(client, world.passwords, ADMIN)
    full = rest_spec(ro, audit_level="full")
    full_tool = art(client, h, "tools", "audited-full", full, scope)
    v1 = publish(client, h, "tools", full_tool["id"])
    run_tool(client, h, full_tool["id"], {"name": "visible-arg"}, version=v1["id"])
    run_tool(client, h, t["id"], {"name": "hidden-arg"})
    rows = (
        owner_session.execute(
            text("SELECT details::text FROM shifde.audit_events WHERE action = 'tool.tested'")
        )
        .scalars()
        .all()
    )
    blob = "\n".join(rows)
    assert "visible-arg" in blob and "hidden-arg" not in blob  # metadata level: keys only
    assert '"arg_keys"' in blob and REST_TOKEN not in blob
    used = owner_session.execute(
        text("SELECT count(*) FROM shifde.audit_events WHERE action = 'connection.used'")
    ).scalar_one()
    assert used >= 4  # every tool call went through ConnectionService.open_for_use


def test_rest_tool_timeout_and_retry(
    client: TestClient, world: World, owner_session: Session, rest_server: RestTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    ro = rest_conn(client, h, rest_server, "slow-api")
    spec = rest_spec(
        ro,
        timeout_s=1,
        retry={"max_attempts": 2, "backoff_s": 0},
        binding={"connection_id": ro, "method": "GET", "path_template": "/slow"},
        input_schema={"type": "object", "properties": {}},
    )
    t = art(client, h, "tools", "slow-tool", spec, tenant_scope(owner_session))
    assert validate(client, h, t["id"])["valid"] is True
    n0 = len([r for r in rest_server.requests if r["path"] == "/slow"])
    started = time.monotonic()
    failed = run_tool(client, h, t["id"], {}, status=502)
    assert time.monotonic() - started < 2.9  # bounded by the tool's timeout, not the server's 3 s
    assert failed["code"] == "upstream_error"
    assert len([r for r in rest_server.requests if r["path"] == "/slow"]) >= n0 + 1


# ================================================================== mcp.tool
def test_mcp_tool_snapshot_is_taken_at_publish(
    client: TestClient, world: World, owner_session: Session, mcp_server: McpTestServer
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    sid = secret(client, h, "mcp-tok", MCP_TOKEN)
    conn = connection(
        client,
        h,
        {
            "name": "mcp-srv",
            "connection_type": "mcp",
            "adapter_key": "mcp_http",
            "config": {"url": mcp_server.url},
            "auth_method": "bearer",
            "secret_id": sid,
            "access_mode": "read_write",
        },
    )
    activate(client, h, conn["id"])
    add_schema = {
        "type": "object",
        "required": ["a", "b"],
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    }

    def mcp_spec(tool: str, side: str = "read", **over: Any) -> dict[str, Any]:
        return {
            "executor": "mcp.tool",
            "binding": {"connection_id": conn["id"], "tool_name": tool},
            "input_schema": add_schema
            if tool == "add"
            else {
                "type": "object",
                "required": ["target"],
                "properties": {"target": {"type": "string"}},
            },
            "output_schema": {"type": "object"},
            "side_effect_class": side,
            "required_permissions": [],
            "invocation_policy": {},
            "timeout_s": 10,
            "retry": {"max_attempts": 1, "backoff_s": 0},
            "audit_level": "metadata",
            "idempotency": "none",
            **over,
        }

    t = art(client, h, "tools", "adder", mcp_spec("add"), scope)
    assert {e["code"] for e in validate(client, h, t["id"])["errors"]} == {"mcp_discovery_required"}
    discovery_of(client, h, A, conn["id"])
    assert validate(client, h, t["id"])["valid"] is True
    v1 = publish(client, h, "tools", t["id"])
    snap = v1["spec"]["binding"]["schema_snapshot"]
    assert snap["name"] == "add" and set(snap["input_schema"]["properties"]) == {"a", "b"}
    out = run_tool(client, h, t["id"], {"a": 2, "b": 3}, version=v1["id"])
    assert out["output"]["is_error"] is False and out["output"]["structured"] == {"result": 5}
    assert mcp_server.calls[-1] == "add"
    # arguments are checked before the server is called
    calls = len(mcp_server.calls)
    run_tool(client, h, t["id"], {"a": "x", "b": 1}, version=v1["id"], status=422)
    assert len(mcp_server.calls) == calls

    # unknown tool; a non-read-only tool declared as read; the same tool as a write
    ghost = art(client, h, "tools", "ghost", mcp_spec("nonexistent"), scope)
    assert {e["code"] for e in validate(client, h, ghost["id"])["errors"]} == {
        "mcp_tool_not_discovered"
    }
    wipe_read = art(client, h, "tools", "wipe-read", mcp_spec("wipe", "read"), scope)
    assert {e["code"] for e in validate(client, h, wipe_read["id"])["errors"]} == {
        "tool_not_read_only"
    }
    wipe = art(client, h, "tools", "wipe", mcp_spec("wipe", "destructive"), scope)
    assert validate(client, h, wipe["id"])["valid"] is True
    run_tool(client, h, wipe["id"], {"target": "x"}, status=422)  # no acknowledgement
    assert "wipe" not in mcp_server.calls
    done = run_tool(client, h, wipe["id"], {"target": "x"}, ack=True)
    assert done["output"]["is_error"] is False and mcp_server.calls[-1] == "wipe"


# ================================================================== connection.sql


# ================================================================== permissions on tools
def test_viewer_cannot_author_tools_and_only_publishers_publish(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    t = art(client, h, "tools", "guarded", builtin_tool_spec(), scope)
    v = login(client, world.passwords, VIEWER)
    assert client.get(f"{A}/tools/{t['id']}", headers=v).status_code == 200
    assert client.get(f"{A}/tools", headers=v).json()["items"][0]["id"] == t["id"]
    assert client.get(f"{A}/tools/{t['id']}/draft", headers=v).status_code == 200
    r = client.post(
        f"{A}/tools",
        json={"key": "viewer-tool", "name": "x", "scope": scope, "visibility": "scope"},
        headers=v,
    )
    assert r.status_code == 403
    assert (
        client.put(
            f"{A}/tools/{t['id']}/draft", json={"spec": {}, "row_version": 1}, headers=v
        ).status_code
        == 403
    )
    assert client.post(f"{A}/tools/{t['id']}/publish", json={}, headers=v).status_code == 403
    assert (
        client.patch(
            f"{A}/tools/{t['id']}", json={"name": "z", "row_version": 1}, headers=v
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{A}/tools/{t['id']}/test",
            json={"version_id": "draft", "args": {"text": "x"}},
            headers=v,
        ).status_code
        == 403
    )
    assert codes({"errors": []}) == set()


# Ported in app-2 part 2c over `sample_db_url` (B-2420, B-2423; SETH test_p4_tools.py).
def test_sql_tool_binds_parameters_and_stays_read_only(
    client: TestClient, world: World, owner_session: Session, sample_db_url: str
) -> None:
    h = login(client, world.passwords, ADMIN)
    scope = tenant_scope(owner_session)
    pg_password = sample_db_url.split(":")[2].split("@")[0]
    sid = secret(client, h, "pg-pass", pg_password)
    conn = connection(client, h, pg_body(sample_db_url, sid))
    activate(client, h, conn["id"])

    def sql_spec(sql: str, side: str = "read", **binding: Any) -> dict[str, Any]:
        return {
            "executor": "connection.sql",
            "binding": {"connection_id": conn["id"], "sql_template": sql, **binding},
            "input_schema": {
                "type": "object",
                "properties": {"dept": {"type": "string"}, "min_id": {"type": "integer"}},
            },
            "output_schema": {"type": "object", "required": ["columns", "rows"]},
            "side_effect_class": side,
            "required_permissions": [],
            "invocation_policy": {},
            "timeout_s": 10,
            "retry": {"max_attempts": 1, "backoff_s": 0},
            "audit_level": "metadata",
            "idempotency": "none",
        }

    t = art(
        client,
        h,
        "tools",
        "by-dept",
        sql_spec("SELECT name FROM employees WHERE department = %(dept)s ORDER BY id", max_rows=2),
        scope,
    )
    assert validate(client, h, t["id"])["valid"] is True
    total = post(
        client,
        f"{A}/connections/{conn['id']}/invoke",
        h,
        {"sql": "SELECT count(*) FROM employees"},
        200,
    )["result"]["rows"][0][0]
    dept = post(
        client,
        f"{A}/connections/{conn['id']}/invoke",
        h,
        {"sql": "SELECT department FROM employees ORDER BY id LIMIT 1"},
        200,
    )["result"]["rows"][0][0]
    res = run_tool(client, h, t["id"], {"dept": dept})
    assert res["output"]["columns"] == ["name"] and 1 <= len(res["output"]["rows"]) <= 2
    # an injection attempt is just a (non-matching) value
    inj = run_tool(client, h, t["id"], {"dept": "x'; DROP TABLE employees; --"})
    assert inj["output"]["rows"] == []
    after = post(
        client,
        f"{A}/connections/{conn['id']}/invoke",
        h,
        {"sql": "SELECT count(*) FROM employees"},
        200,
    )["result"]["rows"][0][0]
    assert after == total

    def codes_of(spec: dict[str, Any], rv: int) -> set[str]:
        edit(client, h, "tools", t["id"], spec, rv)
        return {e["code"] for e in validate(client, h, t["id"])["errors"]}

    assert codes_of(sql_spec("DELETE FROM employees"), 1) == {
        "write_statement_requires_write_class"
    }
    assert codes_of(sql_spec("SELECT 1; SELECT 2"), 2) == {"invalid_template"}
    assert codes_of(sql_spec("SELECT %s"), 3) == {"invalid_template"}
    assert codes_of(sql_spec("SELECT %(ghost)s"), 4) == {"unknown_argument"}
    assert codes_of(sql_spec("SELECT 1 WHERE 'a' LIKE 'a%%'"), 5) == set()
    assert codes_of(
        sql_spec(
            "UPDATE employees SET title = 'x'",
            "write_idempotent",
        ),
        6,
    ) >= {"connection_read_only"}
    assert codes_of(sql_spec("SELECT 1", max_rows=0), 7) == {"invalid_value"}
