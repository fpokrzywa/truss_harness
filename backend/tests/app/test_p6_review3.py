"""P6 review round 3: model output laundered through pass-through nodes; orphan-cancel audit."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p5util import thread_worker, wait_for
from tests._support.p6util import FAIL_SCRIPT, World6, agent_node, mini, post_tool, report_of
from tests.app.test_p6_review1 import PARAMS, approval_gate, children, deliver


def middle(kind: str) -> dict[str, Any]:
    """A pass-through node fed (by edge) from the agent a; it forwards `$params.doc` or a.output."""
    if kind == "validate":
        return {
            "id": "t",
            "type": "validate",
            "config": {"schema": {}, "on_invalid": "fail"},
            "inputs": {"data": "a.output"},
        }
    return {
        "id": "t",
        "type": "transform",
        "config": {"operations": [{"op": "map", "field": "m", "expression": "row.a == 1"}]},
        "inputs": {"data": "$params.doc"},
    }


def graph(
    w: World6, kind: str, *, bypass: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = [
        agent_node(w, "a", "$params.t"),
        middle(kind),
        {"id": "c", "type": "condition", "config": {"expression": "params.go"}, "inputs": {}},
        approval_gate("g"),
        approval_gate("g2"),
        deliver(w, "s", "t.data"),
    ]
    edges = [
        {"from": "a", "to": "t"},
        {"from": "t", "to": "c"},
        {"from": "c", "to": "g", "when": "true"},
        {"from": "g", "to": "s"},
    ]
    if bypass == "transform":  # lead's repro: g2 -> x -> s
        nodes.append(
            {
                "id": "x",
                "type": "transform",
                "config": {"operations": [{"op": "select", "fields": ["a"]}]},
                "inputs": {"data": "$params.doc"},
            }
        )
        edges += [{"from": "g2", "to": "x"}, {"from": "x", "to": "s"}]
    else:  # g2 -> s directly: valid, but only g2 is granted at runtime
        edges.append({"from": "g2", "to": "s"})
    return nodes, edges


@pytest.mark.parametrize("kind", ["validate", "transform"])
def test_a_bypass_through_a_pass_through_node_does_not_validate(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
    kind: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url, script=FAIL_SCRIPT)
    nodes, edges = graph(w, kind, bypass="transform")
    report = report_of(w, mini(nodes, edges, params=PARAMS))
    assert any(
        e["code"] == "approval_required_before_side_effect" and e["node_id"] == "s"
        for e in report["errors"]
    ), report


@pytest.mark.parametrize("kind", ["validate", "transform"])
def test_model_output_laundered_through_a_pass_through_node_is_not_delivered_unapproved(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
    kind: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url, script=FAIL_SCRIPT)
    nodes, edges = graph(w, kind, bypass="direct")
    pipe = w.pipeline(f"launder-{kind}", mini(nodes, edges, params=PARAMS))
    run = w.run(pipe["id"], {"t": "plain", "go": False, "doc": {"a": 1}})
    with thread_worker(settings):
        w.wait_parked(run["id"])
        [appr] = w.pending_approvals()  # g2 only: the condition was false
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    n = w.nodes(run["id"])
    assert n["g"]["status"] == "skipped" and n["s"]["status"] == "skipped"
    assert w.posts() == 0


def test_cancelling_an_orphaned_child_is_audited_and_journaled(
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
        script=FAIL_SCRIPT,
        tool=post_tool,
        tool_key="post_item",
    )
    pipe = w.pipeline(
        "orphan3", mini([agent_node(w, "b", "$params.t"), approval_gate("g")], [], params=PARAMS)
    )
    run = w.run(pipe["id"], {"t": "need-approval"})
    with thread_worker(settings, reap=3600):
        wait_for(lambda: len(w.pending_approvals()) == 2, 40)
        owner_session.execute(
            text(
                "UPDATE shifde.runs SET status = 'failed', state_version = state_version + 1, "
                "finished_at = now(), error_class = 'approval_rejected', lease_token = NULL, "
                "lease_job_id = NULL WHERE id = :r"
            ),
            {"r": run["id"]},
        )
        owner_session.commit()
        [tool] = [a for a in w.pending_approvals() if a["reason"] == "tool_side_effect"]
        assert w.decide(tool["id"]).status_code == 200
        wait_for(lambda: children(owner_session, run["id"]) == ["cancelled"], 30)
    owner_session.rollback()
    child = owner_session.execute(
        text("SELECT id FROM shifde.runs WHERE parent_run_id = :r"), {"r": run["id"]}
    ).scalar_one()
    audited = owner_session.execute(
        text(
            "SELECT details->>'reason' FROM shifde.audit_events "
            "WHERE target_id = :c AND action = 'run.cancelled'"
        ),
        {"c": str(child)},
    ).all()
    assert [a[0] for a in audited] == ["parent_ended"]
    events = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.run_events WHERE run_id = :c "
            "AND type = 'state_change' AND to_status = 'cancelled'"
        ),
        {"c": str(child)},
    ).scalar_one()
    assert events == 1
