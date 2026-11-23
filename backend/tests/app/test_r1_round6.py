"""R1 critic round 6: only a 2xx answers an unknown delivery (on the automatic resend and after a
crash, not only after an operator's retry), 3xx and MCP `isError` are refusals, a verdict is
applied together with the finish of a node, and a run that is already classed still says which
call is unknown (and counts them correctly as decisions are made).

Moved from SETH 3ba0e06 `backend/tests/test_r1_round6.py` (APP-2 part 2e), assertions unchanged.
Setup change (B-2115): the `repo.land_verdicts` spy takes the client second and passes it through.
SETH's `test_r1_model` helpers come from `tests/_support/r1model.py` (that file is not ported).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from tests._support.conntests import discovery_of
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A, activate, connection, secret
from tests._support.p5util import (
    Stack,
    audit_actions,
    builtin_tool_spec,
    get_run,
    start_run,
    thread_worker,
    tool_call_reply,
    wait_run,
)
from tests._support.p6util import World6
from tests._support.r1model import DONE, drive, drive_pipeline, pipeline
from tests.app.test_p5_worker import SCRIPT
from tests.app.test_r1_round2 import node_steps
from tests.app.test_r1_round3 import ENDED, ONE, RETRY, Scripted, start
from tests.app.test_r1_round4 import epochs, kill_during_the_write
from tests.app.test_r1_unknown_outcome import (
    UNKNOWN,
    cancel,
    decide,
    invocation_states,
    pending_unknown,
)


# ------------------------------------------------------------------ 1: the automatic resend
@pytest.mark.parametrize("first", ["cut", 500])
@pytest.mark.parametrize("answer", [400, 404, 409, 422, 302, 304])
def test_a_resend_answered_without_the_work_parks_instead_of_completing(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    first: Any,
    answer: int,
) -> None:
    srv = Scripted([first, answer])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=RETRY)
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, h, run["id"], ENDED, timeout=60)
            assert done["status"] == "waiting_for_approval", done
            assert done["outcome_unknown"] is True
            assert srv.deliveries == 2
            assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
            assert epochs(owner_session, run["id"]) == [(0, "pending")]
            [appr] = pending_unknown(ah, run["id"])
            assert decide(ah, appr["id"], "mark_succeeded", "it is there").status_code == 200
            end = wait_run(client, h, run["id"], {"completed", "failed"}, timeout=60)
        assert end["status"] == "completed" and end["outcome_unknown"] is False, end
        assert invocation_states(owner_session, run["id"]) == ["resolved_succeeded"]
    finally:
        srv.close()


def test_a_resend_after_a_crash_answered_without_the_work_parks(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    srv = Scripted([409], hold_first=True)
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=RETRY)
        kill_during_the_write(settings, srv, owner_session, "w1")  # delivery 1 never answers
        with thread_worker(settings, lease=3, reap=1000):
            parked = wait_run(client, h, run["id"], ENDED, timeout=60)
        assert parked["status"] == "waiting_for_approval", parked
        assert parked["outcome_unknown"] is True
        assert srv.deliveries == 2  # the recovery resent once; the 409 did not settle delivery 1
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        srv.close()


def test_an_ordinary_4xx_without_an_earlier_unknown_delivery_is_still_an_output(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    srv = Scripted([409])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=RETRY)
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, h, run["id"], ENDED, timeout=60)
        assert done["status"] == "completed" and done["outcome_unknown"] is False
        assert invocation_states(owner_session, run["id"]) == ["succeeded"]
    finally:
        srv.close()


# ------------------------------------------------------------------ 3: a 3xx is not a success
def test_a_3xx_after_an_operator_retry_does_not_close_the_question_on_a_tool(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    srv = Scripted(["cut", 302])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=ONE)
        done = drive(client, h, ah, settings, run, ["retry"])
        assert done["status"] == "failed" and done["outcome_unknown"] is True, done
        assert invocation_states(owner_session, run["id"]) == ["resolved_retry", "failed"]
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (2, "pending")]
    finally:
        srv.close()


def test_a_3xx_after_an_operator_retry_does_not_close_the_question_on_a_pipeline_node(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    srv = Scripted(["cut", 302])
    try:
        w, run = pipeline(client, world, owner_session, rest_server, sample_db_url, srv)
        done = drive_pipeline(w, settings, run, ["retry"])
        assert done["status"] == "failed" and done["outcome_unknown"] is True, done
        assert node_steps(owner_session, run["id"], "post") == [(1, "indeterminate"), (2, "failed")]
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (2, "pending")]
        assert w.nodes(run["id"])["post"]["outcome_unknown"] is True
    finally:
        srv.close()


def test_a_first_attempt_3xx_sink_is_unchanged(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    """Nothing is unknown before it, so a 3xx stays what it always was: the node's output."""
    srv = Scripted([302])
    try:
        w, run = pipeline(client, world, owner_session, rest_server, sample_db_url, srv)
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, w.h, run["id"], DONE, timeout=60)
        assert done["status"] == "completed", done
    finally:
        srv.close()


# ------------------------------------------------------------------ 4: MCP isError end to end
def test_an_mcp_error_result_on_a_resend_is_a_refusal(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    mcp_server: Any,
) -> None:
    h = login(client, world.passwords, ADMIN)
    conn = connection(
        client,
        h,
        {
            "name": "mcp-flaky",
            "connection_type": "mcp",
            "adapter_key": "mcp_http",
            "config": {"url": mcp_server.url},
            "auth_method": "bearer",
            "secret_id": secret(client, h, "mcp-flaky-token", "mcp-secret-token-0002"),
            "access_mode": "read_write",
        },
    )
    activate(client, h, conn["id"])
    discovery_of(client, h, A, conn["id"])
    spec = builtin_tool_spec()
    spec.update(
        executor="mcp.tool",
        binding={"connection_id": conn["id"], "tool_name": "flaky"},
        input_schema={
            "type": "object",
            "required": ["target"],
            "properties": {"target": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect_class="write_idempotent",
        timeout_s=2,
        retry={"max_attempts": 3, "backoff_s": 0},
        idempotency="key_supported",
    )
    stack = Stack(
        client,
        h,
        owner_session,
        script=[SCRIPT[0], {"match": "go", "reply": tool_call_reply("target", {"target": "t1"})}],
        tool_key="target",
        tool=spec,
    )
    run = start_run(client, h, stack.agent["id"], "go")
    with thread_worker(settings, lease=10, reap=1000):
        done = wait_run(client, h, run["id"], ENDED, timeout=90)
    # the first call timed out (it may have landed); the resend came back as an MCP error result
    assert done["status"] == "waiting_for_approval", done
    assert done["outcome_unknown"] is True
    assert invocation_states(owner_session, run["id"]) == ["indeterminate"]


# ------------------------------------------------------------------ 5/7/8: pipeline verdicts
def test_a_pipeline_verdict_given_live_on_a_second_decision_lands_on_the_first_delivery(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    srv = Scripted(["cut", "cut"])
    try:
        w, run = pipeline(client, world, owner_session, rest_server, sample_db_url, srv)
        done = drive_pipeline(w, settings, run, ["retry", "mark_succeeded"])
        assert done["status"] == "completed", done
        assert node_steps(owner_session, run["id"], "post") == [(1, "succeeded"), (2, "succeeded")]
        owner_session.rollback()
        classes = owner_session.execute(
            text(
                "SELECT error_class, error_message FROM shifde.run_steps "
                "WHERE run_id = :r AND node_id = 'post'"
            ),
            {"r": run["id"]},
        ).all()
        assert all(c[0] is None for c in classes), classes  # the table says: no error class
        assert srv.deliveries == 2
    finally:
        srv.close()


def test_a_tool_step_has_no_error_class_after_mark_succeeded(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    srv = Scripted(["cut"])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=ONE)
        done = drive(client, h, ah, settings, run, ["mark_succeeded"])
        assert done["status"] == "completed", done
        owner_session.rollback()
        rows = owner_session.execute(
            text(
                "SELECT status, error_class FROM shifde.run_steps "
                "WHERE run_id = :r AND kind = 'tool_call'"
            ),
            {"r": run["id"]},
        ).all()
        assert [tuple(r) for r in rows] == [("succeeded", None)], rows
    finally:
        srv.close()


def test_a_pipeline_node_is_finished_and_its_verdict_landed_in_one_transaction(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv = Scripted(["cut", "cut"])
    seen: list[str | None] = []
    real = repo.land_verdicts

    # B-2115: the repository takes the client second; the spy passes it through (setup only)
    def spy(session: Session, tenant_id: Any, run_id: Any) -> None:
        # a SECOND connection must not yet see the node as finished: nothing was committed
        with owner_session.get_bind().connect() as c:  # type: ignore[union-attr]
            seen.append(
                c.execute(
                    text(
                        "SELECT status FROM shifde.run_steps WHERE run_id = :r "
                        "AND node_id = 'post' ORDER BY attempt DESC LIMIT 1"
                    ),
                    {"r": run_id},
                ).scalar_one_or_none()
            )
        real(session, tenant_id, run_id)

    try:
        w, run = pipeline(client, world, owner_session, rest_server, sample_db_url, srv)
        monkeypatch.setattr(repo, "land_verdicts", spy)
        done = drive_pipeline(w, settings, run, ["retry", "mark_succeeded"])
        assert done["status"] == "completed", done
        assert seen and seen[-1] == "indeterminate", seen
    finally:
        srv.close()


# ------------------------------------------------------------------ 2: a run that is already classed
def two_parked_writes(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> tuple[World6, dict[str, Any], dict[str, dict[str, Any]], list[Scripted]]:
    from tests._support.p5util import rest_connection
    from tests._support.p6util import mini

    srvs = [Scripted(["cut"]), Scripted(["cut"])]
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    nodes: list[dict[str, Any]] = [
        {
            "id": "t",
            "type": "transform",
            "config": {"operations": [{"op": "select", "fields": ["a"]}]},
            "inputs": {"data": "$params.doc"},
        }
    ]
    for nid, srv in zip(("a", "b"), srvs, strict=True):
        conn = rest_connection(w.client, w.h, srv.base_url, name=f"conn-{nid}")
        nodes.append(
            {
                "id": nid,
                "type": "sink",
                "config": {
                    "target": "connection",
                    "connection_id": conn,
                    "mode": "append",
                    "path_template": "/hang",
                },
                "inputs": {"document": "t.data"},
            }
        )
    spec = mini(
        nodes,
        [{"from": "t", "to": "a"}, {"from": "t", "to": "b"}],
        params={"type": "object", "properties": {"doc": {"type": "object"}}},
        policies={"max_parallel": 1, "deadline_seconds": 300, "on_failure": "continue"},
    )
    pipe = w.pipeline("two-writes-r6", spec)
    run = w.run(pipe["id"], {"doc": {"a": 1}})
    return w, run, {}, srvs


def pending_by_node(w: World6) -> dict[str, dict[str, Any]]:
    return {
        (x["arguments"] or {}).get("node"): x
        for x in w.pending_approvals()
        if x["reason"] == UNKNOWN
    }


def flag_events(owner: Session, run_id: str) -> int:
    owner.rollback()
    return int(
        owner.execute(
            text(
                "SELECT count(*) FROM shifde.run_events WHERE run_id = :r "
                "AND type = 'outcome_unknown'"
            ),
            {"r": run_id},
        ).scalar_one()
    )


def test_a_fail_verdict_while_another_decision_is_pending_still_flags_and_names_the_call(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    w, run, _, srvs = two_parked_writes(
        client, world, owner_session, settings, rest_server, sample_db_url
    )
    try:
        with thread_worker(settings, lease=5, reap=1000):
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
            both = pending_by_node(w)
            assert set(both) == {"a", "b"}
            assert w.decide(both["b"]["id"], "fail", "x").status_code == 200  # ends the run
            done = wait_run(w.client, w.h, run["id"], DONE, timeout=60)
        assert done["status"] == "failed" and done["outcome_unknown"] is True, done
        assert set(pending_by_node(w)) == {"a"}
        assert "node:a" in done["error_message"], done["error_message"]
        assert "unknown until an operator resolves the approval" in done["error_message"]
        assert flag_events(owner_session, run["id"]) == 1
        assert "run.indeterminate_side_effect" in audit_actions(owner_session, run["id"])
        # settle again: nothing is said twice
        owner_session.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
        owner_session.commit()
        assert flag_events(owner_session, run["id"]) == 1
    finally:
        for s in srvs:
            s.close()


def test_the_count_in_the_message_falls_as_decisions_are_made(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    w, run, _, srvs = two_parked_writes(
        client, world, owner_session, settings, rest_server, sample_db_url
    )
    try:
        with thread_worker(settings, lease=5, reap=1000):
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
        both = pending_by_node(w)
        cancel(client, w.h, run["id"])
        msg = get_run(w.client, w.h, run["id"])["error_message"]
        assert "the 2 approvals" in msg and "node:a" in msg and "node:b" in msg, msg
        assert w.decide(both["a"]["id"], "mark_succeeded", "seen").status_code == 200
        msg = get_run(w.client, w.h, run["id"])["error_message"]
        assert "the 2 approvals" not in msg and "the approval" in msg, msg
        assert "node:b" in msg and "node:a" not in msg, msg
        assert w.decide(both["b"]["id"], "fail", "x").status_code == 200
        final = get_run(w.client, w.h, run["id"])
        assert final["outcome_unknown"] is False
        assert "1 confirmed succeeded" in final["error_message"], final
    finally:
        for s in srvs:
            s.close()
