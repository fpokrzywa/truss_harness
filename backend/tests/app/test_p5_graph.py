"""P5 orchestrations: versioned artifact kind, validation, and the graph executor."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A
from tests._support.p4util import art, edit, publish
from tests._support.p5util import (
    Stack,
    limited_user,
    thread_worker,
    tool_call_reply,
    user_session,
    wait_for,
    wait_run,
)

SCRIPT = [
    {"match": "alpha-in", "reply": "alpha"},
    {"match": "beta-in", "reply": "beta"},
    {"match": "bad-in", "reply": tool_call_reply("not_pinned", {})},
]
LIMITS = {
    "max_nodes_executed": 10,
    "max_depth": 3,
    "max_total_tokens": 100000,
    "deadline_s": 120,
    "max_parallel": 4,
}


def spec(
    stack: Stack, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **over: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"nodes": nodes, "edges": edges, "limits": {**LIMITS}}
    out.update(over)
    return out


def agent_node(stack: Stack, nid: str, text_in: str) -> dict[str, Any]:
    return {
        "id": nid,
        "type": "agent",
        "ref": {"agent_version_id": f"latest:{stack.agent['id']}"},
        "input_map": {"input": text_in},
    }


def tool_node(stack: Stack, nid: str, expr: str) -> dict[str, Any]:
    return {
        "id": nid,
        "type": "tool",
        "ref": {"tool_version_id": f"latest:{stack.tool['id']}"},
        "input_map": {"text": expr},
    }


def make(client: TestClient, h: dict[str, str], stack: Stack, key: str, s: dict[str, Any]) -> Any:
    o = art(client, h, "orchestrations", key, s, stack.scope)
    return o, publish(client, h, "orchestrations", o["id"])


def run_orch(
    client: TestClient, h: dict[str, str], oid: str, text_in: str = "alpha-in"
) -> dict[str, Any]:
    r = client.post(
        f"{A}/runs",
        headers=h,
        json={
            "kind": "orchestration",
            "orchestration_id": oid,
            "workspace": "main",
            "project": "sample",
            "input": text_in,
        },
    )
    assert r.status_code == 202, r.text
    out: dict[str, Any] = r.json()
    return out


def result(client: TestClient, h: dict[str, str], rid: str) -> Any:
    return client.get(f"{A}/runs/{rid}/result", headers=h).json()["output"]


def validate(client: TestClient, h: dict[str, str], oid: str) -> dict[str, Any]:
    r = client.post(f"{A}/orchestrations/{oid}/draft/validate", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def setup(client: TestClient, world: World, owner: Session) -> tuple[dict[str, str], Stack]:
    from tests._support.p4util import set_tenant_setting

    set_tenant_setting(owner, "demo-a", "capture_content", "full")
    h = login(client, world.passwords, ADMIN)
    return h, Stack(client, h, owner, script=SCRIPT)


# ================================================================== validation
def test_orchestration_validation_rules(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, stack = setup(client, world, owner_session)

    def codes_of(s: dict[str, Any]) -> set[str]:
        o = art(
            client,
            h,
            "orchestrations",
            f"v{abs(hash(json.dumps(s, sort_keys=True))) % 10**8}",
            s,
            stack.scope,
        )
        return {e["code"] for e in validate(client, h, o["id"])["errors"]}

    a, b = agent_node(stack, "a", "x"), agent_node(stack, "b", "{{ nodes.a.output }}")
    ok = spec(stack, [a, b], [{"from": "a", "to": "b"}])
    assert codes_of(ok) == set()
    assert "cycle" in codes_of(
        spec(stack, [a, b], [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}])
    )
    assert "unknown_node" in codes_of(spec(stack, [a, b], [{"from": "a", "to": "zzz"}]))
    assert "reference_not_upstream" in codes_of(spec(stack, [a, b], []))
    bad_expr = {"id": "c", "type": "condition", "condition": "len(input) > 1"}
    assert "invalid_expression" in codes_of(spec(stack, [a, bad_expr], [{"from": "a", "to": "c"}]))
    assert "invalid_expression" in codes_of(
        spec(stack, [{**a, "input_map": {"input": "{{ os.environ }}"}}], [])
    )
    cond = {"id": "c", "type": "condition", "condition": "nodes.a.output == 'x'"}
    assert "invalid_value" in codes_of(
        spec(stack, [a, cond, b], [{"from": "a", "to": "b", "when": "true"}])
    )  # only condition edges can be true/false
    assert "required" in codes_of({"nodes": [a], "edges": []})  # limits are required
    assert "exceeds_maximum" in codes_of(spec(stack, [a], [], limits={**LIMITS, "max_depth": 9}))
    assert "exceeds_maximum" in codes_of(
        spec(stack, [a], [], limits={**LIMITS, "max_parallel": 40})
    )
    b2 = agent_node(stack, "b", "y")
    par = {"id": "p", "type": "parallel", "branches": ["a", "b"], "input_map": {}}
    assert codes_of(spec(stack, [a, b2, par], [])) == set()
    assert "branch_has_edges" in codes_of(spec(stack, [a, b2, par], [{"from": "a", "to": "b"}]))
    wide = [agent_node(stack, f"w{i}", "x") for i in range(5)]
    wide_par = {"id": "p", "type": "parallel", "branches": [w["id"] for w in wide], "input_map": {}}
    assert "exceeds_maximum" in codes_of(
        spec(stack, [*wide, wide_par], [], limits={**LIMITS, "max_parallel": 4})
    )
    unpublished = {
        "id": "t",
        "type": "tool",
        "ref": {"tool_version_id": "01900000-0000-7000-8000-0000000000bb"},
        "input_map": {"text": "x"},
    }
    assert "unresolved_reference" in codes_of(spec(stack, [unpublished], []))
    missing_args = {**tool_node(stack, "t", "x"), "input_map": {}}
    assert "missing_arguments" in codes_of(spec(stack, [missing_args], []))


def test_orchestration_is_a_p4_versioned_artifact_with_pins_and_ui_positions(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, stack = setup(client, world, owner_session)
    s1 = spec(stack, [agent_node(stack, "a", "x")], [], ui={"positions": {"a": {"x": 1, "y": 2}}})
    o, v1 = make(client, h, stack, "orch-ui", s1)
    assert v1["state"] == "published" and v1["spec"]["ui"]["positions"]["a"] == {"x": 1, "y": 2}
    assert [(p["role"], p["kind"]) for p in v1["pins"]] == [("agent", "agents")]
    assert v1["spec"]["nodes"][0]["ref"]["agent_version_id"] == stack.agent_v["id"]
    # D1.3: positions are part of the published artifact (and its spec_hash) but not validated further
    s2 = spec(stack, [agent_node(stack, "a", "x")], [], ui={"positions": {"a": {"x": 9, "y": 9}}})
    edit(client, h, "orchestrations", o["id"], s2, 0)
    v2 = publish(client, h, "orchestrations", o["id"], "move")
    assert v1["spec_hash"] != v2["spec_hash"]
    bad = spec(stack, [agent_node(stack, "a", "x")], [], ui={"positions": {"a": {"x": "no"}}})
    edit(client, h, "orchestrations", o["id"], bad, 0)
    assert {e["code"] for e in validate(client, h, o["id"])["errors"]} == {"invalid_value"}


def test_orchestration_permissions_follow_the_artifact_pattern(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h, stack = setup(client, world, owner_session)
    o, _ = make(client, h, stack, "perm-orch", spec(stack, [agent_node(stack, "a", "x")], []))
    limited_user(
        client, owner_session, h, world.passwords, "noorch@demo-a.local", ["workspace:read"]
    )
    nh = user_session(client, world.passwords, "noorch@demo-a.local")
    assert nh.client.get(f"{A}/orchestrations/{o['id']}", headers=nh).status_code == 404
    limited_user(
        client,
        owner_session,
        login(client, world.passwords, ADMIN),
        world.passwords,
        "rd@demo-a.local",
        ["workspace:read", "project:read", "orchestration:read", "run:read"],
    )
    rh = user_session(client, world.passwords, "rd@demo-a.local")
    assert rh.client.get(f"{A}/orchestrations/{o['id']}", headers=rh).status_code == 200
    run = rh.client.post(
        f"{A}/runs",
        headers=rh,
        json={
            "kind": "orchestration",
            "orchestration_id": o["id"],
            "workspace": "main",
            "project": "sample",
            "input": "x",
        },
    )
    assert run.status_code == 403  # orchestration:execute is needed


# ================================================================== execution
def test_sequential_conditional_graph_runs_children_and_skips_the_untaken_branch(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h, stack = setup(client, world, owner_session)
    nodes = [
        agent_node(stack, "a", "{{ input }}"),
        {"id": "c", "type": "condition", "condition": "nodes.a.output == 'alpha'", "input_map": {}},
        tool_node(stack, "yes", "got {{ nodes.a.output }}"),
        tool_node(stack, "no", "never"),
    ]
    edges = [
        {"from": "a", "to": "c"},
        {"from": "c", "to": "yes", "when": "true"},
        {"from": "c", "to": "no", "when": "false"},
    ]
    o, _ = make(client, h, stack, "seq-orch", spec(stack, nodes, edges))
    run = run_orch(client, h, o["id"])
    with thread_worker(settings, concurrency=3):
        done = wait_run(client, h, run["id"])
        assert done["status"] == "completed", done
        assert result(client, h, run["id"]) == {"text": "got alpha"}
        got = {
            s["node_id"]: s["status"]
            for s in client.get(f"{A}/runs/{run['id']}/steps", headers=h).json()
            if s["kind"] == "orchestration_node"
        }
        assert got == {"a": "succeeded", "c": "succeeded", "yes": "succeeded", "no": "skipped"}
        child_id = next(
            s["child_run_id"]
            for s in client.get(f"{A}/runs/{run['id']}/steps", headers=h).json()
            if s["node_id"] == "a"
        )
        child = client.get(f"{A}/runs/{child_id}", headers=h).json()
        assert child["parent_run_id"] == run["id"] and child["status"] == "completed"
        assert child["initiated_by"] == done["initiated_by"]  # the root principal, no elevation
        # the parent's usage includes its children
        assert done["usage"]["model_calls"] >= 1
        listed = client.get(f"{A}/runs", headers=h).json()["items"]
        assert [r["id"] for r in listed] == [run["id"]]  # children are not top-level history


def test_parallel_node_runs_branches_as_concurrent_child_runs(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h, stack = setup(client, world, owner_session)
    nodes = [
        agent_node(stack, "b1", "alpha-in"),
        agent_node(stack, "b2", "beta-in"),
        {"id": "p", "type": "parallel", "branches": ["b1", "b2"], "input_map": {}},
        tool_node(stack, "after", "{{ nodes.p.output.b1 }}-{{ nodes.p.output.b2 }}"),
    ]
    o, _ = make(client, h, stack, "par-orch", spec(stack, nodes, [{"from": "p", "to": "after"}]))
    run = run_orch(client, h, o["id"])
    with thread_worker(settings, concurrency=3):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed", (done["error_class"], done["error_message"])
    assert result(client, h, run["id"]) == {"text": "alpha-beta"}
    kids = owner_session.execute(
        text(
            "SELECT node_id, status, depth FROM shifde.runs WHERE parent_run_id = :p ORDER BY node_id"
        ),
        {"p": run["id"]},
    ).all()
    assert kids == [("b1", "completed", 1), ("b2", "completed", 1)]


def test_failing_child_fails_the_orchestration_with_its_error_class(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h, stack = setup(client, world, owner_session)
    o, _ = make(
        client, h, stack, "fail-orch", spec(stack, [agent_node(stack, "a", "{{ input }}")], [])
    )
    run = run_orch(client, h, o["id"], "bad-in")
    with thread_worker(settings, concurrency=2):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "failed" and done["error_class"] == "authz_denied"
    assert "node a failed" in done["error_message"]


def test_node_cap_stops_the_graph(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h, stack = setup(client, world, owner_session)
    nodes = [tool_node(stack, "t1", "a"), tool_node(stack, "t2", "{{ nodes.t1.output.text }}")]
    s = spec(stack, nodes, [{"from": "t1", "to": "t2"}], limits={**LIMITS, "max_nodes_executed": 1})
    o, _ = make(client, h, stack, "cap-orch", s)
    run = run_orch(client, h, o["id"])
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "failed" and done["error_class"] == "budget_denied"
    assert "max_nodes_executed" in done["error_message"]


def test_approval_node_pauses_until_a_human_decides(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h, stack = setup(client, world, owner_session)
    nodes = [
        {"id": "gate", "type": "approval", "input_map": {"summary": "ship {{ input }}"}},
        tool_node(stack, "t", "approved {{ input }}"),
    ]
    o, _ = make(client, h, stack, "appr-orch", spec(stack, nodes, [{"from": "gate", "to": "t"}]))
    limited_user(
        client,
        owner_session,
        h,
        world.passwords,
        "approver@demo-a.local",
        ["workspace:read", "project:read", "run:read", "approval:read", "approval:decide"],
    )
    ah = user_session(client, world.passwords, "approver@demo-a.local")
    h = login(client, world.passwords, ADMIN)
    run = run_orch(client, h, o["id"], "v1")
    with thread_worker(settings):
        waiting = wait_run(client, h, run["id"], {"waiting_for_approval"})
        assert waiting["waiting_reason"] == "approval"
        [appr] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
        assert appr["reason"] == "orchestration_node" and appr["tool"] is None
        assert appr["arguments"] == {"summary": "ship v1"}
        r = ah.client.post(
            f"{A}/approvals/{appr['id']}/decide",
            headers=ah,
            json={"decision": "approve", "comment": "ok"},
        )
        assert r.status_code == 200
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed" and result(client, h, run["id"]) == {"text": "approved v1"}


def test_cancelling_an_orchestration_cancels_its_children(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h, stack = setup(client, world, owner_session)
    owner_session.execute(
        text("UPDATE shifde.connections SET config = config || CAST(:c AS jsonb) WHERE id = :i"),
        {"c": json.dumps({"latency_ms": 2500}), "i": stack.connection},
    )
    owner_session.commit()
    o, _ = make(
        client, h, stack, "cancel-orch", spec(stack, [agent_node(stack, "a", "{{ input }}")], [])
    )
    run = run_orch(client, h, o["id"])
    with thread_worker(settings, concurrency=3):
        waiting = wait_run(client, h, run["id"], {"waiting_for_approval"})
        assert waiting["waiting_reason"] == "child_runs"
        cancelled = client.post(f"{A}/runs/{run['id']}/cancel", headers=h).json()
        assert cancelled["status"] in ("cancelled", "pending", "running", "waiting_for_approval")
        done = wait_run(client, h, run["id"], timeout=60)

        def child_status() -> str:
            return str(
                owner_session.execute(
                    text("SELECT status FROM shifde.runs WHERE parent_run_id = :p"),
                    {"p": run["id"]},
                ).scalar_one()
            )

        wait_for(lambda: (owner_session.rollback() or child_status()) == "cancelled", 30)
    assert done["status"] == "cancelled"
    kids = owner_session.execute(
        text("SELECT status FROM shifde.runs WHERE parent_run_id = :p"), {"p": run["id"]}
    ).all()
    assert [k[0] for k in kids] == ["cancelled"]
