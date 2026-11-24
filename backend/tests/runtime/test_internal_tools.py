"""SP7-1 (design section 3): the worker's internal tool registry and the `seth.*` dispatch.

`InternalToolRegistry.of` refuses an undeclared key and is immutable; the composition root
(`worker_main`) hands `runtime.worker.run` a registry of every declared key (`assist.tools`). A worker given a registry (the
`p5util.thread_worker` wiring) runs a published `builtin.function` tool bound to `seth.board.read`
inside an agent run: the function gets the run's ids, its principal and step key, and runs on the
step's session in the run's client (A's run sees A, B's run sees B). Without a registry, or
without the key in it, the run fails with `internal_function_unavailable` and nothing runs. The
runtime never commits inside the call: a row the function writes is gone when it raises and kept
when the step succeeds. Each test has a database of its own (the full-app harness of `tests/app`).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import worker_main
from shifde.assist import tools as assist_tools
from shifde.audit import service as audit
from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.core.errors import NotFound
from shifde.core.settings import Settings
from shifde.runtime import state
from shifde.runtime import worker as worker_mod
from shifde.runtime.internal_tools import InternalToolCall, InternalToolRegistry
from shifde.runtime.worker import Worker
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, ADMIN_B, A, B, fake_model_stack
from tests._support.p4util import (
    agent_spec,
    art,
    builtin_tool_spec,
    prompt_spec,
    publish,
    set_tenant_setting,
    tenant_scope,
)
from tests._support.p5util import (
    TERMINAL,
    Hdr,
    fast,
    get_run,
    start_run,
    thread_worker,
    tool_call_reply,
    user_session,
    wait_for,
)
from tests.app.conftest import (
    _admin_engine,
    _dropper,
    app_db,
    app_url,
    client,
    owner_db,
    owner_engine,
    owner_session,
    owner_url,
    test_database,
    world,
)

__all__ = [
    "_admin_engine",
    "_dropper",
    "app_db",
    "app_url",
    "client",
    "owner_db",
    "owner_engine",
    "owner_session",
    "owner_url",
    "test_database",
    "world",
]

KEY = "seth.board.read"
SCRIPT = [
    {"match": "Tool results", "reply": "done"},
    {"match": "board", "reply": tool_call_reply("board", {})},
]


def _fn(call: InternalToolCall) -> Any:  # Any: tool result
    return {}


# ------------------------------------------------------------------ registry
def test_an_undeclared_key_cannot_be_registered() -> None:
    with pytest.raises(ValueError, match="not declared"):
        InternalToolRegistry.of({"seth.nope.read": _fn})


def test_the_registry_is_immutable() -> None:
    functions = {KEY: _fn}
    registry = InternalToolRegistry.of(functions)
    functions["seth.costs.read"] = _fn  # the source mapping is copied
    assert list(registry) == [KEY] and len(registry) == 1
    with pytest.raises(TypeError):
        registry["seth.costs.read"] = _fn  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        registry._functions["seth.costs.read"] = _fn  # type: ignore[index]
    assert "seth.costs.read" not in registry


def test_the_composition_root_registers_every_declared_function() -> None:
    registry = worker_main.internal_tools()
    assert isinstance(registry, InternalToolRegistry)
    assert set(registry) == set(INTERNAL_FUNCTIONS)
    assert dict(registry) == dict(assist_tools.REGISTRY)


def test_the_composition_root_passes_the_registry_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(argv: list[str] | None = None, **kwargs: Any) -> int:
        seen.append({"argv": argv, **kwargs})
        return 0

    monkeypatch.setattr(worker_mod, "run", fake_run)
    assert worker_main.run([]) == 0
    assert len(seen) == 1
    assert isinstance(seen[0]["internal_tools"], InternalToolRegistry)
    assert dict(seen[0]["job_handlers"]) == dict(worker_main.job_handlers())


# ------------------------------------------------------------------ in a run
def _seth_tool() -> dict[str, Any]:
    decl = INTERNAL_FUNCTIONS[KEY]
    return builtin_tool_spec(
        binding={"function_key": KEY},
        input_schema=decl.input_schema,
        output_schema=decl.output_schema,
        side_effect_class=decl.side_effect_class,
    )


def _agent(h: Hdr, owner: Session, org: str, slug: str) -> str:
    """A published agent in `org` whose scripted fake model calls the published `seth.board.read`
    tool `board` once, then answers."""
    c = h.client
    scope = tenant_scope(owner, slug)
    model = fake_model_stack(c, h, model_key="chat-a", conn_name="fake-a",
                             config={"script": SCRIPT}, org=org)["model"]  # fmt: skip
    prompt = art(c, h, "prompts", "agent-prompt", prompt_spec(), scope, org=org)
    publish(c, h, "prompts", prompt["id"], org=org)
    tool = art(c, h, "tools", "board", _seth_tool(), scope, org=org)
    publish(c, h, "tools", tool["id"], org=org)
    spec = agent_spec(f"latest:{prompt['id']}", model, tool_version_ids=[f"latest:{tool['id']}"])
    agent = art(c, h, "agents", "runner", spec, scope, org=org)
    publish(c, h, "agents", agent["id"], org=org)
    return str(agent["id"])


def _run(h: Hdr, org: str, agent: str) -> str:
    run = start_run(h.client, h, agent, "read the board", org=org)
    return str(run["id"])


def _ended(h: Hdr, org: str, run: str) -> dict[str, Any]:
    out: dict[str, Any] = wait_for(
        lambda: (r := get_run(h.client, h, run, org)) and r["status"] in TERMINAL and r, 40.0
    )
    return out


@contextlib.contextmanager
def _worker(settings: Settings, registry: InternalToolRegistry | None) -> Iterator[Worker]:
    w = Worker(fast(settings), worker_id=f"test-{time.monotonic_ns()}", concurrency=2,
               lease_seconds=5, poll_ms=100, reap_seconds=0.3,
               internal_tools=registry)  # fmt: skip
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        yield w
    finally:
        w.request_stop()
        t.join(timeout=30)


def _run_row(owner: Session, run: str) -> Any:  # Any: row
    owner.rollback()
    return owner.execute(
        text("SELECT tenant_id, workspace_id, project_id, initiated_by_principal_id, status, "
             "error_class FROM shifde.runs WHERE id = :r"),
        {"r": run},
    ).one()  # fmt: skip


def _tool_steps(owner: Session, run: str) -> list[Any]:  # Any: rows
    owner.rollback()
    return list(
        owner.execute(
            text("SELECT s.status, s.idempotency_key, a.content, i.side_effect_class, i.status "
                 "FROM shifde.run_steps s "
                 "LEFT JOIN shifde.run_artifacts a ON a.id = s.output_ref "
                 "LEFT JOIN shifde.tool_invocations i ON i.id = s.tool_invocation_id "
                 "WHERE s.run_id = :r AND s.kind = 'tool_call' ORDER BY s.seq, s.attempt"),
            {"r": run},
        ).all()
    )  # fmt: skip


def test_a_seth_tool_runs_inside_a_run_in_its_own_client(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for slug in ("demo-a", "demo-b"):
        set_tenant_setting(owner_session, slug, "capture_content", "full")
    calls: list[tuple[InternalToolCall, str]] = []

    def board(call: InternalToolCall) -> Any:  # Any: tool result
        seen = call.session.execute(text("SELECT current_setting('app.tenant_id')")).scalar_one()
        calls.append((call, seen))
        return {"columns": ["todo"], "truncated": False}

    monkeypatch.setattr(
        worker_main, "internal_tools", lambda: InternalToolRegistry.of({KEY: board})
    )
    a = user_session(client, world.passwords, ADMIN)
    b = user_session(client, world.passwords, ADMIN_B)
    runs = {
        "demo-a": _run(a, A, _agent(a, owner_session, A, "demo-a")),
        "demo-b": _run(b, B, _agent(b, owner_session, B, "demo-b")),
    }
    with thread_worker(settings):  # passes worker_main.internal_tools()
        assert _ended(a, A, runs["demo-a"])["status"] == "completed"
        assert _ended(b, B, runs["demo-b"])["status"] == "completed"
    assert len(calls) == 2
    for slug, run in runs.items():
        row = _run_row(owner_session, run)
        call, seen = next(c for c in calls if str(c[0].run_id) == run)
        assert (call.tenant_id, call.workspace_id, call.project_id, call.run_id) == (
            row.tenant_id, row.workspace_id, row.project_id, UUID(run),
        )  # fmt: skip
        assert call.scope.tenant_id == row.tenant_id
        assert call.scope.principal.user_id == row.initiated_by_principal_id
        assert call.args == {}
        assert seen == str(row.tenant_id), f"{slug}'s run saw another client's context"
        [step] = _tool_steps(owner_session, run)
        assert step[0] == "succeeded" and step[1] == call.step_key
        assert step[2] == {"columns": ["todo"], "truncated": False}
        assert (step[3], step[4]) == ("read", "succeeded")  # journaled, class read
    assert calls[0][1] != calls[1][1]


def test_a_worker_of_the_composition_root_runs_the_real_board_function(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    set_tenant_setting(owner_session, "demo-a", "capture_content", "full")
    a = user_session(client, world.passwords, ADMIN)
    run = _run(a, A, _agent(a, owner_session, A, "demo-a"))
    with thread_worker(settings):  # worker_main.internal_tools(): `assist.tools.REGISTRY`
        assert _ended(a, A, run)["status"] == "completed"
    [step] = _tool_steps(owner_session, run)
    assert step[0] == "succeeded" and step[2]["truncated"] is False
    [project] = step[2]["board"]["projects"]
    assert project["project"]["id"] == str(_run_row(owner_session, run).project_id)


@pytest.mark.parametrize("registered", ["nothing", "another key"])
def test_a_missing_function_fails_the_run_unrun(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    registered: str,
) -> None:
    called: list[InternalToolCall] = []
    registry = (
        None
        if registered == "nothing"
        else InternalToolRegistry.of({"seth.costs.read": called.append})
    )
    a = user_session(client, world.passwords, ADMIN)
    run = _run(a, A, _agent(a, owner_session, A, "demo-a"))
    with _worker(settings, registry):
        done = _ended(a, A, run)
    assert done["status"] == "failed", done
    assert _run_row(owner_session, run).error_class == state.INTERNAL_FUNCTION_UNAVAILABLE
    assert called == []
    assert all(s[0] != "succeeded" for s in _tool_steps(owner_session, run))


@pytest.mark.parametrize("raises", [True, False])
def test_the_runtime_never_commits_inside_the_call(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    raises: bool,
) -> None:
    marker = f"internal-write-{time.monotonic_ns()}"

    def write(call: InternalToolCall) -> Any:  # Any: tool result
        audit.record(call.session, call.scope.ctx, action=marker, tenant_id=call.tenant_id,
                     workspace_id=call.workspace_id, project_id=call.project_id)  # fmt: skip
        if raises:
            raise NotFound("no such board")
        return {}

    a = user_session(client, world.passwords, ADMIN)
    run = _run(a, A, _agent(a, owner_session, A, "demo-a"))
    with _worker(settings, InternalToolRegistry.of({KEY: write})):
        done = _ended(a, A, run)
    row = _run_row(owner_session, run)
    written = owner_session.execute(
        text("SELECT count(*) FROM shifde.audit_events WHERE action = :m"), {"m": marker}
    ).scalar_one()
    if raises:
        assert (done["status"], row.error_class) == ("failed", state.AUTHZ_DENIED)
        assert written == 0
    else:
        assert done["status"] == "completed", done
        assert written == 1
        assert _tool_steps(owner_session, run)[0][0] == "succeeded"
