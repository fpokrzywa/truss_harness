"""Helpers for the P6 pipeline tests: the reference-pipeline world and API shortcuts."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, REST_TOKEN, A, activate, connection, pg_body, secret
from tests._support.p4util import art, publish, set_tenant_setting
from tests._support.p5util import (
    Hdr,
    Stack,
    limited_user,
    rest_connection,
    rest_tool_spec,
    tool_call_reply,
    user_session,
    wait_for,
    wait_run,
)

APPROVER_PERMS = [
    "workspace:read",
    "project:read",
    "run:read",
    "approval:read",
    "approval:decide",
    "pipeline:read",
]
SCRIPT = [{"match": "open_requests", "reply": "summary: elevated customer risk"}]
ROW_SCHEMA = {
    "type": "object",
    "required": ["customer_id", "status"],
    "properties": {"customer_id": {"type": "integer"}, "status": {"type": "string"}},
}
PARAMS = {
    "type": "object",
    "required": ["region", "threshold"],
    "properties": {"region": {"type": "string"}, "threshold": {"type": "integer"}},
    "additionalProperties": False,
}


class World6:
    """Admin session + connections + a published agent stack + an approver user."""

    def __init__(
        self,
        client: TestClient,
        world: World,
        owner: Session,
        rest_server: RestTestServer,
        sample_db_url: str,
        *,
        script: list[dict[str, str]] | None = None,
        tool: Callable[[str], dict[str, Any]] | None = None,
        tool_key: str = "echo",
    ) -> None:
        set_tenant_setting(owner, "demo-a", "capture_content", "full")
        self.client, self.owner, self.world = client, owner, world
        self.h = login(client, world.passwords, ADMIN)
        self.rest = rest_server
        self.rest_id = rest_connection(client, self.h, rest_server.base_url)
        password = sample_db_url.split(":")[2].split("@")[0]
        self.pg = str(
            activate(
                client,
                self.h,
                connection(
                    client,
                    self.h,
                    pg_body(sample_db_url, secret(client, self.h, "pg-pw", password)),
                )["id"],
            )["id"]
        )
        self.stack = Stack(
            client,
            self.h,
            owner,
            script=script or SCRIPT,
            tool=tool(self.rest_id) if tool else None,
            tool_key=tool_key,
        )
        self.scope = self.stack.scope
        limited_user(
            client, owner, self.h, world.passwords, "approver@demo-a.local", APPROVER_PERMS
        )
        self.approver: Hdr = user_session(client, world.passwords, "approver@demo-a.local")
        self.h = login(client, world.passwords, ADMIN)  # the approver login replaced the cookie
        rest_server.requests.clear()

    # ---------------------------------------------------------------- spec builders
    def reference_spec(self, **over: Any) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "schema_version": "1",
            "params": deepcopy(PARAMS),
            "policies": {"max_parallel": 4, "deadline_seconds": 600, "on_failure": "fail_fast"},
            "nodes": [
                {
                    "id": "src",
                    "type": "db_query",
                    "config": {
                        "connection_id": self.pg,
                        "sql": "SELECT customer_id, status, priority, region FROM service_requests "
                        "WHERE region = %(region)s ORDER BY id",
                        "args": {"region": "$params.region"},
                        "max_rows": 500,
                    },
                    "inputs": {},
                },
                {
                    "id": "check",
                    "type": "validate",
                    "config": {"schema": {"$ref": "#/schemas/Row"}, "on_invalid": "fail"},
                    "inputs": {"data": "src.rows"},
                },
                {
                    "id": "prep",
                    "type": "transform",
                    "config": {
                        "operations": [
                            {"op": "filter", "expression": "row.status == 'open'"},
                            {"op": "select", "fields": ["customer_id", "priority"]},
                            {
                                "op": "aggregate",
                                "group_by": ["customer_id"],
                                "fn": "count",
                                "as": "open_requests",
                            },
                        ]
                    },
                    "inputs": {"data": "check.data"},
                },
                {
                    "id": "rollup",
                    "type": "transform",
                    "config": {
                        "operations": [
                            {
                                "op": "aggregate",
                                "group_by": [],
                                "fn": "max",
                                "field": "open_requests",
                                "as": "max_open",
                            }
                        ]
                    },
                    "inputs": {"data": "prep.data"},
                },
                {
                    "id": "summ",
                    "type": "agent",
                    "config": {"agent_version_id": f"latest:{self.stack.agent['id']}"},
                    "inputs": {"input": "prep.data"},
                },
                {
                    "id": "risk",
                    "type": "condition",
                    "config": {"expression": "inputs.rollup[0].max_open > params.threshold"},
                    "inputs": {"rollup": "rollup.data"},
                },
                {
                    "id": "gate",
                    "type": "approval",
                    "config": {"message": "Notify the customer team?", "expires_seconds": 3600},
                    "inputs": {"context": "summ.output"},
                },
                {
                    "id": "deliver",
                    "type": "sink",
                    "config": {
                        "target": "connection",
                        "connection_id": self.rest_id,
                        "mode": "append",
                        "path_template": "/echo",
                    },
                    "inputs": {"document": "summ.output"},
                },
                {
                    "id": "store",
                    "type": "sink",
                    "config": {"target": "run_artifact", "mode": "replace"},
                    "inputs": {"document": "summ.output"},
                },
            ],
            "edges": [
                {"from": "src", "to": "check"},
                {"from": "check", "to": "prep"},
                {"from": "prep", "to": "rollup"},
                {"from": "prep", "to": "summ"},
                {"from": "rollup", "to": "risk"},
                {"from": "summ", "to": "risk"},
                {"from": "risk", "to": "gate", "when": "true"},
                {"from": "gate", "to": "deliver"},
                {"from": "summ", "to": "store"},
            ],
            "schemas": {"Row": deepcopy(ROW_SCHEMA)},
        }
        spec.update(over)
        return spec

    # ---------------------------------------------------------------- API shortcuts
    def pipeline(
        self, key: str, spec: dict[str, Any], *, publish_it: bool = True
    ) -> dict[str, Any]:
        created: dict[str, Any] = art(self.client, self.h, "pipelines", key, spec, self.scope)
        if publish_it:
            created["version"] = publish(self.client, self.h, "pipelines", created["id"])
        return created

    def run(
        self,
        pipeline_id: str,
        params: dict[str, Any] | None = None,
        *,
        version: str | None = None,
        status: int = 202,
        key: str | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": "pipeline",
            "pipeline_id": pipeline_id,
            "workspace": "main",
            "project": "sample",
            "params": params if params is not None else {"region": "north", "threshold": 2},
            **({"version_id": version} if version else {}),
            **extra,
        }
        hdr = {**(headers or self.h), **({"Idempotency-Key": key} if key else {})}
        r = self.client.post(f"{A}/runs", json=body, headers=hdr)
        assert r.status_code == status, f"{r.status_code} {r.text}"
        out: dict[str, Any] = r.json()
        return out

    def nodes(self, run_id: str) -> dict[str, dict[str, Any]]:
        r = self.client.get(f"{A}/runs/{run_id}/nodes", headers=self.h)
        assert r.status_code == 200, r.text
        return {n["node_id"]: n for n in r.json()["items"]}

    def wait(
        self, run_id: str, statuses: set[str] | None = None, timeout: float = 60.0
    ) -> dict[str, Any]:
        return wait_run(self.client, self.h, run_id, statuses, timeout)

    def wait_parked(
        self, run_id: str, reason: str = "approval", timeout: float = 60.0
    ) -> dict[str, Any]:
        """Wait until the run is parked for `reason` (child runs are a different parking)."""

        def check() -> dict[str, Any] | None:
            run: dict[str, Any] = self.client.get(f"{A}/runs/{run_id}", headers=self.h).json()
            if run["status"] in ("completed", "failed", "cancelled", "timed_out"):
                raise AssertionError(f"run ended while waiting for {reason}: {run}")
            return run if run["waiting_reason"] == reason else None

        out: dict[str, Any] = wait_for(check, timeout)
        return out

    def pending_approvals(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.approver.client.get(
            f"{A}/approvals?status=pending", headers=self.approver
        ).json()["items"]
        return out

    def decide(self, approval_id: str, decision: str = "approve", comment: str = "ok") -> Any:
        return self.approver.client.post(
            f"{A}/approvals/{approval_id}/decide",
            headers=self.approver,
            json={"decision": decision, "comment": comment},
        )

    def posts(self) -> int:
        return len(
            [r for r in self.rest.requests if r["method"] == "POST" and r["path"] == "/echo"]
        )


def validate(client: TestClient, h: dict[str, str], pid: str) -> dict[str, Any]:
    r = client.post(f"{A}/pipelines/{pid}/draft/validate", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def error_codes(report: dict[str, Any]) -> set[str]:
    return {e["code"] for e in report["errors"]}


class HangServer:
    """Records requests; the FIRST request to /hang blocks until released (the worker is killed
    meanwhile); later requests answer immediately."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, str | None]] = []
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
                outer.log.append((self.command, self.path, self.headers.get("Idempotency-Key")))
                first = len([1 for _, p, _k in outer.log if p == "/hang"]) == 1
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

            do_GET = do_POST = do_PUT = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def count(self, method: str) -> int:
        return len([1 for m, p, _ in self.log if m == method and p == "/hang"])

    def keys(self) -> list[str | None]:
        return [k for m, p, k in self.log if p == "/hang" and m != "GET"]

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


REST_TOKEN_VALUE = REST_TOKEN


def mini(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    """A small valid pipeline spec around `nodes` / `edges`."""
    spec: dict[str, Any] = {
        "schema_version": "1",
        "params": {"type": "object"},
        "policies": {"max_parallel": 4, "deadline_seconds": 300, "on_failure": "fail_fast"},
        "nodes": nodes,
        "edges": edges,
    }
    spec.update(over)
    return spec


def sink_artifact(nid: str, source: str) -> dict[str, Any]:
    return {
        "id": nid,
        "type": "sink",
        "config": {"target": "run_artifact"},
        "inputs": {"document": source},
    }


def doc_transform(nid: str, field: str = "doc") -> dict[str, Any]:
    return {
        "id": nid,
        "type": "transform",
        "config": {"operations": [{"op": "select", "fields": ["a"]}]},
        "inputs": {"data": f"$params.{field}"},
    }


def report_of(w: World6, spec: dict[str, Any], key: str = "p") -> dict[str, Any]:
    """Create a pipeline draft with `spec` and return its validation report."""
    created = art(w.client, w.h, "pipelines", key, spec, w.scope)
    return validate(w.client, w.h, created["id"])


FAIL_SCRIPT = [
    {"match": "Tool results", "reply": "done"},
    {"match": "bad-in", "reply": tool_call_reply("not_pinned", {})},
    {"match": "need-approval", "reply": tool_call_reply("post_item", {"item": "x"})},
]


def post_tool(rest_id: str) -> dict[str, Any]:
    return rest_tool_spec(rest_id, "POST", "/echo", "write_non_idempotent", body=True)


def agent_node(w: World6, nid: str, source: str) -> dict[str, Any]:
    return {
        "id": nid,
        "type": "agent",
        "config": {"agent_version_id": f"latest:{w.stack.agent['id']}"},
        "inputs": {"input": source},
    }
