"""P5 human approvals: side-effect gating, separation of duties, decisions, expiry, cancellation."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, VIEWER, A
from tests._support.p4util import set_tenant_setting
from tests._support.p5util import (
    Stack,
    audit_actions,
    events,
    limited_user,
    rest_connection,
    rest_tool_spec,
    start_run,
    steps,
    thread_worker,
    tool_call_reply,
    user_session,
    wait_for,
    wait_run,
)

POST_SCRIPT = [
    {"match": "Tool results", "reply": "posted"},
    {"match": "create", "reply": tool_call_reply("post_item", {"item": "x1"})},
]
APPROVER = ["workspace:read", "project:read", "run:read", "approval:read", "approval:decide"]


def posts(server: RestTestServer) -> int:
    return len([r for r in server.requests if r["method"] == "POST" and r["path"] == "/echo"])


def setup(
    client: TestClient,
    world: World,
    owner: Session,
    server: RestTestServer,
    *,
    side: str = "write_non_idempotent",
    method: str = "POST",
    **tool_over: Any,
) -> tuple[dict[str, str], Stack, dict[str, str]]:
    h = login(client, world.passwords, ADMIN)
    conn = rest_connection(client, h, server.base_url)
    server.requests.clear()
    stack = Stack(
        client,
        h,
        owner,
        script=POST_SCRIPT,
        tool_key="post_item",
        tool=rest_tool_spec(conn, method, "/echo", side, body=True, **tool_over),
    )
    limited_user(client, owner, h, world.passwords, "approver@demo-a.local", APPROVER)
    ah = user_session(client, world.passwords, "approver@demo-a.local")
    return login(client, world.passwords, ADMIN), stack, ah


def pending(client: TestClient, h: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = client.get(f"{A}/approvals?status=pending", headers=h).json()[
        "items"
    ]
    return out


def test_non_idempotent_write_waits_for_approval_then_runs_once(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h, stack, ah = setup(client, world, owner_session, rest_server)
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        waiting = wait_run(client, h, run["id"], {"waiting_for_approval"})
        assert waiting["waiting_reason"] == "approval" and posts(rest_server) == 0
        [appr] = pending(client, ah)
        assert appr["reason"] == "tool_side_effect"
        assert appr["tool"] == {
            "key": "post_item",
            "version": 1,
            "side_effect_class": "write_non_idempotent",
        }
        assert appr["arguments"] == {"item": "x1"}  # the exact validated arguments
        assert appr["run_initiated_by"]["display"] and appr["status"] == "pending"
        # separation of duties: the initiator cannot approve their own run's action
        own = client.post(
            f"{A}/approvals/{appr['id']}/decide", headers=h, json={"decision": "approve"}
        )
        assert own.status_code == 403 and own.json()["code"] == "separation_of_duties"
        ok = ah.client.post(
            f"{A}/approvals/{appr['id']}/decide",
            headers=ah,
            json={"decision": "approve", "comment": "looks right"},
        )
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        done = wait_run(client, h, run["id"])
        assert done["status"] == "completed"
        again = ah.client.post(
            f"{A}/approvals/{appr['id']}/decide", headers=ah, json={"decision": "approve"}
        )
        assert again.status_code == 409 and again.json()["code"] == "already_decided"
    assert posts(rest_server) == 1
    types = [e["type"] for e in events(client, h, run["id"])]
    assert "approval_requested" in types and "approval_decided" in types
    acts = audit_actions(owner_session)
    assert "approval.requested" in acts and "approval.decided" in acts
    assert [s["kind"] for s in steps(client, h, run["id"])].count("approval") == 1
    decided = ah.client.get(f"{A}/approvals?status=decided", headers=ah).json()["items"]
    assert [d["decision"]["decision"] for d in decided] == ["approve"]


def test_rejected_approval_fails_the_run_and_nothing_is_sent(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h, stack, ah = setup(client, world, owner_session, rest_server)
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        wait_run(client, h, run["id"], {"waiting_for_approval"})
        [appr] = pending(client, ah)
        r = ah.client.post(
            f"{A}/approvals/{appr['id']}/decide", headers=ah, json={"decision": "reject"}
        )
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        done = wait_run(client, h, run["id"])
    assert done["status"] == "failed" and done["error_class"] == "approval_rejected"
    assert posts(rest_server) == 0


def test_self_approval_is_possible_only_when_the_tenant_allows_it(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    set_tenant_setting(owner_session, "demo-a", "allow_self_approval", True)
    h, stack, _ah = setup(client, world, owner_session, rest_server)
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        wait_run(client, h, run["id"], {"waiting_for_approval"})
        [appr] = pending(client, h)
        assert (
            client.post(
                f"{A}/approvals/{appr['id']}/decide", headers=h, json={"decision": "approve"}
            ).status_code
            == 200
        )
        assert wait_run(client, h, run["id"])["status"] == "completed"


def test_decide_needs_approval_decide_and_valid_decision_values(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h, stack, ah = setup(client, world, owner_session, rest_server)
    limited_user(
        client,
        owner_session,
        login(client, world.passwords, ADMIN),
        world.passwords,
        "reader@demo-a.local",
        ["workspace:read", "project:read", "approval:read"],
    )
    rh = user_session(client, world.passwords, "reader@demo-a.local")
    h = login(client, world.passwords, ADMIN)
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        wait_run(client, h, run["id"], {"waiting_for_approval"})
        [appr] = pending(client, ah)
        assert (
            rh.client.post(
                f"{A}/approvals/{appr['id']}/decide", headers=rh, json={"decision": "approve"}
            ).status_code
            == 403
        )
        assert (
            ah.client.post(
                f"{A}/approvals/{appr['id']}/decide",
                headers=ah,
                json={"decision": "retry", "comment": "x"},
            ).status_code
            == 422
        )
        assert (
            ah.client.post(
                f"{A}/approvals/{appr['id']}/decide",
                headers=ah,
                json={"decision": "approve", "extra": 1},
            ).status_code
            == 422
        )
        vh = user_session(client, world.passwords, VIEWER)  # Viewer has no approval:read -> 404
        assert vh.client.get(f"{A}/approvals/{appr['id']}", headers=vh).status_code == 404
        assert ah.client.get(f"{A}/approvals?status=bogus", headers=ah).status_code == 422
        client.post(f"{A}/runs/{run['id']}/cancel", headers=h)


def test_cancel_a_run_waiting_for_approval(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h, stack, ah = setup(client, world, owner_session, rest_server)
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        wait_run(client, h, run["id"], {"waiting_for_approval"})
        r = client.post(f"{A}/runs/{run['id']}/cancel", headers=h)
        assert r.json()["status"] == "cancelled"
    [row] = owner_session.execute(text("SELECT status FROM shifde.approvals")).all()
    assert row[0] == "cancelled" and posts(rest_server) == 0


def test_expired_approval_times_the_run_out_through_the_reaper(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h, stack, ah = setup(client, world, owner_session, rest_server)
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        wait_run(client, h, run["id"], {"waiting_for_approval"})
        owner_session.execute(
            text("UPDATE shifde.approvals SET expires_at = now() - interval '1 second'")
        )
        owner_session.commit()
        done = wait_run(client, h, run["id"], timeout=30)
        assert done["status"] == "timed_out" and "approval" in done["error_message"]
        [appr] = ah.client.get(f"{A}/approvals?status=expired", headers=ah).json()["items"]
        assert appr["status"] == "expired"
    assert posts(rest_server) == 0 and "approval.expired" in audit_actions(owner_session)


def test_write_idempotent_tool_runs_without_approval_and_sends_its_idempotency_key(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h, stack, _ = setup(
        client,
        world,
        owner_session,
        rest_server,
        side="write_idempotent",
        method="PUT",
        idempotency="key_supported",
    )
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed"
    [req] = [r for r in rest_server.requests if r["method"] == "PUT"]
    assert req["idempotency_key"] and len(req["idempotency_key"]) == 64
    assert wait_for(lambda: True)


def test_destructive_tools_always_need_approval_even_when_the_agent_opts_out(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    h = login(client, world.passwords, ADMIN)
    conn = rest_connection(client, h, rest_server.base_url)
    rest_server.requests.clear()
    stack = Stack(
        client,
        h,
        owner_session,
        script=POST_SCRIPT,
        tool_key="post_item",
        tool=rest_tool_spec(conn, "DELETE", "/echo", "destructive", body=True),
        agent_over={"approval_rules": {"require_approval_for": []}},
    )
    run = start_run(client, h, stack.agent["id"], "please create it")
    with thread_worker(settings):
        waiting = wait_run(client, h, run["id"], {"waiting_for_approval"})
    assert waiting["waiting_reason"] == "approval"
    assert [r for r in rest_server.requests if r["method"] == "DELETE"] == []
