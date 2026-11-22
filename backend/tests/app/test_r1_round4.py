"""R1 focused check: a decided retry that never ran, max_attempts across worker restarts, the
per-node unknown outcome through child runs, malformed URLs, overlong UTF-8 escapes.

Moved from SETH 3ba0e06 `backend/tests/test_r1_round4.py` (APP-2 part 2e), assertions unchanged.
Not here: `test_a_malformed_base_url_is_a_config_error_not_a_crash` (already in
`tests/runtime/test_steps.py`); `test_a_base_url_ending_in_an_empty_query_or_fragment_is_refused`,
`test_overlong_utf8_escapes_are_refused` and `test_other_non_utf8_bytes_are_ordinary` (already in
`tests/adapters/test_r1_round4_adapters.py`).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from shifde.runtime import gate as gate_mod
from shifde.runtime import state
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p5util import SubprocessWorker, get_run, start_run, thread_worker, wait_run
from tests._support.p6util import World6, agent_node, mini
from tests._support.runs import lose_rows
from tests.app.test_p5_worker import HangServer
from tests.app.test_r1_critic import build2, park_live
from tests.app.test_r1_round2 import node_steps, pipeline_killed
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


def epochs(owner: Session, run_id: str) -> list[tuple[int, str]]:
    owner.rollback()
    return [
        (r[0], r[1])
        for r in owner.execute(
            text(
                "SELECT epoch, status FROM shifde.approvals WHERE run_id = :r "
                "AND reason = 'indeterminate_side_effect' ORDER BY epoch"
            ),
            {"r": run_id},
        )
    ]


# ------------------------------------------------------------------ 1: a decided retry that never ran
def test_a_retry_that_never_ran_leaves_a_new_decision_when_the_run_is_cancelled(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [first] = pending_unknown(ah, run["id"])
        assert decide(ah, first["id"], "retry", "no record at the target").status_code == 200
        cancel(client, h, run["id"])  # no worker: the retry never started
        done = get_run(client, h, run["id"])
        assert done["status"] == "cancelled" and done["error_class"] == UNKNOWN, done
        assert done["outcome_unknown"] is True
        listed = client.get(f"{A}/runs?outcome_unknown=true", headers=h).json()["items"]
        assert [r["id"] for r in listed] == [run["id"]]
        [new] = pending_unknown(ah, run["id"])
        assert new["id"] != first["id"]
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (1, "pending")]
        assert decide(ah, new["id"], "retry").status_code == 422  # the run has ended
        ok = decide(ah, new["id"], "mark_succeeded", "seen at the target")
        assert ok.status_code == 200, ok.text
        # the verdict lands on the invocation and step that hold the unknown delivery
        assert invocation_states(owner_session, run["id"]) == ["resolved_succeeded"]
        assert ("tool_call", "succeeded") in step_states(owner_session, run["id"])
        assert get_run(client, h, run["id"])["outcome_unknown"] is False
        assert server.count("POST") == 1
    finally:
        server.close()


def test_a_retry_denied_by_the_gate_leaves_a_new_decision(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [first] = pending_unknown(ah, run["id"])
        real = gate_mod.check

        def check(*a: Any, **k: Any) -> Any:
            if str(k.get("label", "")).startswith("tool:"):
                raise state.GateDenied(state.AUTHZ_DENIED, "revoked", "no_binding")
            return real(*a, **k)

        monkeypatch.setattr(gate_mod, "check", check)
        with thread_worker(settings, lease=3, reap=1000):
            assert decide(ah, first["id"], "retry", "no record").status_code == 200
            done = wait_run(client, h, run["id"], {"failed", "cancelled"}, timeout=60)
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (1, "pending")]
        [new] = pending_unknown(ah, run["id"])
        assert decide(ah, new["id"], "fail", "never reached the target").status_code == 200
        assert invocation_states(owner_session, run["id"]) == ["resolved_failed"]
        assert server.count("POST") == 1
    finally:
        server.close()


def test_a_pipeline_retry_that_never_ran_leaves_a_new_decision(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    w, hang, run = pipeline_killed(
        client, world, owner_session, settings, rest_server, sample_db_url
    )
    try:
        with thread_worker(settings, lease=3, reap=0.3):  # recovers and parks the run
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
        [first] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert w.decide(first["id"], "retry", "no record").status_code == 200
        cancel(client, w.h, run["id"])  # the retry never started
        done = get_run(client, w.h, run["id"])
        assert done["status"] == "cancelled" and done["outcome_unknown"] is True, done
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (1, "pending")]
        [new] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert w.decide(new["id"], "mark_succeeded", "seen").status_code == 200
        assert node_steps(owner_session, run["id"], "post") == [(1, "succeeded")]
        assert hang.count("POST") == 1
    finally:
        hang.close()


# ------------------------------------------------------------------ 2: max_attempts across restarts
def rearm(server: HangServer) -> None:
    server.log.clear()
    server.first_started.clear()
    server.release.clear()


def kill_during_the_write(
    settings: Settings, server: HangServer, owner: Session, name: str
) -> None:
    w = SubprocessWorker(settings, name)
    try:
        assert server.first_started.wait(60), "the write never started"
        w.kill()
    finally:
        if w.proc.poll() is None:
            w.kill()
    expire_leases(owner)
    reap(settings)


@pytest.mark.parametrize(("attempts", "extra_kills"), [(1, 0), (2, 1)])
def test_an_idempotent_write_is_not_resent_once_max_attempts_is_spent_across_restarts(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    attempts: int,
    extra_kills: int,
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build2(
            client,
            world,
            owner_session,
            server,
            "write_idempotent",
            idempotency="key_supported",
            retry={"max_attempts": attempts, "backoff_s": 0},
        )
        run = start_run(client, h, stack.agent["id"], "go")
        kill_during_the_write(settings, server, owner_session, "w1")  # delivery 1
        deliveries = 1
        for i in range(extra_kills):  # deliveries 2.. each end in a kill
            rearm(server)
            w = SubprocessWorker(settings, f"x{i}")
            try:
                assert server.first_started.wait(60), "the resend never started"
                deliveries += 1
                w.kill()
            finally:
                if w.proc.poll() is None:
                    w.kill()
            expire_leases(owner_session)
            reap(settings)
        rearm(server)
        with thread_worker(settings, lease=3, reap=1000):
            parked = wait_run(client, h, run["id"], {"waiting_for_approval", "completed", "failed"})
        assert parked["status"] == "waiting_for_approval", parked
        assert parked["outcome_unknown"] is True
        assert server.count("POST") == 0  # nothing further was sent: the budget was spent
        assert deliveries == attempts
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        server.close()


def test_an_idempotent_write_within_its_budget_is_resent_under_the_same_key_after_a_restart(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, ah = build2(
            client,
            world,
            owner_session,
            server,
            "write_idempotent",
            idempotency="key_supported",
            retry={"max_attempts": 3, "backoff_s": 0},
        )
        run = start_run(client, h, stack.agent["id"], "go")
        kill_during_the_write(settings, server, owner_session, "w1")
        rearm(server)
        server.log.append(("POST", "/hang"))  # a "first" request already happened: answer at once
        with thread_worker(settings, lease=3, reap=1000):
            done = wait_run(client, h, run["id"], timeout=60)
        assert done["status"] == "completed", done
        assert server.count("POST") == 2  # the marker entry and exactly one resend
        assert invocation_states(owner_session, run["id"]) == ["succeeded"]
    finally:
        server.close()


# ------------------------------------------------------------------ 3: node unknown through child runs
def test_an_agent_node_reports_an_unknown_outcome_from_its_child_and_grandchild(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
    admin_url: str,
    owner_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    nodes = [agent_node(w, "a", "$params.a"), agent_node(w, "b", "$params.b")]
    params = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    pipe = w.pipeline("kids", mini(nodes, [], params=params))
    run = w.run(pipe["id"], {"a": "hi", "b": "hi"})
    with thread_worker(settings):
        w.wait(run["id"])
    o = owner_session
    o.rollback()
    kids = {
        r[0]: r[1]
        for r in o.execute(
            text("SELECT node_id, id FROM shifde.runs WHERE parent_run_id = :r"), {"r": run["id"]}
        )
    }
    assert set(kids) == {"a", "b"}
    assert {n["outcome_unknown"] for n in w.nodes(run["id"]).values()} == {False}
    # an open unknown-outcome decision on node a's child ...
    o.execute(
        text(
            "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, project_id, "
            "call_ref, epoch, reason, side_effect_class, requested_by, expires_at) "
            "SELECT gen_random_uuid(), tenant_id, id, workspace_id, project_id, 'call-1', 0, "
            "'indeterminate_side_effect', 'write_idempotent', initiated_by_principal_id, "
            "now() + interval '1 day' FROM shifde.runs WHERE id = :c"
        ),
        {"c": kids["a"]},
    )
    o.commit()
    nodes_now = w.nodes(run["id"])
    assert nodes_now["a"]["outcome_unknown"] is True
    assert nodes_now["b"]["outcome_unknown"] is False  # the sibling is untouched
    # ... and the same one moved to a grandchild
    grand = "01a1af3d-0000-7000-8000-00000000fa55"
    o.execute(
        text(
            "INSERT INTO shifde.runs (id, tenant_id, workspace_id, project_id, kind, status, "
            "state_version, initiated_by_principal_id, principal_type, agent_id, "
            "agent_version_id, prompt_version_id, root_run_id, parent_run_id, trace_id, "
            "correlation_id, deadline_at, finished_at) SELECT CAST(:g AS uuid), tenant_id, "
            "workspace_id, project_id, kind, 'completed', 3, initiated_by_principal_id, "
            "principal_type, agent_id, agent_version_id, prompt_version_id, root_run_id, id, "
            "trace_id, correlation_id, deadline_at, now() FROM shifde.runs WHERE id = :c"
        ),
        {"g": grand, "c": kids["a"]},
    )
    lose_rows(admin_url, owner_url, "DELETE FROM shifde.approvals WHERE run_id = :c", c=kids["a"])
    o.execute(
        text(
            "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, project_id, "
            "call_ref, epoch, reason, side_effect_class, requested_by, expires_at) "
            "SELECT gen_random_uuid(), tenant_id, id, workspace_id, project_id, 'call-1', 0, "
            "'indeterminate_side_effect', 'write_idempotent', initiated_by_principal_id, "
            "now() + interval '1 day' FROM shifde.runs WHERE id = :g"
        ),
        {"g": grand},
    )
    o.commit()
    nodes_now = w.nodes(run["id"])
    assert nodes_now["a"]["outcome_unknown"] is True
    assert nodes_now["b"]["outcome_unknown"] is False


def test_creating_a_connection_with_a_malformed_base_url_is_a_422(
    client: TestClient, world: World
) -> None:
    from tests._support.p2util import login
    from tests._support.p3util import ADMIN

    h = login(client, world.passwords, ADMIN)
    r = client.post(
        f"{A}/connections",
        headers=h,
        json={
            "name": "bad",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "scope": {},
            "auth_method": "none",
            "access_mode": "read",
            "config": {"base_url": "https://[bad/x"},
        },
    )
    assert r.status_code == 422, r.text
