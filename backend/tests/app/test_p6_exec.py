"""P6 pipeline execution through the real worker: the reference pipeline end to end, failure
policies, approvals, recovery, versioning and untrusted-data handling."""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p4util import edit
from tests._support.p5util import audit_actions, events, steps, thread_worker
from tests._support.p6util import World6


def expected_max_open(sample_db_url: str, region: str) -> tuple[int, int]:
    """(customers with open requests, max open requests of one customer) computed independently
    straight from the sample database."""
    with psycopg.connect(sample_db_url) as conn:
        rows = conn.execute(
            "SELECT customer_id, count(*) FROM service_requests "
            "WHERE region = %s AND status = 'open' GROUP BY customer_id",
            (region,),
        ).fetchall()
    return len(rows), max(int(r[1]) for r in rows)


def test_reference_pipeline_runs_end_to_end_with_approval_before_the_side_effect(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    customers, max_open = expected_max_open(sample_db_url, "north")
    assert max_open >= 1
    pipe = w.pipeline("customer-risk-review", w.reference_spec())
    run = w.run(pipe["id"], {"region": "north", "threshold": max_open - 1})
    assert run["kind"] == "pipeline" and run["is_test"] is False
    assert run["pipeline_id"] == pipe["id"] and run["pipeline_version_id"] == pipe["version"]["id"]
    with thread_worker(settings):
        w.wait_parked(run["id"])
        assert w.posts() == 0  # the side effect does not run before the approval
        nodes = w.nodes(run["id"])
        assert {n: nodes[n]["status"] for n in nodes} == {
            "src": "succeeded",
            "check": "succeeded",
            "prep": "succeeded",
            "rollup": "succeeded",
            "summ": "succeeded",
            "risk": "succeeded",
            "gate": "waiting_for_approval",
            "deliver": "pending",
            "store": "succeeded",
        }
        assert nodes["rollup"]["output_preview"]["data"] == [{"max_open": max_open}]
        assert len(nodes["prep"]["output_preview"]["data"]) == customers
        assert nodes["src"]["attempts"] == 1 and nodes["src"]["error_class"] is None
        assert nodes["rollup"]["output_ref"]
        [appr] = w.pending_approvals()
        assert appr["reason"] == "pipeline_node" and appr["run_id"] == run["id"]
        assert w.decide(appr["id"]).status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    assert w.posts() == 1
    nodes = w.nodes(run["id"])
    assert all(n["status"] == "succeeded" for n in nodes.values()), nodes
    assert nodes["deliver"]["output_preview"]["result"] == {"status": 200, "delivered": True}
    # the stored result is a run artifact reachable through the run
    result = client.get(f"{A}/runs/{run['id']}/result", headers=w.h).json()["output"]
    artifact_id = result["artifacts"]["store"]
    assert result["skipped"] == []
    stored = client.get(f"{A}/runs/{run['id']}/artifacts/{artifact_id}", headers=w.h).json()
    assert stored["content"] == "summary: elevated customer risk" and stored["kind"] == "output"
    # execution events, usage (model call of the agent child) and audit
    types = [e["type"] for e in events(client, w.h, run["id"])]
    assert types.count("step_started") >= 8 and "approval_requested" in types
    assert "approval_decided" in types and "gate_decision" in types
    done = client.get(f"{A}/runs/{run['id']}", headers=w.h).json()
    assert done["usage"]["model_calls"] == 1 and done["usage"]["input_tokens"] > 0
    usage = owner_session.execute(
        text(
            "SELECT event_type, count(*) FROM shifde.usage_events WHERE root_run_id = :r "
            "GROUP BY event_type ORDER BY event_type"
        ),
        {"r": run["id"]},
    ).all()
    assert dict(usage) == {"model_call": 1, "pipeline_node": 9}
    acts = audit_actions(owner_session)
    assert "pipeline.created" in acts and "pipeline.published" in acts
    assert "run.created" in acts and "run.completed" in acts and "approval.decided" in acts
    # the run appears in the pipeline's history
    hist = client.get(f"{A}/pipelines/{pipe['id']}/runs", headers=w.h).json()
    assert [r["id"] for r in hist["items"]] == [run["id"]] and hist["next_cursor"] is None
    # every node journaled as a pipeline_node step
    kinds = [s["kind"] for s in steps(client, w.h, run["id"])]
    assert kinds.count("pipeline_node") == 9


def test_condition_false_skips_the_approval_and_the_side_effect(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    pipe = w.pipeline("risk", w.reference_spec())
    run = w.run(pipe["id"], {"region": "north", "threshold": 10_000})
    with thread_worker(settings):
        done = w.wait(run["id"])
    assert done["status"] == "completed", done
    nodes = w.nodes(run["id"])
    assert nodes["gate"]["status"] == "skipped" and nodes["deliver"]["status"] == "skipped"
    assert nodes["store"]["status"] == "succeeded"
    assert w.posts() == 0 and w.pending_approvals() == []
    result = client.get(f"{A}/runs/{run['id']}/result", headers=w.h).json()["output"]
    assert result["skipped"] == ["gate", "deliver"]


def test_rejecting_the_approval_fails_the_run_and_nothing_is_sent(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    pipe = w.pipeline("risk", w.reference_spec())
    run = w.run(pipe["id"], {"region": "north", "threshold": 0})
    with thread_worker(settings):
        w.wait_parked(run["id"])
        [appr] = w.pending_approvals()
        # separation of duties: the initiator cannot approve their own run
        own = client.post(
            f"{A}/approvals/{appr['id']}/decide", headers=w.h, json={"decision": "approve"}
        )
        assert own.status_code == 403 and own.json()["code"] == "separation_of_duties"
        assert w.decide(appr["id"], "reject").status_code == 200
        done = w.wait(run["id"])
    assert done["status"] == "failed" and done["error_class"] == "approval_rejected"
    assert w.posts() == 0
    assert w.nodes(run["id"])["deliver"]["status"] in ("pending", "cancelled")


def test_published_version_is_pinned_and_draft_runs_are_test_runs(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    simple = {
        "schema_version": "1",
        "params": {"type": "object", "properties": {"x": {"type": "integer"}}},
        "nodes": [
            {
                "id": "mk",
                "type": "condition",
                "config": {"expression": "params.x > 1"},
                "inputs": {},
            },
        ],
        "edges": [],
    }
    pipe = w.pipeline("simple", simple)
    v1 = pipe["version"]
    changed = {
        **simple,
        "nodes": [{**simple["nodes"][0], "config": {"expression": "params.x > 100"}}],
    }
    edit(client, w.h, "pipelines", pipe["id"], changed)
    v2_run_before_publish = w.run(pipe["id"], {"x": 5})
    assert v2_run_before_publish["pipeline_version_id"] == v1["id"]  # still v1: v2 is only a draft
    from tests._support.p4util import publish

    v2 = publish(client, w.h, "pipelines", pipe["id"], "v2")
    assert v2["version_no"] == 2
    run_v2 = w.run(pipe["id"], {"x": 5})
    run_v1 = w.run(pipe["id"], {"x": 5}, version=v1["id"])
    assert run_v2["pipeline_version_id"] == v2["id"] and run_v1["pipeline_version_id"] == v1["id"]
    # a draft test run executes the draft (not any published version) and is labelled
    edit(client, w.h, "pipelines", pipe["id"], simple, row_version=0)
    draft = w.run(pipe["id"], {"x": 5}, version="draft", is_test=False)
    assert draft["is_test"] is True
    with thread_worker(settings):
        for r in (v2_run_before_publish, run_v2, run_v1, draft):
            assert w.wait(r["id"])["status"] == "completed"

    def outcome(rid: str) -> Any:
        return client.get(f"{A}/runs/{rid}/result", headers=w.h).json()["output"]["outputs"]

    assert outcome(run_v1["id"]) == {"mk": {"result": True}}
    assert outcome(run_v2["id"]) == {"mk": {"result": False}}
    assert outcome(draft["id"]) == {"mk": {"result": True}}
    listed = client.get(f"{A}/pipelines/{pipe['id']}/runs", headers=w.h).json()["items"]
    assert [r["is_test"] for r in listed] == [True, False, False, False]
