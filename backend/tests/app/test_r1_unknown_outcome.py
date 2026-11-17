"""R1 / C1 + J-M3: a write whose outcome is unknown is never reported as a plain cancel or timeout,
and the human decision about it survives Cancel and the deadline.

The state is built with the real recovery harness (a `shifde-worker` subprocess killed while the
POST is in flight, or a live worker whose call is cut); the reaper is then run directly so every
step is deterministic. Deliveries are counted at the local target."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from shifde.runtime.worker import Worker
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p5util import (
    SubprocessWorker,
    audit_actions,
    fast,
    get_run,
    start_run,
    thread_worker,
    wait_for,
    wait_run,
)
from tests._support.p6util import HangServer as PipelineHang
from tests._support.p6util import World6
from tests.app.test_p5_worker import HangServer, build
from tests.app.test_p6_exec2 import hang_pipeline

UNKNOWN = "indeterminate_side_effect"


# ------------------------------------------------------------------ helpers
def reap(settings: Settings) -> None:
    """One reaper pass (what every worker does every few seconds), including its audit."""
    Worker(fast(settings), worker_id="reaper-test", concurrency=1)._reap()


def cancel(client: TestClient, h: dict[str, str], run_id: str) -> None:
    r = client.post(f"{A}/runs/{run_id}/cancel", headers=h)
    assert r.status_code in (200, 202), r.text


def expire_leases(owner: Session) -> None:
    owner.rollback()
    owner.execute(
        text("UPDATE shifde.jobs SET lease_expires_at = now() - interval '1 second' "
             "WHERE status = 'leased'")
    )  # fmt: skip
    owner.commit()


def run_row(owner: Session, run_id: str) -> dict[str, Any]:
    owner.rollback()
    row = owner.execute(
        text("SELECT status, error_class, error_message FROM shifde.runs WHERE id = :r"),
        {"r": run_id},
    ).one()
    return {"status": row[0], "error_class": row[1], "error_message": row[2]}


def step_states(owner: Session, run_id: str) -> list[tuple[str, str]]:
    owner.rollback()
    return [
        (r[0], r[1])
        for r in owner.execute(
            text(
                "SELECT kind, status FROM shifde.run_steps WHERE run_id = :r ORDER BY seq, attempt"
            ),
            {"r": run_id},
        )
    ]


def invocation_states(owner: Session, run_id: str) -> list[str]:
    owner.rollback()
    return [
        r[0]
        for r in owner.execute(
            text(
                "SELECT status FROM shifde.tool_invocations WHERE run_id = :r ORDER BY started_at"
            ),
            {"r": run_id},
        )
    ]


def pending_unknown(ah: Any, run_id: str | None = None) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()[
        "items"
    ]
    return [a for a in got if a["reason"] == UNKNOWN and (run_id is None or a["run_id"] == run_id)]


def decide(ah: Any, approval_id: str, decision: str, comment: str = "checked at the target") -> Any:
    return ah.client.post(
        f"{A}/approvals/{approval_id}/decide",
        headers=ah,
        json={"decision": decision, **({"comment": comment} if comment else {})},
    )


def crash_midwrite(
    client: TestClient, world: World, owner: Session, settings: Settings, server: HangServer
) -> tuple[dict[str, str], Any, dict[str, Any]]:
    """An approved write_non_idempotent POST reaches the target, then the worker process is killed.
    Returns (initiator headers, approver session, run)."""
    h, stack, ah = build(client, world, owner, server, "write_non_idempotent", "POST")
    run = start_run(client, h, stack.agent["id"], "go")
    w1 = SubprocessWorker(settings, "w1")
    try:
        wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=90)
        [appr] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
        assert decide(ah, appr["id"], "approve", "").status_code == 200
        assert server.first_started.wait(60), "the write never started"
        w1.kill()  # the POST reached the target; its outcome is unknown to the platform
    finally:
        if w1.proc.poll() is None:
            w1.kill()
    assert get_run(client, h, run["id"])["status"] == "running"  # a dead worker still holds it
    return h, ah, run


def assert_flagged(
    owner: Session, client: TestClient, h: dict[str, str], ah: Any, run: dict[str, Any], status: str
) -> dict[str, Any]:
    row = run_row(owner, run["id"])
    assert row["status"] == status, row
    assert row["error_class"] == UNKNOWN, row
    assert "unknown" in (row["error_message"] or "").lower()
    # nothing is left running on a terminal run; the write step and invocation say "unknown"
    states = step_states(owner, run["id"])
    assert all(st != "running" for _, st in states), states
    assert ("tool_call", "indeterminate") in states, states
    assert invocation_states(owner, run["id"]) == ["indeterminate"]
    api = get_run(client, h, run["id"])
    assert api["error_class"] == UNKNOWN and api["outcome_unknown"] is True
    # the decision is open and reachable through the approvals API, with the call's arguments
    [appr] = pending_unknown(ah, run["id"])
    assert appr["arguments"] == {"item": "x"} and appr["tool"]["key"] == "target"
    assert "run.indeterminate_side_effect" in audit_actions(owner, run["id"])
    return appr


def resolve_and_check(
    owner: Session,
    client: TestClient,
    h: dict[str, str],
    ah: Any,
    run: dict[str, Any],
    appr: dict[str, Any],
    server: HangServer,
) -> None:
    assert decide(ah, appr["id"], "mark_succeeded", "").status_code == 422  # a comment is required
    assert decide(ah, appr["id"], "retry").status_code == 422  # the run has ended: no retry
    ok = decide(ah, appr["id"], "mark_succeeded", "verified in the target")
    assert ok.status_code == 200, ok.text
    assert invocation_states(owner, run["id"]) == ["resolved_succeeded"]
    assert ("tool_call", "succeeded") in step_states(owner, run["id"])
    api = get_run(client, h, run["id"])
    assert api["outcome_unknown"] is False and api["status"] in ("cancelled", "timed_out")
    assert "confirmed" in (api["error_message"] or "")
    assert server.count("POST") == 1  # never repeated


# ------------------------------------------------------------------ C1: kill, then Cancel
def test_worker_killed_mid_write_then_cancelled_is_not_reported_as_a_plain_cancel(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash_midwrite(client, world, owner_session, settings, server)
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)  # lease lost -> pending -> the cancel flag finalizes it (reaper section 3)
        appr = assert_flagged(owner_session, client, h, ah, run, "cancelled")
        resolve_and_check(owner_session, client, h, ah, run, appr, server)
    finally:
        server.close()


def test_worker_killed_mid_write_then_past_the_deadline_is_not_a_plain_timeout(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash_midwrite(client, world, owner_session, settings, server)
        owner_session.execute(
            text("UPDATE shifde.runs SET deadline_at = now() - interval '1 second' WHERE id = :r"),
            {"r": run["id"]},
        )
        owner_session.commit()
        expire_leases(owner_session)
        reap(settings)  # reaper section 2
        appr = assert_flagged(owner_session, client, h, ah, run, "timed_out")
        resolve_and_check(owner_session, client, h, ah, run, appr, server)
    finally:
        server.close()


def test_the_operator_can_declare_the_write_failed_on_an_ended_run(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash_midwrite(client, world, owner_session, settings, server)
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        appr = assert_flagged(owner_session, client, h, ah, run, "cancelled")
        assert decide(ah, appr["id"], "fail", "the target has no record").status_code == 200
        assert invocation_states(owner_session, run["id"]) == ["resolved_failed"]
        assert ("tool_call", "failed") in step_states(owner_session, run["id"])
        api = get_run(client, h, run["id"])
        assert api["outcome_unknown"] is False and api["status"] == "cancelled"
        assert "declared" in api["error_message"]
        assert server.count("POST") == 1
    finally:
        server.close()


def test_a_run_with_no_unknown_write_still_cancels_and_times_out_plainly(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """The flag is only for runs that really have an open write: a cancelled read keeps `cancelled`
    and its running step is closed instead of being left `running`."""
    server = HangServer()
    try:
        h, stack, _ = build(client, world, owner_session, server, "read", "GET")
        run = start_run(client, h, stack.agent["id"], "go")
        w1 = SubprocessWorker(settings, "w1")
        try:
            assert server.first_started.wait(60)
            w1.kill()
        finally:
            if w1.proc.poll() is None:
                w1.kill()
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        row = run_row(owner_session, run["id"])
        assert row["status"] == "cancelled" and row["error_class"] == "cancelled", row
        assert all(st != "running" for _, st in step_states(owner_session, run["id"]))
        assert get_run(client, h, run["id"])["outcome_unknown"] is False
    finally:
        server.close()


# ------------------------------------------------------------------ settle / parent / budget
def test_reaper_terminal_state_still_releases_reservations_and_wakes_a_waiting_parent(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash_midwrite(client, world, owner_session, settings, server)
        parent = "01a1af3d-0000-7000-8000-00000000fa11"
        owner_session.execute(
            text(
                "INSERT INTO shifde.runs (id, tenant_id, workspace_id, project_id, kind, status, "
                "state_version, initiated_by_principal_id, principal_type, agent_id, "
                "agent_version_id, prompt_version_id, root_run_id, trace_id, correlation_id, "
                "deadline_at, waiting_reason) "
                "SELECT CAST(:p AS uuid), tenant_id, workspace_id, project_id, kind, "
                "'waiting_for_approval', 3, initiated_by_principal_id, principal_type, agent_id, "
                "agent_version_id, prompt_version_id, CAST(:p AS uuid), trace_id, correlation_id, "
                "deadline_at, 'child_runs' FROM shifde.runs WHERE id = :c"
            ),
            {"p": parent, "c": run["id"]},
        )
        owner_session.execute(
            text("UPDATE shifde.runs SET parent_run_id = :p WHERE id = :c"),
            {"p": parent, "c": run["id"]},
        )
        owner_session.execute(
            text(
                "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, "
                "budget_ids, amount, status, expires_at) SELECT gen_random_uuid(), tenant_id, id, "
                "'k1', ARRAY[]::uuid[], 10, 'held', now() + interval '1 hour' "
                "FROM shifde.runs WHERE id = :c"
            ),
            {"c": run["id"]},
        )
        owner_session.commit()
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        assert run_row(owner_session, run["id"])["status"] == "cancelled"
        assert (
            owner_session.execute(
                text("SELECT status FROM shifde.budget_reservations WHERE run_id = :c"),
                {"c": run["id"]},
            ).scalar_one()
            == "released"
        )
        # the parent was waiting for this child: it is runnable again (pending + a queued job)
        assert run_row(owner_session, parent)["status"] == "pending"
        queued = owner_session.execute(
            text("SELECT count(*) FROM shifde.jobs WHERE run_id = :p AND status = 'queued'"),
            {"p": parent},
        ).scalar_one()
        assert queued == 1
        assert pending_unknown(ah, run["id"])  # and the child's decision is still open
    finally:
        server.close()


# ------------------------------------------------------------------ J-M3: the window to decide
def parked_on_unknown_write(
    client: TestClient, h: dict[str, str], ah: Any, server: HangServer, stack_run: dict[str, Any]
) -> dict[str, Any]:
    """A live worker made the call; the connection is cut: the run parks on an unknown outcome."""
    appr = wait_for(
        lambda: ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"] or None
    )
    assert decide(ah, appr[0]["id"], "approve", "").status_code == 200
    assert server.first_started.wait(60)
    server.release.set()  # the request ends without an answer
    return wait_run(client, h, stack_run["id"], {"waiting_for_approval"})


def test_live_worker_past_the_deadline_leaves_the_decision_open(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build(client, world, owner_session, server, "write_non_idempotent", "POST")
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=0.3):
            appr = wait_for(
                lambda: (
                    ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
                    or None
                )
            )
            assert decide(ah, appr[0]["id"], "approve", "").status_code == 200
            assert server.first_started.wait(60)
            owner_session.execute(
                text("UPDATE shifde.runs SET deadline_at = now() + interval '1 second' "
                     "WHERE id = :r"),
                {"r": run["id"]},
            )  # fmt: skip
            owner_session.commit()
            server.release.set()  # the call is cut after the deadline: unknown outcome
            done = wait_run(client, h, run["id"], {"timed_out"}, timeout=40)
            assert done["error_class"] == UNKNOWN and done["outcome_unknown"] is True
            [open_appr] = pending_unknown(ah, run["id"])  # NOT expired by the deadline
            assert all(st != "running" for _, st in step_states(owner_session, run["id"]))
            assert decide(ah, open_appr["id"], "mark_succeeded").status_code == 200
        assert invocation_states(owner_session, run["id"]) == ["resolved_succeeded"]
        assert server.count("POST") == 1
    finally:
        server.close()


def test_cancel_while_parked_on_an_unknown_outcome_keeps_the_decision_open(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """Cancel is accepted (the run is not executing) but the question stays open and the run's
    final state says the outcome is unknown."""
    server = HangServer()
    try:
        h, stack, ah = build(client, world, owner_session, server, "write_non_idempotent", "POST")
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=0.3):
            parked = parked_on_unknown_write(client, h, ah, server, run)
            assert parked["waiting_reason"] == UNKNOWN and parked["outcome_unknown"] is True
            cancel(client, h, run["id"])
            done = get_run(client, h, run["id"])
            assert done["status"] == "cancelled" and done["error_class"] == UNKNOWN
            assert done["outcome_unknown"] is True and "unknown" in done["error_message"].lower()
            [open_appr] = pending_unknown(ah, run["id"])
            reap(settings)  # a later reaper pass does not close it either
            assert pending_unknown(ah, run["id"])
            assert (
                decide(ah, open_appr["id"], "fail", "never reached the target").status_code == 200
            )
        assert get_run(client, h, run["id"])["outcome_unknown"] is False
        assert invocation_states(owner_session, run["id"]) == ["resolved_failed"]
        assert server.count("POST") == 1
    finally:
        server.close()


def test_an_unknown_outcome_approval_does_not_expire_and_can_still_be_decided(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build(client, world, owner_session, server, "write_non_idempotent", "POST")
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=0.3):
            parked_on_unknown_write(client, h, ah, server, run)
            owner_session.execute(
                text("UPDATE shifde.approvals SET expires_at = now() - interval '1 hour' "
                     "WHERE reason = 'indeterminate_side_effect'")
            )  # fmt: skip
            owner_session.commit()
            reap(settings)
            still = get_run(client, h, run["id"])
            assert still["status"] == "waiting_for_approval", still  # not "the approval expired"
            [open_appr] = pending_unknown(ah, run["id"])
            ok = decide(ah, open_appr["id"], "retry", "the target has no record of it")
            assert ok.status_code == 200, ok.text
            done = wait_run(client, h, run["id"], timeout=60)
        assert done["status"] == "completed", done
        assert server.count("POST") == 2  # the original attempt and the operator-authorized retry
    finally:
        server.close()


def test_a_normal_approval_still_expires_with_its_run(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """The exemption is only for unknown-outcome decisions."""
    server = HangServer()
    try:
        h, stack, ah = build(client, world, owner_session, server, "write_non_idempotent", "POST")
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=0.3):
            wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=60)
            owner_session.execute(
                text("UPDATE shifde.approvals SET expires_at = now() - interval '1 hour' "
                     "WHERE reason = 'tool_side_effect'")
            )  # fmt: skip
            owner_session.commit()
            done = wait_run(client, h, run["id"], {"timed_out"}, timeout=30)
        assert done["error_message"] == "the approval expired"
        assert done["error_class"] == "timeout" and done["outcome_unknown"] is False
    finally:
        server.close()


# Ported in part 2c (B-2422) over `tests/_support/p6util.py` and `sample_db_url`.
def test_pipeline_write_node_in_flight_when_the_worker_dies_then_cancelled(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    hang = PipelineHang()
    try:
        pipe = hang_pipeline(w, hang, write=True)
        run = w.run(pipe["id"], {"doc": {"a": 1}})
        w1 = SubprocessWorker(settings, "w1")
        try:
            assert hang.first_started.wait(60), "the write never started"
            w1.kill()
        finally:
            if w1.proc.poll() is None:
                w1.kill()
        cancel(client, w.h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        row = run_row(owner_session, run["id"])
        assert row["status"] == "cancelled" and row["error_class"] == UNKNOWN, row
        states = step_states(owner_session, run["id"])
        assert (
            all(st != "running" for _, st in states)
            and ("pipeline_node", "indeterminate") in states
        )
        api = get_run(client, w.h, run["id"])
        assert api["outcome_unknown"] is True
        [appr] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert appr["arguments"] == {"node": "post", "type": "sink"} and appr["tool"] is None
        assert w.decide(appr["id"], "mark_succeeded", "checked the target").status_code == 200
        assert get_run(client, w.h, run["id"])["outcome_unknown"] is False
        assert hang.count("POST") == 1
    finally:
        hang.close()


# Not ported here (B-2413): `test_migration_downgrade_restores_the_previous_reaper_and_guard`
# downgrades SETH's `p11_0001` to `p09_0001`, revisions the unified schema does not have
# (`runtime_0001` creates the final reaper and guard, B-2101).
