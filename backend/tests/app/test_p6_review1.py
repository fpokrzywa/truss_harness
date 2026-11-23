"""P6 review round 1: one test per finding (each fails on the code before the fix)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from shifde.runtime import lifecycle, state
from shifde.runtime.pipeline import PipelineRunner
from tests._support.fullapp import World
from tests._support.p2util import login, post, system_role
from tests._support.p3util import A
from tests._support.p4util import edit, publish
from tests._support.p5util import limited_user, thread_worker, wait_for, wait_run
from tests._support.p6util import (
    FAIL_SCRIPT,
    World6,
    agent_node,
    doc_transform,
    mini,
    post_tool,
    report_of,
)


def world6(
    client: TestClient,
    world: World,
    owner: Session,
    rest: RestTestServer,
    sample: str,
    **kw: Any,
) -> World6:
    return World6(client, world, owner, rest, sample, **kw)


def children(owner: Session, run_id: str) -> list[str]:
    owner.rollback()
    rows = owner.execute(
        text("SELECT status FROM shifde.runs WHERE parent_run_id = :r ORDER BY node_id"),
        {"r": run_id},
    ).all()
    return [r[0] for r in rows]


def approval_gate(nid: str) -> dict[str, Any]:
    return {"id": nid, "type": "approval", "config": {}, "inputs": {}}


def deliver(w: World6, nid: str, source: str) -> dict[str, Any]:
    return {
        "id": nid,
        "type": "sink",
        "config": {
            "target": "connection",
            "connection_id": w.rest_id,
            "mode": "append",
            "path_template": "/echo",
        },
        "inputs": {"document": source},
    }


PARAMS = {
    "type": "object",
    "properties": {
        "t": {"type": "string"},
        "go": {"type": "boolean"},
        "doc": {"type": "object"},
        "args": {"type": "object"},
    },
}


# ------------------------------------------------------------------ 1. rule 8 across OR-joined edges
def test_f1_an_unguarded_edge_into_a_side_effect_node_does_not_validate(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url)
    nodes = [
        agent_node(w, "a", "$params.t"),
        {"id": "c", "type": "condition", "config": {"expression": "params.go"}, "inputs": {}},
        approval_gate("g"),
        doc_transform("t"),
        deliver(w, "s", "a.output"),
    ]
    edges = [
        {"from": "a", "to": "c"},
        {"from": "c", "to": "g", "when": "true"},
        {"from": "g", "to": "s"},
        {"from": "t", "to": "s"},  # the bypass
    ]
    report = report_of(w, mini(nodes, edges, params=PARAMS))
    assert any(
        e["code"] == "approval_required_before_side_effect" and e["node_id"] == "s"
        for e in report["errors"]
    )


def test_f1_a_side_effect_runs_only_when_every_upstream_approval_was_granted(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url, script=FAIL_SCRIPT)
    nodes = [
        agent_node(w, "a", "$params.t"),
        {"id": "c", "type": "condition", "config": {"expression": "params.go"}, "inputs": {}},
        approval_gate("g"),
        approval_gate("g2"),
        deliver(w, "s", "a.output"),
    ]
    edges = [
        {"from": "a", "to": "c"},
        {"from": "c", "to": "g", "when": "true"},
        {"from": "g", "to": "s"},
        {"from": "g2", "to": "s"},  # a live edge that does not pass through g
    ]
    pipe = w.pipeline("or-join", mini(nodes, edges, params=PARAMS))
    run = w.run(pipe["id"], {"t": "plain", "go": False})
    with thread_worker(settings):
        w.wait_parked(run["id"])
        [appr] = w.pending_approvals()  # only g2: the condition was false, g is skipped
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    n = w.nodes(run["id"])
    assert n["g"]["status"] == "skipped" and n["s"]["status"] == "skipped"
    assert w.posts() == 0  # the agent output was NOT delivered without g's approval


# ------------------------------------------------------------------ 2. continue keeps its failure
def test_f2_a_continue_failure_survives_later_advances(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url)
    bad = {
        "id": "bad",
        "type": "transform",
        "config": {"operations": [{"op": "cast", "field": "a", "to": "integer"}]},
        "inputs": {"data": "$params.doc"},
    }
    policies = {"max_parallel": 2, "deadline_seconds": 300, "on_failure": "continue"}
    pipe = w.pipeline(
        "cont2", mini([bad, approval_gate("g")], [], params=PARAMS, policies=policies)
    )
    run = w.run(pipe["id"], {"doc": {"a": "x"}})
    with thread_worker(settings):
        w.wait_parked(run["id"])  # the failure happened in an EARLIER advance than the end
        [appr] = w.pending_approvals()
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "failed" and done["error_class"] == "validation", done
    assert "node bad failed" in done["error_message"]


# ------------------------------------------------------------------ 3. lost wake-up
def test_f3_a_child_finishing_before_the_parent_parks_does_not_strand_the_run(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world6(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        script=FAIL_SCRIPT,
        tool=post_tool,
        tool_key="post_item",
    )
    nodes = [agent_node(w, "a", "$params.t"), agent_node(w, "b", "$params.doc")]
    params = {"type": "object", "properties": {"t": {"type": "string"}, "doc": {"type": "string"}}}
    pipe = w.pipeline("race", mini(nodes, [], params=params))
    run = w.run(pipe["id"], {"t": "plain", "doc": "need-approval"})
    real = lifecycle.finalize
    fired: list[int] = []

    def racing(ctx: Any, to_status: str, **kw: Any) -> None:
        if to_status == state.WAITING and kw.get("waiting_reason") == "child_runs" and not fired:
            fired.append(1)  # child b finishes AFTER this advance read it and BEFORE it parks
            wait_for(w.pending_approvals, 30)
            for appr in w.pending_approvals():
                w.decide(appr["id"])
            wait_for(lambda: set(children(owner_session, run["id"])) == {"completed"}, 30)
        real(ctx, to_status, **kw)

    monkeypatch.setattr(lifecycle, "finalize", racing)
    with thread_worker(settings, reap=3600):  # the reaper must not rescue the run
        done = w.wait(run["id"], timeout=40)
    assert fired and done["status"] == "completed", done


# ------------------------------------------------------------------ 4. per-node tool approval
def test_f4_an_unrelated_approval_node_does_not_waive_a_tool_nodes_own_approval(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        tool=post_tool,
        tool_key="post_item",
    )
    tool = {
        "id": "post",
        "type": "tool",
        "config": {"tool_version_id": f"latest:{w.stack.tool['id']}"},
        "inputs": {"args": "$params.args"},
    }
    pipe = w.pipeline("tool-gate", mini([approval_gate("g"), tool], [], params=PARAMS))
    run = w.run(pipe["id"], {"args": {"item": "x"}})
    with thread_worker(settings):
        wait_for(lambda: len(w.pending_approvals()) == 2, 30)
        assert w.posts() == 0
        assert {a["reason"] for a in w.pending_approvals()} == {"pipeline_node", "tool_side_effect"}
        for appr in w.pending_approvals():
            assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed" and w.posts() == 1


# ------------------------------------------------------------------ 5. agent nodes pass the node gate
def test_f5_a_revoked_pipeline_permission_stops_the_next_agent_node(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url, script=FAIL_SCRIPT)
    nodes = [approval_gate("g"), agent_node(w, "ag", "$params.t")]
    pipe = w.pipeline("agent-gate", mini(nodes, [{"from": "g", "to": "ag"}], params=PARAMS))
    _, role = limited_user(
        client,
        owner_session,
        w.h,
        world.passwords,
        "runner2@demo-a.local",
        [
            "workspace:read",
            "project:read",
            "pipeline:read",
            "pipeline:execute",
            "run:read",
            "agent:read",
            "agent:execute",
            "model:invoke",
        ],
    )
    uh = login(client, world.passwords, "runner2@demo-a.local")
    run = w.run(pipe["id"], {"t": "plain"}, headers=uh)
    with thread_worker(settings):
        w.wait_parked(run["id"])
        owner_session.execute(
            text(
                "DELETE FROM shifde.role_permissions WHERE role_id = :r "
                "AND permission_key = 'pipeline:execute'"
            ),
            {"r": role},
        )
        owner_session.commit()
        [appr] = w.pending_approvals()
        assert w.decide(appr["id"]).status_code == 200
        done = wait_run(client, uh, run["id"])
    assert done["status"] == "failed" and done["error_class"] == "authz_denied", done
    assert children(owner_session, run["id"]) == []  # no agent run was started


# ------------------------------------------------------------------ 6. draft test run snapshot
def test_f6_a_parked_test_run_keeps_its_snapshot_and_test_gate_when_the_draft_is_published(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url)
    spec = mini([doc_transform("t"), approval_gate("g")], [{"from": "t", "to": "g"}], params=PARAMS)
    pipe = w.pipeline("snap", spec)
    edit(client, w.h, "pipelines", pipe["id"], spec, row_version=0)
    run = w.run(pipe["id"], {"doc": {"a": 1}}, version="draft")
    with thread_worker(settings):
        w.wait_parked(run["id"])
        publish(client, w.h, "pipelines", pipe["id"], "v2")  # the draft becomes a published version
        [appr] = w.pending_approvals()
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed" and done["is_test"] is True, done
    owner_session.rollback()
    actions = {
        r[0]
        for r in owner_session.execute(
            text("SELECT action FROM shifde.policy_decisions WHERE run_id = :r"), {"r": run["id"]}
        )
    }
    assert actions == {"pipeline:test"}  # never flipped to pipeline:execute


# ------------------------------------------------------------------ 7. rejection cancels children
def test_f7_rejecting_a_pipeline_approval_cancels_the_running_agent_children(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        script=FAIL_SCRIPT,
        tool=post_tool,
        tool_key="post_item",
    )
    nodes = [agent_node(w, "b", "$params.t"), approval_gate("g")]
    pipe = w.pipeline("reject", mini(nodes, [], params=PARAMS))
    run = w.run(pipe["id"], {"t": "need-approval"})
    with thread_worker(settings):
        wait_for(lambda: len(w.pending_approvals()) == 2, 40)  # g and the child's tool approval
        # an approval commits before its run parks (steps._create_approval, then
        # lifecycle.finalize): wait until the child and the pipeline are both waiting_for_approval
        seen: dict[str, Any] = {}

        def parked() -> bool:
            seen["children"] = children(owner_session, run["id"])
            seen["run"] = w.client.get(f"{A}/runs/{run['id']}", headers=w.h).json()["status"]
            return seen["children"] == ["waiting_for_approval"] and (
                seen["run"] == "waiting_for_approval"
            )

        try:
            wait_for(parked, 60)
        except AssertionError:
            raise AssertionError(
                f"run {run['id']} not parked: status {seen.get('run')}, "
                f"children {seen.get('children')}"
            ) from None
        assert children(owner_session, run["id"]) == ["waiting_for_approval"]
        [gate] = [a for a in w.pending_approvals() if a["reason"] == "pipeline_node"]
        assert w.decide(gate["id"], "reject").status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "failed" and done["error_class"] == "approval_rejected"
    assert children(owner_session, run["id"]) == ["cancelled"]  # not left running
    assert w.posts() == 0


# ------------------------------------------------------------------ 8. external data is sanitised
def test_f8_non_finite_numbers_from_a_database_do_not_crash_the_run(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url)
    node = {
        "id": "q",
        "type": "db_query",
        "config": {
            "connection_id": w.pg,
            "sql": "SELECT 'NaN'::float8 AS v, 'Infinity'::float8 AS i",
        },
        "inputs": {},
    }
    pipe = w.pipeline("nan", mini([node], []))
    run = w.run(pipe["id"])
    with thread_worker(settings):
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    assert w.nodes(run["id"])["q"]["output_preview"]["rows"] == [{"v": None, "i": None}]


# ------------------------------------------------------------------ 10. recovered write + gate denial
def test_f10_a_recovered_write_step_whose_gate_is_denied_is_indeterminate_not_failed(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url)
    nodes = [doc_transform("t"), deliver(w, "post", "t.data")]
    pipe = w.pipeline("recovered", mini(nodes, [{"from": "t", "to": "post"}], params=PARAMS))
    run = w.run(pipe["id"], {"doc": {"a": 1}})
    denied: list[int] = []
    real_gate = PipelineRunner._node_gate
    real_invoke = PipelineRunner._invoke_connection

    def invoke(self: PipelineRunner, *a: Any, **k: Any) -> Any:
        if not denied:
            denied.append(1)  # the worker stops right after the write-ahead step was committed
            raise state.Shutdown()
        return real_invoke(self, *a, **k)

    def node_gate(self: PipelineRunner, nid: str) -> None:
        if denied and nid == "post" and len(denied) == 1:
            denied.append(2)
            raise state.GateDenied(state.AUTHZ_DENIED, "revoked", "no_binding")
        real_gate(self, nid)

    monkeypatch.setattr(PipelineRunner, "_invoke_connection", invoke)
    monkeypatch.setattr(PipelineRunner, "_node_gate", node_gate)
    with thread_worker(settings):
        done = w.wait(run["id"])
    # R1: the write may have been delivered, so the denied recovery ends the run as an unknown
    # outcome with the decision open (not as a plain authorization failure)
    assert done["status"] == "failed" and done["error_class"] == "indeterminate_side_effect", done
    assert done["outcome_unknown"] is True
    assert [a["reason"] for a in w.pending_approvals()] == ["indeterminate_side_effect"]
    owner_session.rollback()
    status = owner_session.execute(
        text(
            "SELECT status FROM shifde.run_steps WHERE run_id = :r AND node_id = 'post' "
            "ORDER BY attempt DESC LIMIT 1"
        ),
        {"r": run["id"]},
    ).scalar_one()
    assert status == "indeterminate" and w.posts() == 0


# ------------------------------------------------------------------ 11. numeric overflow
# ------------------------------------------------------------------ 12. schedules
def test_f12_schedules_validate_params_and_the_service_account_privileges(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    from shifde.app import create_app
    from tests.conftest import ORIGIN

    w = world6(client, world, owner_session, rest_server, sample_db_url)
    pipe = w.pipeline("sched2", w.reference_spec())
    on = settings.model_copy(update={"feature_pipeline_schedules": True})
    with TestClient(create_app(on), base_url="https://testserver", headers={"Origin": ORIGIN}) as c:
        h = login(c, world.passwords, "admin@demo-a.local")
        sa = post(c, f"{A}/service-accounts", h, {"name": "sched-sa2"})["id"]
        good = {
            "cron": "*/15 * * * *",
            "run_as_service_account_id": sa,
            "params": {"region": "north", "threshold": 1},
        }
        bad_params = c.post(
            f"{A}/pipelines/{pipe['id']}/schedules",
            headers=h,
            json={**good, "params": {"region": 5}},
        )
        assert bad_params.status_code == 422
        assert (
            c.post(f"{A}/pipelines/{pipe['id']}/schedules", headers=h, json=good).status_code == 201
        )
        # a powerful service account may not be used by a caller who lacks its permissions
        post(
            c,
            f"{A}/role-bindings",
            h,
            {
                "subject_type": "service_account",
                "subject_id": sa,
                "role_id": str(system_role(owner_session, "tenant_administrator")),
                "scope": {"type": "tenant", "id": w.scope["id"]},
            },
        )
        limited_user(
            c,
            owner_session,
            h,
            world.passwords,
            "sched@demo-a.local",
            ["workspace:read", "project:read", "pipeline:read", "pipeline:update"],
        )
        uh = login(c, world.passwords, "sched@demo-a.local")
        r = c.post(f"{A}/pipelines/{pipe['id']}/schedules", headers=uh, json=good)
        assert r.status_code == 403, r.text


# ------------------------------------------------------------------ 13. run/version integrity
def test_f13_a_run_cannot_pair_a_pipeline_with_another_pipelines_version(
    client: TestClient,
    world: World,
    owner_session: Session,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(client, world, owner_session, rest_server, sample_db_url)
    a = w.pipeline("pa", mini([doc_transform("t")], [], params=PARAMS))
    b = w.pipeline("pb", mini([doc_transform("t")], [], params=PARAMS))
    run = w.run(a["id"], {"doc": {"a": 1}})
    owner_session.rollback()
    with pytest.raises(Exception, match="fk_runs__pipeline_of_version"):
        owner_session.execute(
            text("UPDATE shifde.runs SET pipeline_id = :p WHERE id = :r"),
            {"p": b["id"], "r": run["id"]},
        )
    owner_session.rollback()


# ------------------------------------------------------------------ 14. approval-parked wake-up
def test_f14_a_finished_child_wakes_a_pipeline_parent_parked_on_an_approval(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = world6(
        client,
        world,
        owner_session,
        rest_server,
        sample_db_url,
        script=FAIL_SCRIPT,
        tool=post_tool,
        tool_key="post_item",
    )
    nodes = [agent_node(w, "b", "$params.t"), approval_gate("g")]
    pipe = w.pipeline("park", mini(nodes, [], params=PARAMS))
    run = w.run(pipe["id"], {"t": "need-approval"})

    def wakes() -> int:
        owner_session.rollback()
        return int(
            owner_session.execute(
                text(
                    "SELECT count(*) FROM shifde.run_events WHERE run_id = :r "
                    "AND type = 'state_change' AND to_status = 'pending'"
                ),
                {"r": run["id"]},
            ).scalar_one()
        )

    with thread_worker(settings, reap=3600):
        wait_for(lambda: len(w.pending_approvals()) == 2, 40)
        # the run is parked on the human approval while its child is still open
        owner_session.execute(
            text("UPDATE shifde.runs SET waiting_reason = 'approval' WHERE id = :r"),
            {"r": run["id"]},
        )
        owner_session.commit()
        before = wakes()
        [tool] = [a for a in w.pending_approvals() if a["reason"] == "tool_side_effect"]
        assert w.decide(tool["id"]).status_code == 200  # the child completes
        wait_for(lambda: wakes() > before, 30)  # the parent was woken to collect it
        wait_for(lambda: w.nodes(run["id"])["b"]["status"] == "succeeded", 30)
        w.client.post(f"{A}/runs/{run['id']}/cancel", headers=w.h)


# Moved earlier, not duplicated here (assertions unchanged there): test_f8_sanitize_json_and_a_
# delivered_write_is_never_failed_for_its_size -> tests/runtime/test_pipeline.py (RT-9);
# test_f11_huge_numbers_are_data_errors_not_internal_errors -> tests/runtime/test_pipeline_ops.py
# (B-2109).
