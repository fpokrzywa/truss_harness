"""P6 review round 2: live-path approvals, rejection under continue, orphan children, parking
races, indeterminate records."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from shifde.runtime import lifecycle, state
from shifde.runtime.pipeline import NodeError, PipelineRunner
from tests._support.fullapp import World
from tests._support.p5util import thread_worker, wait_for
from tests._support.p6util import (
    FAIL_SCRIPT,
    World6,
    agent_node,
    doc_transform,
    mini,
    post_tool,
    sink_artifact,
)
from tests.app.test_p6_review1 import PARAMS, approval_gate, children, deliver


def build(
    client: TestClient,
    world: World,
    owner: Session,
    rest: RestTestServer,
    sample: str,
    **kw: Any,
) -> World6:
    return World6(client, world, owner, rest, sample, **kw)


def test_a_side_effect_without_model_output_upstream_still_runs_on_the_unguarded_branch(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = build(client, world, owner_session, rest_server, sample_db_url)
    nodes = [
        {
            "id": "q",
            "type": "db_query",
            "config": {"connection_id": w.pg, "sql": "SELECT 1 AS n"},
            "inputs": {},
        },
        {
            "id": "c",
            "type": "condition",
            "config": {"expression": "inputs.r[0].n > 5"},
            "inputs": {"r": "q.rows"},
        },
        approval_gate("g"),
        deliver(w, "s", "q.rows"),
    ]
    edges = [
        {"from": "q", "to": "c"},
        {"from": "c", "to": "g", "when": "true"},
        {"from": "g", "to": "s"},
        {"from": "c", "to": "s", "when": "false"},
    ]
    pipe = w.pipeline("branch-a", mini(nodes, edges, params=PARAMS))
    run = w.run(pipe["id"], {})
    with thread_worker(settings):
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    n = w.nodes(run["id"])
    assert n["g"]["status"] == "skipped" and n["s"]["status"] == "succeeded"
    assert w.posts() == 1  # the low-value branch delivers without an approval


@pytest.mark.parametrize("go", [True, False])
def test_b_the_live_approved_branch_delivers_exactly_once(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
    go: bool,
) -> None:
    w = build(client, world, owner_session, rest_server, sample_db_url, script=FAIL_SCRIPT)
    nodes = [
        agent_node(w, "a", "$params.t"),
        {"id": "c", "type": "condition", "config": {"expression": "params.go"}, "inputs": {}},
        approval_gate("g1"),
        approval_gate("g2"),
        deliver(w, "s", "a.output"),
    ]
    edges = [
        {"from": "a", "to": "c"},
        {"from": "c", "to": "g1", "when": "true"},
        {"from": "c", "to": "g2", "when": "false"},
        {"from": "g1", "to": "s"},
        {"from": "g2", "to": "s"},
    ]
    pipe = w.pipeline("branch-b", mini(nodes, edges, params=PARAMS))
    run = w.run(pipe["id"], {"t": "plain", "go": go})
    with thread_worker(settings):
        w.wait_parked(run["id"])
        [appr] = w.pending_approvals()
        assert w.posts() == 0
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    n = w.nodes(run["id"])
    assert n["s"]["status"] == "succeeded" and n["g1" if go else "g2"]["status"] == "succeeded"
    assert w.posts() == 1


def test_a_rejection_under_continue_ends_the_run_before_dependants_run(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = build(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        script=FAIL_SCRIPT,
        tool=post_tool,
        tool_key="post_item",
    )
    nodes = [agent_node(w, "h", "$params.t"), approval_gate("g"), sink_artifact("st", "h.output")]
    policies = {"max_parallel": 4, "deadline_seconds": 300, "on_failure": "continue"}
    pipe = w.pipeline(
        "rej", mini(nodes, [{"from": "h", "to": "st"}], params=PARAMS, policies=policies)
    )
    run = w.run(pipe["id"], {"t": "need-approval"})
    with thread_worker(settings, reap=3600):
        wait_for(lambda: len(w.pending_approvals()) == 2, 40)
        [gate] = [a for a in w.pending_approvals() if a["reason"] == "pipeline_node"]
        [tool] = [a for a in w.pending_approvals() if a["reason"] == "tool_side_effect"]
        # the rejection is recorded while the run is parked on its child (not through the API,
        # which would end the run itself)
        owner_session.execute(
            text(
                "UPDATE shifde.approvals SET status = 'rejected', decision = 'reject', "
                "decided_by = (SELECT id FROM shifde.users LIMIT 1), decided_at = now() "
                "WHERE id = :i"
            ),
            {"i": gate["id"]},
        )
        owner_session.commit()
        assert w.decide(tool["id"]).status_code == 200  # the child completes, the parent wakes
        done = w.wait(run["id"])
    assert done["status"] == "failed" and done["error_class"] == "approval_rejected", done
    assert w.nodes(run["id"])["st"]["status"] != "succeeded"


def test_a_child_of_an_ended_pipeline_is_cancelled_when_it_is_claimed(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = build(
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
        "orphan", mini([agent_node(w, "b", "$params.t"), approval_gate("g")], [], params=PARAMS)
    )
    run = w.run(pipe["id"], {"t": "need-approval"})
    with thread_worker(settings, reap=3600):
        wait_for(lambda: len(w.pending_approvals()) == 2, 40)
        # the API died after the decision committed and before the child sweep
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
    assert w.posts() == 0


def test_a_child_finishing_before_the_parent_parks_on_an_approval_wakes_it(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = build(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        script=FAIL_SCRIPT,
        tool=post_tool,
        tool_key="post_item",
    )
    params = {"type": "object", "properties": {"t": {"type": "string"}, "d": {"type": "string"}}}
    nodes = [agent_node(w, "ok", "$params.t"), agent_node(w, "b", "$params.d"), approval_gate("g")]
    pipe = w.pipeline("park2", mini(nodes, [], params=params))
    run = w.run(pipe["id"], {"t": "plain", "d": "need-approval"})
    real = lifecycle.finalize
    fired: list[int] = []

    def racing(ctx: Any, to_status: str, **kw: Any) -> None:
        if to_status == state.WAITING and kw.get("waiting_reason") == "approval" and not fired:
            fired.append(1)  # child b finishes after this advance read it, before it parks

            def tool_approvals() -> list[dict[str, Any]]:
                return [a for a in w.pending_approvals() if a["reason"] == "tool_side_effect"]

            wait_for(tool_approvals, 30)
            for appr in tool_approvals():
                w.decide(appr["id"])
            wait_for(lambda: "completed" in children(owner_session, run["id"]), 30)
        real(ctx, to_status, **kw)

    monkeypatch.setattr(lifecycle, "finalize", racing)
    with thread_worker(settings, reap=3600):
        wait_for(lambda: fired, 40)
        wait_for(lambda: w.nodes(run["id"])["b"]["status"] == "succeeded", 30)  # collected
        w.client.post(f"/api/v1/orgs/demo-a/runs/{run['id']}/cancel", headers=w.h)


def test_an_indeterminate_write_step_stays_the_record_when_the_node_gate_is_denied(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = build(client, world, owner_session, rest_server, sample_db_url)
    nodes = [doc_transform("t"), deliver(w, "post", "t.data")]
    pipe = w.pipeline("indet2", mini(nodes, [{"from": "t", "to": "post"}], params=PARAMS))
    run = w.run(pipe["id"], {"doc": {"a": 1}})
    seen: list[int] = []
    real_gate = PipelineRunner._node_gate

    def invoke(self: PipelineRunner, *a: Any, **k: Any) -> Any:
        seen.append(1)  # the call may have been delivered: indeterminate
        raise NodeError(state.TRANSIENT_NETWORK, "connection reset", maybe_sent=True)

    def node_gate(self: PipelineRunner, nid: str) -> None:
        if seen and nid == "post":
            raise state.GateDenied(state.AUTHZ_DENIED, "revoked", "no_binding")
        real_gate(self, nid)

    monkeypatch.setattr(PipelineRunner, "_invoke_connection", invoke)
    monkeypatch.setattr(PipelineRunner, "_node_gate", node_gate)
    with thread_worker(settings):
        w.wait_parked(run["id"], "indeterminate_side_effect")
        [appr] = w.pending_approvals()
        assert w.decide(appr["id"], "retry", "checked the target").status_code == 200
        done = w.wait(run["id"])
    # R1: the operator decided `retry`, the gate refused it before it ran, so the first delivery
    # is still unknown: the run says so and a NEW decision (epoch 1) is open
    assert done["status"] == "failed" and done["error_class"] == "indeterminate_side_effect", done
    assert done["outcome_unknown"] is True
    owner_session.rollback()
    rows = owner_session.execute(
        text(
            "SELECT status FROM shifde.run_steps WHERE run_id = :r AND node_id = 'post' "
            "ORDER BY attempt"
        ),
        {"r": run["id"]},
    ).all()
    assert [r[0] for r in rows] == ["indeterminate"]  # nothing was appended
