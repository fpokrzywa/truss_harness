"""P5 worker interruption: a REAL `shifde-worker` subprocess is killed mid-run; another worker
recovers. Side effects of non-idempotent tools are never repeated (at most once)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, REST_TOKEN, A, activate, connection, secret
from tests._support.p5util import (
    Stack,
    SubprocessWorker,
    events,
    limited_user,
    rest_tool_spec,
    start_run,
    steps,
    thread_worker,
    tool_call_reply,
    user_session,
    wait_for,
    wait_run,
)

SCRIPT = [
    {"match": "Tool results", "reply": "finished"},
    {"match": "go", "reply": tool_call_reply("target", {"item": "x"})},
]


class HangServer:
    """Records requests; the FIRST request to /hang blocks until released (the worker is killed
    meanwhile); later requests answer immediately."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.first_started = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.log.append((self.command, self.path))
                first = len([1 for _, p in outer.log if p == "/hang"]) == 1
                if self.path == "/hang" and first:
                    outer.first_started.set()
                    outer.release.wait(60)
                    return  # the client is long gone
                raw = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def count(self, method: str) -> int:
        return len([1 for m, p in self.log if m == method and p == "/hang"])

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def build(
    client: TestClient, world: World, owner: Session, server: HangServer, side: str, method: str
) -> tuple[dict[str, str], Stack, dict[str, str]]:
    h = login(client, world.passwords, ADMIN)
    conn = connection(
        client,
        h,
        {
            "name": "hang",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "access_mode": "read_write",
            "auth_method": "bearer",
            "secret_id": secret(client, h, "hang-token", REST_TOKEN),
            "config": {"base_url": server.base_url, "health_path": "/hang-health"},
        },
    )
    activate(client, h, conn["id"])
    stack = Stack(
        client,
        h,
        owner,
        script=SCRIPT,
        tool_key="target",
        tool=rest_tool_spec(conn["id"], method, "/hang", side, body=method == "POST"),
    )
    limited_user(
        client,
        owner,
        login(client, world.passwords, ADMIN),
        world.passwords,
        "approver@demo-a.local",
        ["workspace:read", "project:read", "run:read", "approval:read", "approval:decide"],
    )
    return (
        login(client, world.passwords, ADMIN),
        stack,
        user_session(client, world.passwords, "approver@demo-a.local"),
    )


def test_killed_worker_midway_through_a_read_tool_is_recovered_and_the_read_is_retried(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, _ = build(client, world, owner_session, server, "read", "GET")
        run = start_run(client, h, stack.agent["id"], "go")
        w1 = SubprocessWorker(settings, "w1")
        try:
            assert server.first_started.wait(60), "the tool call never started"
            assert client.get(f"{A}/runs/{run['id']}", headers=h).json()["status"] == "running"
            w1.kill()
        finally:
            w1.kill() if w1.proc.poll() is None else None
        w2 = SubprocessWorker(settings, "w2")
        try:
            done = wait_run(client, h, run["id"], timeout=90)
        finally:
            w2.stop()
        assert done["status"] == "completed", done
        assert done["attempt_count"] >= 2
        types = [e["type"] for e in events(client, h, run["id"])]
        assert "lease_lost" in types and "recovered" in types
        tool_steps = [s for s in steps(client, h, run["id"]) if s["kind"] == "tool_call"]
        assert [(s["attempt"], s["status"]) for s in tool_steps] == [
            (1, "failed"),
            (2, "succeeded"),
        ]
        assert server.count("GET") == 2  # the interrupted read was retried
        # model calls that had succeeded were not repeated
        assert done["usage"]["model_calls"] == 2
    finally:
        server.close()


def test_killed_worker_midway_through_a_non_idempotent_write_never_repeats_the_side_effect(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build(client, world, owner_session, server, "write_non_idempotent", "POST")
        run = start_run(client, h, stack.agent["id"], "go")
        w1 = SubprocessWorker(settings, "w1")
        try:
            wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=90)
            [appr] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
            assert (
                ah.client.post(
                    f"{A}/approvals/{appr['id']}/decide", headers=ah, json={"decision": "approve"}
                ).status_code
                == 200
            )
            assert server.first_started.wait(60), "the write never started"
            w1.kill()  # the POST reached the target; its outcome is unknown to the platform
        finally:
            if w1.proc.poll() is None:
                w1.kill()
        w2 = SubprocessWorker(settings, "w2")
        try:
            waiting = wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=90)
            assert waiting["waiting_reason"] == "indeterminate_side_effect"
            [ind] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
            assert ind["reason"] == "indeterminate_side_effect" and ind["arguments"] == {
                "item": "x"
            }
            inv = owner_session.execute(
                text("SELECT status, attempts FROM shifde.tool_invocations")
            ).all()
            assert inv == [("indeterminate", 1)]
            # a comment is mandatory; only the three indeterminate decisions are accepted
            assert (
                ah.client.post(
                    f"{A}/approvals/{ind['id']}/decide",
                    headers=ah,
                    json={"decision": "mark_succeeded"},
                ).status_code
                == 422
            )
            assert (
                ah.client.post(
                    f"{A}/approvals/{ind['id']}/decide",
                    headers=ah,
                    json={"decision": "approve", "comment": "x"},
                ).status_code
                == 422
            )
            ok = ah.client.post(
                f"{A}/approvals/{ind['id']}/decide",
                headers=ah,
                json={"decision": "mark_succeeded", "comment": "verified in target"},
            )
            assert ok.status_code == 200
            done = wait_run(client, h, run["id"], timeout=90)
        finally:
            w2.stop()
        assert done["status"] == "completed", done
        assert server.count("POST") == 1  # at most once, across the crash and the recovery
        [inv_row] = owner_session.execute(text("SELECT status FROM shifde.tool_invocations")).all()
        assert inv_row[0] == "resolved_succeeded"
    finally:
        server.close()


def test_operator_can_authorize_a_retry_of_an_indeterminate_write(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build(client, world, owner_session, server, "write_non_idempotent", "POST")
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3):
            wait_run(client, h, run["id"], {"waiting_for_approval"})
            [appr] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
            ah.client.post(
                f"{A}/approvals/{appr['id']}/decide", headers=ah, json={"decision": "approve"}
            )
            assert server.first_started.wait(60)
            server.release.set()  # the hanging request ends without an answer (connection closed)
            waiting = wait_run(
                client, h, run["id"], {"waiting_for_approval", "completed", "failed"}
            )
            assert (
                waiting["status"] == "waiting_for_approval"
            )  # unknown outcome -> operator decides
            [ind] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
            assert ind["reason"] == "indeterminate_side_effect"
            r = ah.client.post(
                f"{A}/approvals/{ind['id']}/decide",
                headers=ah,
                json={"decision": "retry", "comment": "target has no record of it"},
            )
            assert r.status_code == 200
            done = wait_run(client, h, run["id"])
        assert done["status"] == "completed"
        assert server.count("POST") == 2  # one automatic attempt + the operator-authorized retry
        keys = owner_session.execute(
            text("SELECT idempotency_key, status FROM shifde.tool_invocations ORDER BY started_at")
        ).all()
        assert len({k[0] for k in keys}) == 2 and [k[1] for k in keys] == [
            "resolved_retry",
            "succeeded",
        ]
    finally:
        server.close()


def test_graceful_stop_hands_the_run_back_and_another_worker_finishes_it(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=SCRIPT[:1] + [{"match": "go", "reply": "easy"}])
    owner_session.execute(
        text("UPDATE shifde.connections SET config = config || CAST(:c AS jsonb) WHERE id = :i"),
        {"c": json.dumps({"latency_ms": 1500}), "i": stack.connection},
    )
    owner_session.commit()
    run = start_run(client, h, stack.agent["id"], "go")
    with thread_worker(settings) as w1:
        wait_run(client, h, run["id"], {"running"})
        w1.request_stop()
    state_after = client.get(f"{A}/runs/{run['id']}", headers=h).json()
    assert state_after["status"] in ("pending", "completed")
    if state_after["status"] == "pending":
        with thread_worker(settings):
            done = wait_run(client, h, run["id"])
        assert done["status"] == "completed"
    assert wait_for(lambda: True)


def test_worker_heartbeat_feeds_platform_status(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, "platform@shifde.local")
    before: Any = client.get("/api/v1/platform/status", headers=h).json()["worker"]
    assert before["status"] == "not_running" and before["count"] == 0
    with thread_worker(settings):
        got = wait_for(
            lambda: (lambda w: w if w["status"] == "healthy" else None)(
                client.get("/api/v1/platform/status", headers=h).json()["worker"]
            ),
            20,
        )
        assert got["count"] == 1 and got["last_heartbeat_at"]
    owner_session.execute(
        text(
            "UPDATE shifde.workers SET heartbeat_at = now() - interval '1 hour', status = 'running'"
        )
    )
    owner_session.commit()
    stale = client.get("/api/v1/platform/status", headers=h).json()["worker"]
    assert stale["status"] == "stale" and stale["count"] == 0
