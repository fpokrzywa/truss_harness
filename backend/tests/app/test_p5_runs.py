"""P5 execution harness: the real path User -> Agent -> Prompt -> Model -> Tool -> Result."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN
from tests._support.p4util import set_tenant_setting
from tests._support.p5util import (
    Stack,
    get_run,
    start_run,
    steps,
    thread_worker,
    tool_call_reply,
    wait_run,
)

SCRIPT = [
    {"match": "Tool results", "reply": "The answer is HELLO-RESULT"},
    {"match": "weather", "reply": tool_call_reply("echo", {"text": "HELLO-RESULT"})},
]


def test_real_path_user_agent_prompt_model_tool_result(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    set_tenant_setting(owner_session, "demo-a", "capture_content", "full")
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=SCRIPT)
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    assert run["status"] == "pending" and run["workspace"]["slug"] == "main"
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed", done
    assert done["usage"]["model_calls"] == 2 and done["usage"]["tool_calls"] == 1
    result = client.get(f"/api/v1/orgs/demo-a/runs/{run['id']}/result", headers=h).json()
    assert "HELLO-RESULT" in result["output"]
    kinds = [(s["kind"], s["status"]) for s in steps(client, h, run["id"])]
    assert kinds == [
        ("model_call", "succeeded"),
        ("tool_call", "succeeded"),
        ("model_call", "succeeded"),
    ]
    assert get_run(client, h, run["id"])["trace_id"]


def test_result_is_redacted_unless_content_capture_is_full(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=SCRIPT)
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        wait_run(client, h, run["id"])
    result = client.get(f"/api/v1/orgs/demo-a/runs/{run['id']}/result", headers=h).json()
    assert result["redacted"] is True and result["output"] is None


def test_resolved_agent_view_exposes_the_prompt_version_for_the_run_form(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session, script=SCRIPT)
    url = f"/api/v1/orgs/demo-a/agents/{stack.agent['id']}/versions/{stack.agent_v['id']}/resolved"
    pv = client.get(url, headers=h).json()["prompt_version"]
    assert pv["id"] and pv["version_no"] == 1
    assert [v["name"] for v in pv["spec"]["variables"]] == ["message", "language"]
