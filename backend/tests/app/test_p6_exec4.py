"""P6 execution (continued): tool nodes behind the tool approval gate, bounded parallelism."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p5util import rest_tool_spec, thread_worker
from tests._support.p6util import FAIL_SCRIPT, World6, agent_node, doc_transform, mini


def test_a_write_tool_node_waits_for_the_tool_approval_and_runs_exactly_once(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        tool=lambda rid: rest_tool_spec(rid, "POST", "/echo", "write_non_idempotent", body=True),
        tool_key="post_item",
    )
    nodes = [
        {
            "id": "post",
            "type": "tool",
            "config": {"tool_version_id": f"latest:{w.stack.tool['id']}"},
            "inputs": {"args": "$params.args"},
        }
    ]
    params = {"type": "object", "properties": {"args": {"type": "object"}}}
    pipe = w.pipeline("tool-write", mini(nodes, [], params=params))
    run = w.run(pipe["id"], {"args": {"item": "x1"}})
    with thread_worker(settings):
        w.wait_parked(run["id"])
        assert w.posts() == 0
        assert w.nodes(run["id"])["post"]["status"] == "waiting_for_approval"
        [appr] = w.pending_approvals()
        assert appr["reason"] == "tool_side_effect" and appr["tool"]["key"] == "post_item"
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    assert w.posts() == 1
    assert done["usage"]["tool_calls"] == 1


def test_max_parallel_bounds_the_concurrent_child_runs(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url, script=FAIL_SCRIPT)
    nodes = [agent_node(w, "a", "$params.t"), agent_node(w, "b", "$params.t"), doc_transform("c")]
    params = {
        "type": "object",
        "properties": {"t": {"type": "string"}, "doc": {"type": "object"}},
    }
    policies = {"max_parallel": 1, "deadline_seconds": 300, "on_failure": "fail_fast"}
    pipe = w.pipeline("serial", mini(nodes, [], params=params, policies=policies))
    run = w.run(pipe["id"], {"t": "plain text", "doc": {"a": 1}})
    with thread_worker(settings):
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    rows = owner_session.execute(
        text(
            "SELECT node_id, created_at, finished_at FROM shifde.runs "
            "WHERE parent_run_id = :r ORDER BY created_at"
        ),
        {"r": run["id"]},
    ).all()
    assert [r[0] for r in rows] == ["a", "b"]
    assert rows[1][1] >= rows[0][2]  # b only started after a had finished
