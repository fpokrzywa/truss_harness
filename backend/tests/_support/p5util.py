"""Helpers for the P5 execution tests: an in-process worker thread, a real worker subprocess,
published agent stacks and API shortcuts."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import worker_main
from shifde.core.settings import Settings
from shifde.runtime.worker import Worker
from tests._support.p3util import A, connection, fake_model_stack
from tests._support.p4util import (
    agent_spec,
    art,
    builtin_tool_spec,
    prompt_spec,
    publish,
    tenant_scope,
)

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


def tool_call_reply(name: str, args: dict[str, Any]) -> str:
    return json.dumps({"tool_calls": [{"name": name, "arguments": args}]})


def fast(settings: Settings, **over: Any) -> Settings:
    return settings.model_copy(update={"runtime_retry_base_seconds": 0.01, **over})


@contextlib.contextmanager
def thread_worker(
    settings: Settings, *, lease: int = 5, reap: float = 0.3, concurrency: int = 2
) -> Iterator[Worker]:
    """A real Worker running on a thread of the test process (fast iteration). It gets the
    composition root's internal tool registry (`worker_main.internal_tools()`), as the
    `shifde-worker` process does: this is the in-process agent-run worker of `tests/_support`
    (`SubprocessWorker`, `python -m shifde.runtime.worker`, runs without one).

    On exit it stops the worker and waits for its run thread and then for its heartbeat loop, 30
    seconds for both together. `Worker.run` does not wait for the loop, and a heartbeat still in
    progress can renew a lease after the caller has expired it."""
    worker = Worker(
        fast(settings),
        worker_id=f"test-{time.monotonic_ns()}",
        concurrency=concurrency,
        lease_seconds=lease,
        poll_ms=100,
        reap_seconds=reap,
        internal_tools=worker_main.internal_tools(),
    )
    beating: list[threading.Thread] = []  # this worker's heartbeat thread, once its loop started
    loop = worker._heartbeat_loop

    @functools.wraps(loop)
    def heartbeat_loop() -> None:
        beating.append(threading.current_thread())
        loop()

    worker._heartbeat_loop = heartbeat_loop  # type: ignore[method-assign]
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        yield worker
    finally:
        worker.request_stop()
        end = time.monotonic() + 30
        thread.join(timeout=30)
        for heartbeat in beating:
            heartbeat.join(timeout=max(0.0, end - time.monotonic()))


class SubprocessWorker:
    """`python -m shifde.runtime.worker` as a separate process against the test database."""

    def __init__(self, settings: Settings, worker_id: str, lease: int = 4) -> None:
        env = {
            **os.environ,
            "SHIFDE_ENV": "test",
            "SHIFDE_DATABASE_URL": settings.database_url,
            "SHIFDE_SECRET_KEYS": settings.secret_keys.get_secret_value()
            if settings.secret_keys
            else "",
            "SHIFDE_API_KEY_PEPPER": settings.api_key_pepper.get_secret_value()
            if settings.api_key_pepper
            else "",
            "SHIFDE_RUNTIME_RETRY_BASE_SECONDS": "0.01",
            "SHIFDE_LOG_LEVEL": "WARNING",
        }
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "shifde.runtime.worker",
                "--worker-id",
                worker_id,
                "--lease-seconds",
                str(lease),
                "--poll-ms",
                "100",
                "--reap-seconds",
                "0.5",
                "--concurrency",
                "2",
            ],
            env=env,
            creationflags=flags,
        )

    def kill(self) -> None:
        self.proc.kill()  # TerminateProcess / SIGKILL: no cleanup runs
        self.proc.wait(timeout=20)

    def stop(self) -> int:
        if self.proc.poll() is None:
            sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=20)
        return int(self.proc.returncode)


def wait_for(fn: Any, timeout: float = 30.0, every: float = 0.1) -> Any:  # Any: predicate result
    end = time.monotonic() + timeout
    while True:
        got = fn()
        if got:
            return got
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for a condition")
        time.sleep(every)


def get_run(client: TestClient, h: dict[str, str], run_id: str, org: str = A) -> dict[str, Any]:
    r = client.get(f"{org}/runs/{run_id}", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def wait_run(
    client: TestClient,
    h: dict[str, str],
    run_id: str,
    statuses: set[str] | None = None,
    timeout: float = 40.0,
) -> dict[str, Any]:
    want = statuses or TERMINAL

    def check() -> dict[str, Any] | None:
        run = get_run(client, h, run_id)
        return run if run["status"] in want else None

    out: dict[str, Any] = wait_for(check, timeout)
    return out


def start_run(
    client: TestClient,
    h: dict[str, str],
    agent_id: str,
    text_in: str = "hello",
    *,
    key: str | None = None,
    status: int = 202,
    variables: dict[str, Any] | None = None,
    org: str = A,
) -> dict[str, Any]:
    headers = {**h, **({"Idempotency-Key": key} if key else {})}
    body: dict[str, Any] = {
        "kind": "agent",
        "agent_id": agent_id,
        "workspace": "main",
        "project": "sample",
        "input": text_in,
        **({"variables": variables} if variables else {}),
    }
    r = client.post(f"{org}/runs", json=body, headers=headers)
    assert r.status_code == status, f"{r.status_code} {r.text}"
    out: dict[str, Any] = r.json()
    return out


def events(client: TestClient, h: dict[str, str], run_id: str) -> list[dict[str, Any]]:
    r = client.get(f"{A}/runs/{run_id}/events", headers=h)
    assert r.status_code == 200, r.text
    out: list[dict[str, Any]] = r.json()
    return out


def steps(client: TestClient, h: dict[str, str], run_id: str) -> list[dict[str, Any]]:
    r = client.get(f"{A}/runs/{run_id}/steps", headers=h)
    assert r.status_code == 200, r.text
    out: list[dict[str, Any]] = r.json()
    return out


def audit_actions(owner: Session, run_id: str | None = None) -> list[str]:
    q = "SELECT action FROM shifde.audit_events"
    params: dict[str, Any] = {}
    if run_id:
        q += " WHERE target_id = :r"
        params["r"] = run_id
    return [r[0] for r in owner.execute(text(q + " ORDER BY occurred_at"), params)]


class Stack:
    """Prompt + fake model (scripted) + builtin tool + agent, ready to run."""

    def __init__(
        self,
        client: TestClient,
        h: dict[str, str],
        owner: Session,
        *,
        script: list[dict[str, str]] | None = None,
        tool: dict[str, Any] | None = None,
        tool_key: str = "echo",
        agent_over: dict[str, Any] | None = None,
        model_key: str = "chat-a",
        conn_name: str = "fake-a",
        provider_settings: dict[str, Any] | None = None,
    ) -> None:
        self.client, self.h, self.owner = client, h, owner
        self.scope = tenant_scope(owner)
        self.script = script or []
        stack = fake_model_stack(
            client,
            h,
            model_key=model_key,
            conn_name=conn_name,
            config={"script": self.script},
            provider_settings=provider_settings,
        )
        self.model = stack["model"]
        self.connection = stack["connection"]
        self.prompt = art(client, h, "prompts", "agent-prompt", prompt_spec(), self.scope)
        publish(client, h, "prompts", self.prompt["id"])
        self.tool = art(client, h, "tools", tool_key, tool or builtin_tool_spec(), self.scope)
        self.tool_v = publish(client, h, "tools", self.tool["id"])
        spec = agent_spec(
            f"latest:{self.prompt['id']}",
            self.model,
            tool_version_ids=[f"latest:{self.tool['id']}"],
            **(agent_over or {}),
        )
        spec["execution_permissions"] = {
            "allowed_principal_types": ["user", "service_account"],
            "required_permission": "agent:execute",
        }
        self.agent = art(client, h, "agents", "runner", spec, self.scope)
        self.agent_v = publish(client, h, "agents", self.agent["id"])

    def new_agent(self, key: str, **over: Any) -> dict[str, Any]:
        """Another published agent over the same prompt and tool."""
        spec = agent_spec(
            f"latest:{self.prompt['id']}",
            self.model,
            tool_version_ids=[f"latest:{self.tool['id']}"],
            **over,
        )
        spec["execution_permissions"] = {
            "allowed_principal_types": ["user", "service_account"],
            "required_permission": "agent:execute",
        }
        created = art(self.client, self.h, "agents", key, spec, self.scope)
        publish(self.client, self.h, "agents", created["id"])
        out: dict[str, Any] = created
        return out


def rest_tool_spec(
    connection_id: str,
    method: str,
    path: str,
    side: str,
    *,
    timeout_s: float = 10,
    retry: dict[str, Any] | None = None,
    idempotency: str = "none",
    body: bool = False,
) -> dict[str, Any]:
    spec = builtin_tool_spec()
    spec.update(
        executor="rest.http",
        binding={
            "connection_id": connection_id,
            "method": method,
            "path_template": path,
            **({"body_template": {"item": "{item}"}} if body else {}),
        },
        input_schema={
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect_class=side,
        timeout_s=timeout_s,
        retry=retry or {"max_attempts": 1, "backoff_s": 0},
        idempotency=idempotency,
    )
    return spec


def rest_connection(
    client: TestClient, h: dict[str, str], base_url: str, name: str = "rest"
) -> str:
    from tests._support.p3util import REST_TOKEN, activate, secret

    conn = connection(
        client,
        h,
        {
            "name": name,
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "access_mode": "read_write",
            "config": {"base_url": base_url, "health_path": "/health"},
            "auth_method": "bearer",
            "secret_id": secret(client, h, f"{name}-token", REST_TOKEN),
        },
    )
    activate(client, h, conn["id"])
    return str(conn["id"])


def oai_models(
    client: TestClient, h: dict[str, str], server: Any, keys: list[str], name: str = "oai"
) -> dict[str, str]:
    """An active OpenAI-compatible provider (local fake server) with one model per key."""
    from tests._support.p2util import post
    from tests._support.p3util import OAI_KEY, activate, secret

    sec = secret(client, h, f"{name}-key", OAI_KEY)
    conn = connection(
        client,
        h,
        {
            "name": name,
            "connection_type": "model_api",
            "adapter_key": "openai_compatible",
            "config": {"base_url": f"{server.base_url}/v1"},
            "auth_method": "bearer",
            "secret_id": sec,
        },
    )
    activate(client, h, conn["id"])
    prov = post(
        client,
        f"{A}/model-providers",
        h,
        {"name": f"{name}-prov", "adapter_key": "openai_compatible", "connection_id": conn["id"]},
    )
    out: dict[str, str] = {}
    for key in keys:
        model = post(
            client,
            f"{A}/models",
            h,
            {
                "provider_id": prov["id"],
                "model_key": key,
                "display_name": key,
                "capabilities": ["chat"],
                "context_window": 8192,
                "max_output_tokens": 1024,
                "lifecycle": "active",
                "rate_limits": {},
            },
        )
        out[key] = str(model["id"])
    return out


def limited_user(
    client: TestClient,
    owner: Session,
    admin_h: dict[str, str],
    passwords: dict[str, str],
    email: str,
    permissions: list[str],
) -> tuple[str, str]:
    """A member whose only grants are `permissions` (custom role); returns (user id, role id)."""
    from tests._support.p2util import new_user, post

    role = post(
        client,
        f"{A}/roles",
        admin_h,
        {"key": f"r-{email.split('@')[0]}", "name": email, "permissions": permissions},
    )
    uid = new_user(client, admin_h, "demo-a", email, "a-long-password-1", [role["id"]])
    passwords[email] = "a-long-password-1"
    return uid, str(role["id"])


BASE_PERMS = [
    "workspace:read",
    "project:read",
    "agent:read",
    "agent:execute",
    "prompt:read",
    "tool:read",
    "tool:invoke",
    "model:read",
    "model:invoke",
    "run:read",
    "run:cancel",
    "approval:read",
    "connection:read",
    "connection:use",
    "connection:write",
]


class Hdr(dict[str, str]):
    """CSRF headers of a user who has a session of their own (separate cookie jar), so two
    people can act in one test without logging each other out."""

    client: TestClient


def user_session(client: TestClient, passwords: dict[str, str], email: str) -> Hdr:
    from tests._support.fullapp import ORIGIN
    from tests._support.p2util import login

    own = TestClient(client.app, base_url="https://testserver", headers={"Origin": ORIGIN})
    hdr = Hdr(login(own, passwords, email))
    hdr.client = own
    return hdr
