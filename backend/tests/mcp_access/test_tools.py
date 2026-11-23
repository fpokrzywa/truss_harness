"""The tool calls (SP8 design 3.3, 3.4, 3.7 items 5 to 7; section 10 items 9 to 17):
`allowed_tools`, the per-key and per-client rate limits, `start_run`'s cost cap and the ledger,
the SHI-provider switch, the metadata-only audit and its category, and result truncation."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any  # Any: JSON documents, spy arguments

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import OpenAIFakeServer
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.mcp_access import tools as mcp_tools
from shifde.runtime.service import RunService
from tests._support.fullapp import World
from tests._support.p5util import thread_worker
from tests._support.p8util import budget, project_scope
from tests.mcp_access.helpers import (
    ADMIN_A,
    ADMIN_B,
    ORG_A,
    ORG_B,
    V1_TOOLS,
    A,
    B,
    agent,
    audit_rows,
    call,
    make_key,
    mcp_client,
    mcp_rows,
    mcp_settings,
    ok,
    project,
    refused,
    rpc,
    set_mcp,
    sign_in,
    tenant,
)
from tests.models_admin.routes import shared_model

ALL = sorted(V1_TOOLS)
SENTINEL = "SENTINEL-mcp-7f3a9c41"
SHI_KEY = "sk-shi-installation-key-mcp"
CALLED_KEYS = {"tool", "api_key_id", "outcome", "error_code", "duration_ms"}


def _reasons(owner: Session, org: str) -> list[str]:
    rows = mcp_rows(owner, tenant(owner, org), "mcp.access_refused")
    return [r["details"]["reason"] for r in rows]


# ------------------------------------------------------------------ allowed_tools and rate
def test_allowed_tools_null_is_the_default_set_and_empty_allows_none(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A)
    args = {"project_id": str(uuid7()), "agent_version_id": str(uuid7()), "input": "hi"}
    assert refused(mcp, key, "start_run", args) == "tool_not_allowed"
    assert ok(mcp, key, "whoami")["client_slug"] == ORG_A
    set_mcp(mcp, world, ADMIN_A, A, allowed_tools=[])
    assert refused(mcp, key, "whoami") == "tool_not_allowed"
    assert _reasons(owner_session, ORG_A) == ["tool_not_allowed", "tool_not_allowed"]
    called = mcp_rows(owner_session, tenant(owner_session, ORG_A), "mcp.tool_called")
    assert [(r["details"]["tool"], r["details"]["error_code"]) for r in called] == [
        ("start_run", "tool_not_allowed"),
        ("whoami", None),
        ("whoami", "tool_not_allowed"),
    ]


def test_the_key_rate_trips_at_n_plus_one_and_the_client_ceiling_at_4n_plus_one(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    keys = [make_key(mcp, world, ADMIN_A, A, name=f"key {n}")[1] for n in range(3)]
    set_mcp(mcp, world, ADMIN_A, A, rate_per_minute=2)
    first, second, third = keys
    assert [call(mcp, first, "whoami")[0] for _ in range(2)] == [False, False]
    is_error, body = call(mcp, first, "whoami")  # call N+1 of the key
    assert is_error and body["error"] == "rate_limited" and "API key" in body["message"]
    assert isinstance(body["retry_after"], int) and body["retry_after"] > 0
    call(mcp, first, "whoami")  # the client has now counted 4 calls of `first`
    assert [call(mcp, second, "whoami")[0] for _ in range(4)] == [False, False, True, True]
    # 4N = 8 calls counted: call 4N+1 of the client is refused whichever key makes it
    for key in (second, third):
        is_error, body = call(mcp, key, "whoami")
        assert is_error and body["error"] == "rate_limited"
        assert "organization" in body["message"]
    assert set(_reasons(owner_session, ORG_A)) == {"rate"}
    assert len(_reasons(owner_session, ORG_A)) == 6


# ------------------------------------------------------------------ start_run and get_run
def test_start_run_caps_max_cost_and_get_run_reads_the_run(
    mcp: TestClient, world: World, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = sign_in(mcp, world, ADMIN_A)
    published = agent(mcp, h, A, project(owner_session, ORG_A))
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A, allowed_tools=ALL, max_cost_per_call=0.5)
    caps: list[Decimal | None] = []
    original = RunService.create

    def spy(self: RunService, body: Any, idempotency_key: Any, **kwargs: Any) -> Any:
        caps.append(kwargs.get("max_cost"))
        return original(self, body, idempotency_key, **kwargs)

    monkeypatch.setattr(RunService, "create", spy)
    base = {
        "project_id": str(project(owner_session, ORG_A)),
        "agent_version_id": published["version"],
        "input": "hello",
    }
    started = [
        ok(mcp, key, "start_run", {**base, "max_cost": 5}),
        ok(mcp, key, "start_run", {**base, "max_cost": 0.2}),
        ok(mcp, key, "start_run", base),
    ]
    assert caps == [Decimal("0.5"), Decimal("0.2"), Decimal("0.5")]
    assert all(s["status"] == "pending" for s in started)
    run = ok(mcp, key, "get_run", {"run_id": started[0]["run_id"]})
    assert run["run_id"] == started[0]["run_id"] and run["status"] == "pending"
    assert run["pending_approvals"] == 0 and run["output"] is None
    called = mcp_rows(owner_session, tenant(owner_session, ORG_A), "mcp.tool_called")
    starts = [r["details"] for r in called if r["details"]["tool"] == "start_run"]
    assert [d["run_id"] for d in starts] == [s["run_id"] for s in started]
    assert refused(mcp, key, "start_run", {**base, "max_cost": 0}) == "invalid_arguments"
    assert refused(mcp, key, "start_run", {**base, "surprise": 1}) == "invalid_arguments"


def test_an_exhausted_budget_refuses_start_run_in_the_ledger(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    h = sign_in(mcp, world, ADMIN_A)
    published = agent(mcp, h, A, project(owner_session, ORG_A))
    budget(mcp, h, project_scope(owner_session), metric="cost", amount=0)
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A, allowed_tools=ALL)
    started = ok(
        mcp,
        key,
        "start_run",
        {
            "project_id": str(project(owner_session, ORG_A)),
            "agent_version_id": published["version"],
            "input": "hello",
        },
    )
    assert started["status"] == "failed"
    assert ok(mcp, key, "get_run", {"run_id": started["run_id"]})["error_class"] == "budget_denied"
    used = owner_session.execute(
        text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t"),
        {"t": tenant(owner_session, ORG_A)},
    ).scalar_one()
    assert used == 0  # refused before it started: no model was asked


def test_with_the_shi_switch_off_a_clients_run_never_reaches_shi(
    settings: Settings, world: World, owner_session: Session, app_db: Database
) -> None:
    with OpenAIFakeServer(api_key=SHI_KEY) as server:
        configured = mcp_settings(
            settings,
            shi_provider_config={"base_url": f"{server.base_url}/v1"},
            shi_provider_api_key=SecretStr(SHI_KEY),
        )
        with mcp_client(configured) as c:
            hp = sign_in(c, world, "platform@shifde.local")
            r = c.put(
                "/api/v1/platform/shi-provider",
                headers=hp,
                json={"adapter_key": "openai_compatible", "status": "active"},
            )
            assert r.status_code == 200, r.text
            model = shared_model(app_db, model_key="fake-gpt")
            access = f"/api/v1/platform/orgs/{ORG_B}/shi-provider-access"
            assert c.put(access, headers=hp, json={"enabled": True}).status_code == 200
            hb = sign_in(c, world, ADMIN_B)
            published = agent(c, hb, B, project(owner_session, ORG_B), model=model)
            hp = sign_in(c, world, "platform@shifde.local")
            assert c.put(access, headers=hp, json={"enabled": False}).status_code == 200
            _, key = make_key(c, world, ADMIN_B, B)
            set_mcp(c, world, ADMIN_B, B, allowed_tools=ALL)
            is_error, body = call(
                c,
                key,
                "start_run",
                {
                    "project_id": str(project(owner_session, ORG_B)),
                    "agent_version_id": published["version"],
                    "input": "hello",
                },
            )
            assert not is_error, body  # the run is created; its model call is refused
            with thread_worker(configured):
                end = time.monotonic() + 30
                run = ok(c, key, "get_run", {"run_id": body["run_id"]})
                while run["status"] == "pending" or run["status"] == "running":
                    assert time.monotonic() < end, run
                    time.sleep(0.1)
                    run = ok(c, key, "get_run", {"run_id": body["run_id"]})
            assert run["status"] == "failed", run
            assert server.bodies == []


# ------------------------------------------------------------------ audit
def test_no_argument_reaches_an_audit_row_or_a_log(
    mcp: TestClient,
    world: World,
    owner_session: Session,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = sign_in(mcp, world, ADMIN_A)
    published = agent(mcp, h, A, project(owner_session, ORG_A))
    _, key = make_key(mcp, world, ADMIN_A, A)
    _, key_b = make_key(mcp, world, ADMIN_B, B)
    set_mcp(mcp, world, ADMIN_A, A, allowed_tools=ALL)
    capsys.readouterr()
    caplog.clear()
    start = {
        "project_id": str(project(owner_session, ORG_A)),
        "agent_version_id": published["version"],
        "input": f"please {SENTINEL} now",
    }
    started = ok(mcp, key, "start_run", start)
    ok(mcp, key, "whoami")
    ok(mcp, key, "list_projects")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_projects", {"cursor": SENTINEL}),
        ("list_agents", {"project_id": SENTINEL}),
        ("whoami", {SENTINEL: SENTINEL}),
        (SENTINEL, {"x": SENTINEL}),
        ("get_run", {"run_id": started["run_id"], "note": SENTINEL}),
    ]
    for tool, args in calls:
        assert call(mcp, key, tool, args)[0] is True, tool
    rpc(mcp, key_b, "tools/call", {"name": "whoami", "arguments": {"q": SENTINEL}})  # disabled
    logged = capsys.readouterr()
    assert SENTINEL not in logged.out + logged.err and SENTINEL not in caplog.text
    rows = audit_rows(owner_session)
    assert rows and all(SENTINEL not in json.dumps(r["details"]) for r in rows)
    called = [r["details"] for r in rows if r["action"] == "mcp.tool_called"]
    successes = [d for d in called if d["outcome"] == "success"]
    assert [d["tool"] for d in successes] == ["start_run", "whoami", "list_projects"]
    # `actor` is the label every audit row carries (`audit.record`)
    assert set(successes[0]) - {"actor"} == CALLED_KEYS | {"run_id"}
    assert all(set(d) - {"actor"} == CALLED_KEYS for d in successes[1:])
    assert len(called) == 3 + len(calls)  # one row per call, failures included
    assert "unknown" in [d["tool"] for d in called]  # a client-chosen name is not recorded


def test_the_mcp_audit_category_lists_this_clients_mcp_events_only(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    _, key = make_key(mcp, world, ADMIN_A, A)
    _, key_b = make_key(mcp, world, ADMIN_B, B)
    set_mcp(mcp, world, ADMIN_A, A)
    ok(mcp, key, "whoami")
    assert rpc(mcp, key_b, "tools/list").status_code == 403  # B's refusal, filed in B
    h = sign_in(mcp, world, ADMIN_A)
    r = mcp.get(f"{A}/audit-events", params={"category": "mcp", "limit": 50}, headers=h)
    assert r.status_code == 200, r.text
    actions = [e["action"] for e in r.json()["items"]]
    assert sorted(actions) == ["mcp.settings.updated", "mcp.tool_called"]
    in_b = mcp_rows(owner_session, tenant(owner_session, ORG_B), "mcp.access_refused")
    assert len(in_b) == 1


# ------------------------------------------------------------------ truncation
def test_a_result_above_256_kib_is_truncated(
    mcp: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A)
    items = [{"id": str(uuid7()), "pad": "x" * 1000} for _ in range(400)]

    def many(*_: Any) -> dict[str, Any]:
        return {"items": items, "next_cursor": None}

    def blob(*_: Any) -> dict[str, Any]:
        return {"blob": "y" * 300_000}

    whoami = mcp_tools.TOOLS["whoami"]
    monkeypatch.setitem(mcp_tools.TOOLS, "whoami", replace(whoami, handler=many))
    r = rpc(mcp, key, "tools/call", {"name": "whoami", "arguments": {}})
    text_out = r.json()["result"]["content"][0]["text"]
    listed = json.loads(text_out)
    assert listed["truncated"] is True and 0 < len(listed["items"]) < len(items)
    assert len(text_out.encode()) <= 256 * 1024 and listed["next_cursor"] is not None
    monkeypatch.setitem(mcp_tools.TOOLS, "whoami", replace(whoami, handler=blob))
    r = rpc(mcp, key, "tools/call", {"name": "whoami", "arguments": {}})
    text_out = r.json()["result"]["content"][0]["text"]
    cut = json.loads(text_out)
    assert cut["truncated"] is True and len(text_out.encode()) <= 256 * 1024
