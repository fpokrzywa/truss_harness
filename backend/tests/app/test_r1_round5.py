"""R1 narrow check: a decided retry that did not ANSWER the question (refused before sending, or
refused by the target) leaves the earlier delivery unknown; one that succeeded closes it.

Moved from SETH 3ba0e06 `backend/tests/test_r1_round5.py` (APP-2 part 2e), assertions unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from shifde.runtime import lifecycle, state, steps
from tests._support.fullapp import World
from tests._support.p5util import (
    SubprocessWorker,
    rest_connection,
    thread_worker,
    wait_for,
    wait_run,
)
from tests._support.p6util import World6, mini
from tests.app.test_p5_worker import HangServer
from tests.app.test_r1_critic import build2, crash, park_live
from tests.app.test_r1_round2 import node_steps, pipeline_killed
from tests.app.test_r1_round3 import ONE, Scripted, start
from tests.app.test_r1_round4 import epochs
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

DNS = "http://no-such-host.invalid"


def repoint(owner: Session, name: str, url: str = DNS) -> None:
    owner.rollback()
    owner.execute(
        text(
            "UPDATE shifde.connections SET config = jsonb_set(config, '{base_url}', "
            "CAST(:u AS jsonb)) WHERE name = :n"
        ),
        {"u": f'"{url}"', "n": name},
    )
    owner.commit()


def get(client: TestClient, h: Any, run_id: str) -> dict[str, Any]:
    from tests._support.p5util import get_run

    return get_run(client, h, run_id)


# ------------------------------------------------------------------ 1: tool path
def test_a_retry_refused_before_sending_leaves_the_first_delivery_unknown(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [first] = pending_unknown(ah, run["id"])
        repoint(owner_session, "hang")  # the retry will fail on a real DNS lookup, unsent
        with thread_worker(settings, lease=3, reap=1000):
            assert decide(ah, first["id"], "retry", "no record at the target").status_code == 200
            done = wait_run(client, h, run["id"], {"failed", "cancelled", "waiting_for_approval"})
        assert done["status"] == "failed", done
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        # the retry's own row is a definite failure; the first delivery is still unanswered
        assert invocation_states(owner_session, run["id"]) == ["resolved_retry", "failed"]
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (2, "pending")]
        [new] = pending_unknown(ah, run["id"])
        assert decide(ah, new["id"], "retry").status_code == 422
        assert decide(ah, new["id"], "mark_succeeded", "seen at the target").status_code == 200
        assert invocation_states(owner_session, run["id"]) == ["resolved_succeeded", "failed"]
        assert ("tool_call", "succeeded") in step_states(owner_session, run["id"])
        assert get(client, h, run["id"])["outcome_unknown"] is False
        assert server.count("POST") == 1
        msg = get(client, h, run["id"])["error_message"]
        assert "retry that ran" not in msg and "1 confirmed succeeded" in msg, msg
    finally:
        server.close()


def test_settle_is_idempotent_for_a_retry_that_did_not_answer(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [first] = pending_unknown(ah, run["id"])
        repoint(owner_session, "hang")
        with thread_worker(settings, lease=3, reap=1000):
            decide(ah, first["id"], "retry", "x")
            wait_run(client, h, run["id"], {"failed", "cancelled"})
        o = owner_session
        for _ in range(3):  # repeating settle opens nothing further
            o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
            o.commit()
        assert epochs(o, run["id"]) == [(0, "approved"), (2, "pending")]
        [new] = pending_unknown(ah, run["id"])
        assert decide(ah, new["id"], "fail", "never reached the target").status_code == 200
        o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})  # no loop
        o.commit()
        assert epochs(o, run["id"]) == [(0, "approved"), (2, "rejected")]
        assert invocation_states(o, run["id"]) == ["resolved_failed", "failed"]
    finally:
        server.close()


# ------------------------------------------------------------------ 1: pipeline paths
def test_a_pipeline_sink_retry_refused_before_sending_leaves_a_decision(
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
        with thread_worker(settings, lease=3, reap=0.3):
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
        [first] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        repoint(owner_session, "hang")
        with thread_worker(settings, lease=3, reap=1000):
            assert w.decide(first["id"], "retry", "no record").status_code == 200
            done = wait_run(client, w.h, run["id"], {"failed", "cancelled"}, timeout=60)
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        assert node_steps(owner_session, run["id"], "post") == [(1, "indeterminate"), (2, "failed")]
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (2, "pending")]
        [new] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert w.decide(new["id"], "mark_succeeded", "seen").status_code == 200
        assert node_steps(owner_session, run["id"], "post")[0] == (1, "succeeded")
        assert hang.count("POST") == 1
    finally:
        hang.close()


@pytest.mark.parametrize("second", ["dns", 400])
def test_an_api_request_retry_that_did_not_answer_leaves_a_decision(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
    second: Any,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    srv = Scripted(["cut", 400] if second == 400 else ["cut"])
    try:
        conn = rest_connection(client, w.h, srv.base_url, name="apiconn")
        node = {
            "id": "push",
            "type": "api_request",
            "config": {
                "connection_id": conn,
                "method": "POST",
                "path_template": "/hang",
                "body": {"v": "{v}"},
            },
            "inputs": {"v": "$params.v"},
        }
        params = {"type": "object", "properties": {"v": {"type": "string"}}}
        pipe = w.pipeline("api-retry", mini([node], [], params=params))
        run = w.run(pipe["id"], {"v": "x"})
        with thread_worker(settings, lease=3, reap=1000):
            w.wait_parked(run["id"], UNKNOWN, timeout=90)  # the first POST was cut
        [first] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        if second == "dns":
            repoint(owner_session, "apiconn")
        with thread_worker(settings, lease=3, reap=1000):
            assert w.decide(first["id"], "retry", "no record").status_code == 200
            done = wait_run(client, w.h, run["id"], {"failed", "cancelled"}, timeout=60)
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        assert [a[1] for a in epochs(owner_session, run["id"])] == ["approved", "pending"]
        assert srv.deliveries == (2 if second == 400 else 1)
        [new] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert w.decide(new["id"], "fail", "the first one never landed").status_code == 200
        assert node_steps(owner_session, run["id"], "push")[0] == (1, "failed")
    finally:
        srv.close()


# ------------------------------------------------------------------ 2: a retry that succeeded closes it
def test_a_retry_that_succeeded_closes_the_question_and_is_not_flagged_again(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)  # ended flagged: epoch 0 pending
        [appr] = pending_unknown(ah, run["id"])
        o = owner_session
        # the history of a run whose retry SUCCEEDED, then the run failed for another reason
        o.execute(
            text(
                "UPDATE shifde.approvals SET status = 'approved', decision = 'retry', "
                "decided_by = requested_by, decided_at = now(), decision_note = 'x' "
                "WHERE id = :a"
            ),
            {"a": appr["id"]},
        )
        o.execute(
            text("UPDATE shifde.tool_invocations SET status = 'resolved_retry' WHERE run_id = :r"),
            {"r": run["id"]},
        )
        o.execute(
            text(
                "INSERT INTO shifde.tool_invocations (id, tenant_id, run_id, call_ref, epoch, "
                "tool_version_id, side_effect_class, idempotency_key, status, request_hash) "
                "SELECT gen_random_uuid(), tenant_id, run_id, call_ref, epoch + 1, "
                "tool_version_id, side_effect_class, 'key-1', 'succeeded', request_hash "
                "FROM shifde.tool_invocations WHERE run_id = :r"
            ),
            {"r": run["id"]},
        )
        o.commit()
        for _ in range(2):
            o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
            o.commit()
        assert epochs(o, run["id"]) == [(0, "approved")]  # nothing was reopened
        assert pending_unknown(ah, run["id"]) == []
    finally:
        server.close()


def test_a_pipeline_retry_that_succeeded_closes_the_question_and_is_not_flagged_again(
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
        cancel(client, w.h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        o = owner_session
        o.execute(
            text(
                "UPDATE shifde.approvals SET status = 'approved', decision = 'retry', "
                "decided_by = requested_by, decided_at = now(), decision_note = 'x' "
                "WHERE run_id = :r"
            ),
            {"r": run["id"]},
        )
        o.execute(  # attempt 2: the retry, sent with a key and SUCCEEDED
            text(
                "INSERT INTO shifde.run_steps (id, tenant_id, run_id, seq, attempt, kind, name, "
                "node_id, status, idempotency_key) SELECT gen_random_uuid(), tenant_id, run_id, "
                "seq, 2, kind, name, node_id, 'succeeded', 'k2' FROM shifde.run_steps "
                "WHERE run_id = :r AND node_id = 'post'"
            ),
            {"r": run["id"]},
        )
        o.commit()
        for _ in range(2):
            o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
            o.commit()
        assert epochs(o, run["id"]) == [(0, "approved")]
        assert [a for a in w.pending_approvals() if a["reason"] == UNKNOWN] == []
    finally:
        hang.close()


# ------------------------------------------------------------------ 4: the recovered write's step class
def test_a_recovered_non_idempotent_write_step_carries_the_unknown_class(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        expire_leases(owner_session)
        reap(settings)
        with thread_worker(settings, lease=3, reap=1000):
            wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=60)
        owner_session.rollback()
        rows = owner_session.execute(
            text(
                "SELECT status, error_class FROM shifde.run_steps WHERE run_id = :r "
                "AND kind = 'tool_call'"
            ),
            {"r": run["id"]},
        ).all()
        assert rows == [("indeterminate", UNKNOWN)]
    finally:
        server.close()


# ------------------------------------------------------------------ 5: parking and finalize both fail
def test_a_failed_parking_and_a_failed_finalize_do_not_lead_to_a_resend(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv = Scripted(["cut"])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=ONE)
        real_insert = steps._insert_approval
        real_finalize = lifecycle.finalize

        def boom(*a: Any, **k: Any) -> None:
            if a[2] == "indeterminate_side_effect":
                raise RuntimeError("database hiccup")
            real_insert(*a, **k)

        def no_finalize(ctx: Any, to_status: str, **kw: Any) -> None:
            if to_status == state.FAILED:
                raise RuntimeError("database still down")
            real_finalize(ctx, to_status, **kw)

        monkeypatch.setattr(steps, "_insert_approval", boom)
        monkeypatch.setattr(lifecycle, "finalize", no_finalize)
        with thread_worker(settings, lease=3, reap=1000):
            wait_for(lambda: srv.deliveries == 1, 60)
            wait_for(lambda: get(client, h, run["id"])["status"] == "running", 30)
        monkeypatch.undo()  # the database is back
        expire_leases(owner_session)
        reap(settings)
        with thread_worker(settings, lease=3, reap=1000):
            done = wait_run(client, h, run["id"], {"waiting_for_approval", "completed", "failed"})
        assert srv.deliveries == 1  # no resend
        assert done["status"] == "waiting_for_approval" and done["outcome_unknown"] is True, done
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        srv.close()


# ------------------------------------------------------------------ DECISIONS: job attempts cap
def test_job_attempts_exhausted_on_an_idempotent_write_ends_flagged_not_plain(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(
            client, world, owner_session, settings, server, "write_idempotent", "key_supported"
        )
        owner_session.execute(
            text(
                "UPDATE shifde.jobs SET attempts = max_attempts, "
                "lease_expires_at = now() - interval '1 second' WHERE status = 'leased'"
            )
        )
        owner_session.commit()
        reap(settings)
        done = get(client, h, run["id"])
        assert done["status"] == "failed" and done["outcome_unknown"] is True, done
        assert done["error_class"] == UNKNOWN
        assert len(pending_unknown(ah, run["id"])) == 1
        _ = (SubprocessWorker, build2)
    finally:
        server.close()
