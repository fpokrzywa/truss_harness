"""SETH 3ba0e06 `backend/tests/test_r1_round2.py`, moved with assertions unchanged.

R1 round 2: a maybe-delivered write of any class parks or is flagged on every path (live cut,
back-off, recovery stopped before the retry), retry wording and journal, pipeline verdicts, the
settle function on a completed run, run-tree and outcome-unknown filters under scope and client,
the audit of a decision taken past the deadline, and a NUL from the model.

Not here: the three path tests are in `tests/adapters/test_r1_round2_adapters.py` and
`test_the_journal_copy_of_arguments_can_always_be_stored` in `tests/runtime/test_steps.py` (moved
earlier). Not ported: `test_downgrade_pushes_pending_unknown_approvals_out_of_reach_of_the_old_expiry`
(SETH migration downgrade; B-2101) and `test_runbook_query_lists_runs_that_ended_before_the_migration`
(SETH's operations runbook and pre-migration runs; B-2101). No setup change was needed."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.core.settings import Settings
from shifde.runtime import gate as gate_mod
from shifde.runtime import state
from shifde.runtime import steps as steps_mod
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post, system_role
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import (
    SubprocessWorker,
    audit_actions,
    events,
    get_run,
    start_run,
    thread_worker,
    wait_for,
    wait_run,
)
from tests._support.p6util import HangServer as PipelineHang
from tests._support.p6util import World6
from tests.app.test_p5_worker import HangServer
from tests.app.test_p6_exec2 import hang_pipeline
from tests.app.test_r1_critic import build2, crash, end_by_cancel, make_parent, park_live
from tests.app.test_r1_unknown_outcome import (
    UNKNOWN,
    cancel,
    decide,
    expire_leases,
    invocation_states,
    pending_unknown,
    reap,
    step_states,
)


def api_run(client: TestClient, h: Any, run: dict[str, Any]) -> dict[str, Any]:
    return get_run(client, h, run["id"])


# ------------------------------------------------------------------ 1: live worker, any write class
def test_idempotent_write_cut_on_a_live_worker_parks_on_the_unknown_outcome(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build2(
            client, world, owner_session, server, "write_idempotent", idempotency="key_supported"
        )
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=1000):
            assert server.first_started.wait(60)
            server.release.set()  # the POST reached the target, its answer is lost
            parked = wait_run(client, h, run["id"], {"waiting_for_approval", "failed"}, timeout=60)
            assert parked["status"] == "waiting_for_approval", parked
            assert parked["waiting_reason"] == UNKNOWN and parked["outcome_unknown"] is True
            assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
            [appr] = pending_unknown(ah, run["id"])
            assert appr["arguments"] == {"item": "x"}
            cancel(client, h, run["id"])
        done = api_run(client, h, run)
        assert done["error_class"] == UNKNOWN and done["outcome_unknown"] is True
        assert server.count("POST") == 1
    finally:
        server.close()


RETRY = {"max_attempts": 3, "backoff_s": 20}


@pytest.mark.parametrize("how", ["cancel", "deadline"])
def test_cancel_or_deadline_during_the_backoff_of_a_maybe_delivered_write(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    how: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # full jitter could draw a delay short enough for the resend to be answered before the
    # deadline: pin the back-off so the run is always still waiting when it is stopped
    monkeypatch.setattr(steps_mod, "backoff_seconds", lambda *a, **k: 30.0)
    server = HangServer()
    try:
        h, stack, ah = build2(
            client,
            world,
            owner_session,
            server,
            "write_idempotent",
            idempotency="key_supported",
            retry=RETRY,
        )
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=1000):
            assert server.first_started.wait(60)
            server.release.set()
            wait_for(
                lambda: "retry_scheduled" in [e["type"] for e in events(client, h, run["id"])], 60
            )
            if how == "cancel":
                cancel(client, h, run["id"])
            else:
                owner_session.execute(
                    text(
                        "UPDATE shifde.runs SET deadline_at = now() + interval '1 second' "
                        "WHERE id = :r"
                    ),
                    {"r": run["id"]},
                )
                owner_session.commit()
            done = wait_run(client, h, run["id"], timeout=60)
        assert done["status"] == ("cancelled" if how == "cancel" else "timed_out")
        assert done["error_class"] == UNKNOWN and done["outcome_unknown"] is True, done
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert all(st != "running" for _, st in step_states(owner_session, run["id"]))
        assert len(pending_unknown(ah, run["id"])) == 1
        assert server.count("POST") == 1  # the retry never started
    finally:
        server.close()


@pytest.mark.parametrize("stop", ["gate", "cancelled", "timed_out"])
def test_recovery_that_stops_before_the_retry_keeps_the_delivery_unknown(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(
            client, world, owner_session, settings, server, "write_idempotent", "key_supported"
        )
        expire_leases(owner_session)
        reap(settings)
        assert api_run(client, h, run)["status"] == "pending"
        if stop == "gate":  # the operator disables the connection after the incident
            owner_session.execute(
                text("UPDATE shifde.connections SET status = 'disabled' WHERE name = 'hang'")
            )
            owner_session.commit()
        else:
            real = gate_mod.check

            def check(*a: Any, **k: Any) -> Any:
                if str(k.get("label", "")).startswith("tool:"):
                    raise state.Cancelled() if stop == "cancelled" else state.TimedOut()
                return real(*a, **k)

            monkeypatch.setattr(gate_mod, "check", check)
        with thread_worker(settings, lease=3, reap=1000):
            done = wait_run(client, h, run["id"], timeout=60)
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert all(st != "running" for _, st in step_states(owner_session, run["id"]))
        assert len(pending_unknown(ah, run["id"])) == 1
        assert server.count("POST") == 1
    finally:
        server.close()


# ------------------------------------------------------------------ 3/5: retry wording and journal
def test_a_retry_that_ran_is_not_reported_as_one_that_never_ran(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [appr] = pending_unknown(ah, run["id"])
        server.log.clear()
        server.first_started.clear()
        server.release.clear()  # the retry's POST hangs too
        assert decide(ah, appr["id"], "retry", "no record at the target").status_code == 200
        w2 = SubprocessWorker(settings, "w2")
        try:
            assert server.first_started.wait(60), "the retry never started"
            w2.kill()
        finally:
            if w2.proc.poll() is None:
                w2.kill()
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        [appr2] = pending_unknown(ah, run["id"])
        assert decide(ah, appr2["id"], "mark_succeeded", "seen").status_code == 200
        done = api_run(client, h, run)
        assert "1 confirmed succeeded" in done["error_message"], done["error_message"]
        assert "retry" not in done["error_message"]
        # one verdict settles the call: it lands on BOTH deliveries, the one it was given for and
        # the earlier one whose own retry decision was consumed
        assert invocation_states(owner_session, run["id"]) == [
            "resolved_succeeded",
            "resolved_succeeded",
        ]
    finally:
        server.close()


def test_a_retry_that_never_ran_is_recorded_as_such(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [appr] = pending_unknown(ah, run["id"])
        assert decide(ah, appr["id"], "retry", "no record at the target").status_code == 200
        cancel(client, h, run["id"])  # no worker: the run never resumed
        done = api_run(client, h, run)
        assert done["status"] == "cancelled"
        assert invocation_states(owner_session, run["id"]) == ["resolved_retry"]
        assert ("tool_call", "indeterminate") in step_states(owner_session, run["id"])
        # the retry never ran, so the first delivery is still unknown: a NEW decision is open
        # (the decided retry itself is not reopened)
        assert done["outcome_unknown"] is True and len(pending_unknown(ah, run["id"])) == 1
    finally:
        server.close()


# ------------------------------------------------------------------ 5: pipelines
def pipeline_killed(
    client: TestClient,
    world: World,
    owner: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> tuple[World6, PipelineHang, dict[str, Any]]:
    w = World6(client, world, owner, rest_server, sample_db_url)
    hang = PipelineHang()
    pipe = hang_pipeline(w, hang, write=True)
    run = w.run(pipe["id"], {"doc": {"a": 1}})
    w1 = SubprocessWorker(settings, "w1")
    try:
        assert hang.first_started.wait(60), "the write never started"
        w1.kill()
    finally:
        if w1.proc.poll() is None:
            w1.kill()
    return w, hang, run


def node_steps(owner: Session, run_id: str, node: str) -> list[tuple[int, str]]:
    owner.rollback()
    return [
        (r[0], r[1])
        for r in owner.execute(
            text(
                "SELECT attempt, status FROM shifde.run_steps WHERE run_id = :r "
                "AND node_id = :n ORDER BY attempt"
            ),
            {"r": run_id, "n": node},
        )
    ]


def test_pipeline_verdict_given_before_cancel_is_journaled(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w, hang, run = pipeline_killed(
        client, world, owner_session, settings, rest_server, sample_db_url
    )
    try:
        with thread_worker(settings, lease=3, reap=0.3):  # recovers and parks the run
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
        [appr] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert w.decide(appr["id"], "mark_succeeded", "seen").status_code == 200
        assert api_run(client, w.h, run)["status"] == "pending"  # not resumed: no worker
        cancel(client, w.h, run["id"])
        assert api_run(client, w.h, run)["status"] == "cancelled"
        assert node_steps(owner_session, run["id"], "post") == [(1, "succeeded")]
        assert hang.count("POST") == 1
    finally:
        hang.close()


def test_a_pipeline_verdict_lands_on_an_older_attempt_whatever_a_later_one_did(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: RestTestServer,
    sample_db_url: str,
) -> None:
    w, hang, run = pipeline_killed(
        client, world, owner_session, settings, rest_server, sample_db_url
    )
    try:
        cancel(client, w.h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        assert node_steps(owner_session, run["id"], "post") == [(1, "indeterminate")]
        o = owner_session
        o.execute(  # a decided verdict for attempt 1 ...
            text(
                "UPDATE shifde.approvals SET status = 'approved', decision = 'mark_succeeded', "
                "decided_by = requested_by, decided_at = now() WHERE run_id = :r"
            ),
            {"r": run["id"]},
        )
        o.execute(  # ... and a later attempt of the same node
            text(
                "INSERT INTO shifde.run_steps (id, tenant_id, run_id, seq, attempt, kind, name, "
                "node_id, status) SELECT gen_random_uuid(), tenant_id, run_id, seq, 2, kind, "
                "name, node_id, 'cancelled' FROM shifde.run_steps "
                "WHERE run_id = :r AND node_id = 'post'"
            ),
            {"r": run["id"]},
        )
        o.commit()
        o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
        o.commit()
        # the verdict covers every earlier delivery of the node; a later attempt that only ended
        # does not stand in its way
        assert node_steps(o, run["id"], "post") == [(1, "succeeded"), (2, "cancelled")]
    finally:
        hang.close()


# ------------------------------------------------------------------ 5: a completed run is left alone
def test_settle_leaves_a_completed_run_with_a_started_write_alone(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        o = owner_session
        done = "01a1af3d-0000-7000-8000-00000000fa44"
        o.execute(
            text(
                "INSERT INTO shifde.runs (id, tenant_id, workspace_id, project_id, kind, status, "
                "state_version, initiated_by_principal_id, principal_type, agent_id, "
                "agent_version_id, prompt_version_id, root_run_id, trace_id, correlation_id, "
                "deadline_at, finished_at) SELECT CAST(:d AS uuid), tenant_id, workspace_id, "
                "project_id, kind, 'completed', 3, initiated_by_principal_id, principal_type, "
                "agent_id, agent_version_id, prompt_version_id, CAST(:d AS uuid), trace_id, "
                "correlation_id, deadline_at, now() FROM shifde.runs WHERE id = :r"
            ),
            {"d": done, "r": run["id"]},
        )
        o.execute(
            text(
                "INSERT INTO shifde.tool_invocations (id, tenant_id, run_id, call_ref, epoch, "
                "tool_version_id, side_effect_class, idempotency_key, status, request_hash) "
                "SELECT gen_random_uuid(), tenant_id, CAST(:d AS uuid), call_ref, epoch, "
                "tool_version_id, side_effect_class, 'other-key', 'started', request_hash "
                "FROM shifde.tool_invocations WHERE run_id = :r"
            ),
            {"d": done, "r": run["id"]},
        )
        o.execute(
            text(
                "INSERT INTO shifde.run_steps (id, tenant_id, run_id, seq, attempt, kind, name, "
                "status) SELECT gen_random_uuid(), tenant_id, CAST(:d AS uuid), 1, 1, 'tool_call', "
                "'target', 'running' FROM shifde.runs WHERE id = :r"
            ),
            {"d": done, "r": run["id"]},
        )
        o.commit()
        assert o.execute(text("SELECT shifde.settle_ended_run(:d)"), {"d": done}).scalar_one() == 0
        o.commit()
        assert invocation_states(o, done) == ["started"]
        assert step_states(o, done) == [("tool_call", "running")]
        assert (
            o.execute(
                text("SELECT count(*) FROM shifde.approvals WHERE run_id = :d"), {"d": done}
            ).scalar_one()
            == 0
        )
    finally:
        server.close()


# ------------------------------------------------------------------ 5: downgrade keeps decisions open
# Not ported: `test_downgrade_pushes_pending_unknown_approvals_out_of_reach_of_the_old_expiry`
# downgrades SETH's database to `p09_0001_installation_profile`; the unified schema is created in
# its final shape by `runtime_0001` and has no SETH revisions to go back to (B-2101, B-2412).


# ------------------------------------------------------------------ 5: scope and tenants
def test_tree_run_id_respects_scope_and_tenant(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        make_parent(owner_session, run["id"])
        end_by_cancel(owner_session, settings, client, h, run)
        parent = "01a1af3d-0000-7000-8000-00000000fa22"
        url = f"approvals?status=pending&tree_run_id={parent}"
        admin = login(client, world.passwords, ADMIN)
        assert len(client.get(f"{A}/{url}", headers=admin).json()["items"]) == 1
        # a second tenant finds nothing through the same run id
        hb = login(client, world.passwords, ADMIN_B)
        res = client.get(f"{B}/{url}", headers=hb)
        assert res.status_code == 200, res.text
        assert res.json()["items"] == []
        # a reader bound to another workspace finds nothing; one bound to the run's finds it
        admin = login(client, world.passwords, ADMIN)
        other = post(client, f"{A}/workspaces", admin, {"slug": "w-tree", "name": "Tree"})
        main = client.get(f"{A}/workspaces", headers=admin).json()["items"]
        main_id = next(w["id"] for w in main if w["slug"] != "w-tree")
        role = post(
            client,
            f"{A}/roles",
            admin,
            {"key": "appr-reader", "name": "ar", "permissions": ["approval:read"]},
        )
        for email, ws_id in (
            ("tree-out@demo-a.local", other["id"]),
            ("tree-in@demo-a.local", main_id),
        ):
            uid = new_user(
                client, login(client, world.passwords, ADMIN), "demo-a", email, "Passw0rd!-test-1"
            )
            world.passwords[email] = "Passw0rd!-test-1"
            bound = client.post(
                f"{A}/role-bindings",
                headers=login(client, world.passwords, ADMIN),
                json={
                    "subject_type": "user",
                    "subject_id": uid,
                    "role_id": role["id"],
                    "scope": {"type": "workspace", "id": ws_id},
                },
            )
            assert bound.status_code == 201, bound.text
        hout = login(client, world.passwords, "tree-out@demo-a.local")
        assert client.get(f"{A}/{url}", headers=hout).json()["items"] == []
        hin = login(client, world.passwords, "tree-in@demo-a.local")
        assert len(client.get(f"{A}/{url}", headers=hin).json()["items"]) == 1
    finally:
        server.close()


def test_outcome_unknown_respects_scope_and_tenant(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        make_parent(owner_session, run["id"])
        end_by_cancel(owner_session, settings, client, h, run)
        parent = "01a1af3d-0000-7000-8000-00000000fa22"
        admin = login(client, world.passwords, ADMIN)
        assert get_run(client, admin, parent)["outcome_unknown"] is True
        # a second tenant sees nothing, not even the existence of the run
        hb = login(client, world.passwords, ADMIN_B)
        assert client.get(f"{B}/runs?outcome_unknown=true", headers=hb).json()["items"] == []
        assert client.get(f"{B}/runs/{parent}", headers=hb).status_code == 404
        # a reader bound to another workspace sees none of it either; one bound to the run's does
        admin = login(client, world.passwords, ADMIN)
        other = post(client, f"{A}/workspaces", admin, {"slug": "w-other", "name": "Other"})
        main = client.get(f"{A}/workspaces", headers=admin).json()["items"]
        main_id = next(w["id"] for w in main if w["slug"] != "w-other")
        role = post(
            client,
            f"{A}/roles",
            admin,
            {"key": "run-reader", "name": "rr", "permissions": ["run:read", "workspace:read"]},
        )
        for email, ws_id in (
            ("outside@demo-a.local", other["id"]),
            ("inside@demo-a.local", main_id),
        ):
            uid = new_user(
                client, login(client, world.passwords, ADMIN), "demo-a", email, "Passw0rd!-test-1"
            )
            world.passwords[email] = "Passw0rd!-test-1"
            bound = client.post(
                f"{A}/role-bindings",
                headers=login(client, world.passwords, ADMIN),
                json={
                    "subject_type": "user",
                    "subject_id": uid,
                    "role_id": role["id"],
                    "scope": {"type": "workspace", "id": ws_id},
                },
            )
            assert bound.status_code == 201, bound.text
        hout = login(client, world.passwords, "outside@demo-a.local")
        assert client.get(f"{A}/runs?outcome_unknown=true", headers=hout).json()["items"] == []
        assert client.get(f"{A}/runs/{parent}", headers=hout).status_code == 404
        hin = login(client, world.passwords, "inside@demo-a.local")
        got = client.get(f"{A}/runs?outcome_unknown=true", headers=hin).json()["items"]
        assert [r["id"] for r in got] == [parent]
        _ = system_role
    finally:
        server.close()


# ------------------------------------------------------------------ 4/9: path escapes
# `test_paths_that_could_leave_the_base_path_are_refused`,
# `test_config_validation_refuses_the_same_paths` and `test_ordinary_paths_still_join` are in
# `tests/adapters/test_r1_round2_adapters.py` (moved earlier, same assertions).


# ------------------------------------------------------------------ 6: the operator query
# Not ported: `test_runbook_query_lists_runs_that_ended_before_the_migration` reads the
# unflagged-runs query from SETH's `docs/operations/RUNBOOK.md`, which the unified repository does
# not have; the query finds runs a SETH release ended before SETH's migration flagged them, and
# the unified schema starts in its final shape (B-2101).


# ------------------------------------------------------------------ 11: audit of the ending decision
def test_deciding_on_a_run_past_its_deadline_audits_the_timeout(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        owner_session.execute(
            text("UPDATE shifde.runs SET deadline_at = now() - interval '1 second' WHERE id = :r"),
            {"r": run["id"]},
        )
        owner_session.commit()
        [appr] = pending_unknown(ah, run["id"])
        assert decide(ah, appr["id"], "mark_succeeded", "seen").status_code == 200
        actions = audit_actions(owner_session, run["id"])
        assert "run.timed_out" in actions
    finally:
        server.close()


# ------------------------------------------------------------------ NUL in arguments
# `test_the_journal_copy_of_arguments_can_always_be_stored` is in `tests/runtime/test_steps.py`
# (moved earlier, same assertions).


def test_a_nul_from_the_model_fails_the_run_before_anything_is_sent(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """The model reply that carries the NUL cannot even be stored (it is jsonb), so the run fails
    `internal` at the model call: the failure comes before the write-ahead row and nothing is
    delivered. (The journal copy is sanitized anyway, see above.)"""
    server = HangServer()
    server.log.append(("POST", "/hang"))  # the "first" request already happened: answer at once
    try:
        h, stack, ah = build2(
            client,
            world,
            owner_session,
            server,
            "write_idempotent",
            idempotency="key_supported",
            args_item="a\u0000b",
        )
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=3, reap=1000):
            done = wait_run(client, h, run["id"], timeout=60)
        assert done["status"] == "failed" and done["error_class"] == "internal", done
        assert server.count("POST") == 1  # only the dummy entry: nothing was sent
        assert invocation_states(owner_session, run["id"]) == []
    finally:
        server.close()
