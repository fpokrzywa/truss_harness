"""P6 execution (continued): scheduling edge cases of the executor."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from shifde.runtime import state
from shifde.runtime.pipeline import PipelineRunner
from tests._support.fullapp import World
from tests._support.p5util import thread_worker
from tests._support.p6util import World6, agent_node, doc_transform, mini


def test_agent_nodes_that_fail_their_prechecks_do_not_stall_a_continue_pipeline(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    bad_agent = {
        **agent_node(w, "g", "$params.note"),
        "inputs": {"input": "$params.note", "variables": "$params.vars"},
    }
    nodes = [bad_agent, doc_transform("t")]
    params = {
        "type": "object",
        "properties": {
            "note": {"type": "string"},
            "vars": {"type": "object"},
            "doc": {"type": "object"},
        },
    }
    policies = {"max_parallel": 2, "deadline_seconds": 300, "on_failure": "continue"}
    pipe = w.pipeline("pre", mini(nodes, [], params=params, policies=policies))
    # `language` is a TRUSTED variable of the agent's prompt: pipeline data may not fill it
    run = w.run(pipe["id"], {"note": "x", "vars": {"language": "fr"}, "doc": {"a": 1}})
    with thread_worker(settings):
        done = w.wait(run["id"])
    assert done["status"] == "failed" and done["error_class"] == "validation", done
    assert "node g failed" in done["error_message"]  # a real failure, not "could not be scheduled"
    n = w.nodes(run["id"])
    assert n["g"]["status"] == "failed" and n["t"]["status"] == "succeeded"


def runner_with(nodes: dict[str, dict[str, Any]]) -> PipelineRunner:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.nodes = nodes
    return runner


def test_a_failed_attempt_with_retries_left_is_not_a_finished_node() -> None:
    read = {"type": "api_request", "config": {"method": "GET"}, "retry": {"max_attempts": 3}}
    write = {"type": "api_request", "config": {"method": "POST"}, "retry": {"max_attempts": 3}}
    tool = {"type": "tool", "config": {}, "retry": {"max_attempts": 3}}
    plain = {"type": "transform", "config": {}}
    runner = runner_with({"r": read, "w": write, "t": tool, "p": plain})

    def step(attempt: int, error_class: str) -> dict[str, Any]:
        return {"attempt": attempt, "error_class": error_class}

    assert runner._retries_left("r", step(1, state.TRANSIENT_NETWORK))  # worker died mid-retry
    assert runner._retries_left("r", step(2, state.TIMEOUT))
    assert not runner._retries_left("r", step(3, state.TIMEOUT))  # attempts exhausted
    assert not runner._retries_left("r", step(1, state.VALIDATION))  # not retryable
    assert not runner._retries_left("w", step(1, state.TRANSIENT_NETWORK))  # side effect
    assert not runner._retries_left("t", step(1, state.TRANSIENT_NETWORK))  # tool retries itself
    assert not runner._retries_left("p", step(1, state.TRANSIENT_NETWORK))  # no retry configured
