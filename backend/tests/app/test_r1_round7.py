"""R1 critic round 7: a recovering worker never delivers a call whose answer is already recorded
and never exceeds `max_attempts` for a key, a strict output schema failing after an unknown
delivery still counts as the delivery succeeding, and a back-off that is stopped leaves the
delivery unknown (step included).

Moved from SETH 3ba0e06 `backend/tests/test_r1_round7.py` (APP-2 part 2e), assertions unchanged.
Not here: `test_every_caller_of_a_tool_call_goes_through_the_guarded_runtime` (already in
`tests/runtime/test_pipeline.py`).
SETH's `test_r1_model` helpers come from `tests/_support/r1model.py` (that file is not ported).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from shifde.runtime import lifecycle, state
from shifde.runtime import steps as steps_mod
from tests._support.fullapp import World
from tests._support.p3util import A
from tests._support.p5util import thread_worker, wait_for, wait_run
from tests._support.r1model import DONE, pipeline
from tests.app import test_r1_critic as crit
from tests.app.test_r1_round2 import events, node_steps
from tests.app.test_r1_round3 import ENDED, ONE, RETRY, Scripted, start
from tests.app.test_r1_round4 import epochs
from tests.app.test_r1_unknown_outcome import (
    UNKNOWN,
    cancel,
    decide,
    invocation_states,
    pending_unknown,
    step_states,
)

STRICT = {
    "type": "object",
    "required": ["nope"],
    "properties": {"nope": {"type": "string"}},
}


def strict_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every REST tool built by the tests declares an output schema the target's answer violates."""
    real = crit.rest_tool_spec

    def wrapped(*a: Any, **k: Any) -> dict[str, Any]:
        spec = real(*a, **k)
        spec["output_schema"] = STRICT
        return spec

    monkeypatch.setattr(crit, "rest_tool_spec", wrapped)


def die_before_finalize(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The worker loses its lease once, after the failing attempt was committed and before the
    run was finalized (the window between the last journal write and the end of the run)."""
    real = lifecycle.finalize
    fired: list[int] = []

    def wrapped(ctx: Any, to_status: str, **k: Any) -> Any:
        if to_status == "failed" and not fired:
            fired.append(1)
            ctx.session.rollback()
            raise state.LeaseLost()
        return real(ctx, to_status, **k)

    monkeypatch.setattr(lifecycle, "finalize", wrapped)
    return fired


# ------------------------------------------------------------------ 1: the recorded answer stands
@pytest.mark.parametrize("side", ["write_non_idempotent", "write_idempotent"])
def test_death_after_a_schema_violation_does_not_send_again(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    strict_schema(monkeypatch)
    fired = die_before_finalize(monkeypatch)
    srv = Scripted([200, 200, 200])
    try:
        h, ah, run = start(client, world, owner_session, srv, side=side, retry=ONE)
        with thread_worker(settings, lease=2, reap=0.3):
            if side == "write_non_idempotent":
                wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=60)
                [appr] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
                assert decide(ah, appr["id"], "approve", "").status_code == 200
            wait_for(lambda: fired, timeout=60)
            done = wait_run(client, h, run["id"], DONE, timeout=90)
        assert srv.deliveries == 1, (srv.deliveries, done)
        assert done["status"] == "failed" and done["error_class"] == state.SCHEMA_VIOLATION, done
        assert done["outcome_unknown"] is False
        assert invocation_states(owner_session, run["id"]) == ["succeeded"]
    finally:
        srv.close()


def test_death_after_a_429_with_the_budget_spent_does_not_send_again(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fired = die_before_finalize(monkeypatch)
    srv = Scripted([429, 200])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=ONE)
        with thread_worker(settings, lease=2, reap=0.3):
            wait_for(lambda: fired, timeout=60)
            done = wait_run(client, h, run["id"], DONE, timeout=90)
        assert srv.deliveries == 1, (srv.deliveries, done)
        assert done["status"] == "failed" and done["error_class"] == state.RATE_LIMITED, done
    finally:
        srv.close()


def test_death_after_a_429_with_budget_left_still_resends(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is the recorded outcome and the budget, not a ban on recovery: a 429 is a
    definite refusal and `max_attempts` allows another try."""
    srv = Scripted([429, 200])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=RETRY)
        with thread_worker(settings, lease=2, reap=0.3):
            done = wait_run(client, h, run["id"], DONE, timeout=90)
        assert done["status"] == "completed" and srv.deliveries == 2, (srv.deliveries, done)
    finally:
        srv.close()


def test_death_after_a_refused_retry_does_not_send_a_third_time_and_stays_open(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fired = die_before_finalize(monkeypatch)
    srv = Scripted(["cut", 409, 200, 200])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=ONE)
        with thread_worker(settings, lease=2, reap=0.3):
            wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=60)
            [first] = pending_unknown(ah, run["id"])
            assert decide(ah, first["id"], "retry").status_code == 200
            wait_for(lambda: fired, timeout=60)
            done = wait_run(client, h, run["id"], DONE, timeout=90)
        assert srv.deliveries == 2, (srv.deliveries, done)
        # the recorded 409 ends the run exactly as the dead worker would have: failed, and still
        # open (the first delivery was never answered), with a new decision
        assert done["status"] == "failed", done
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        assert invocation_states(owner_session, run["id"]) == ["resolved_retry", "failed"]
        assert epochs(owner_session, run["id"]) == [(0, "approved"), (2, "pending")]
    finally:
        srv.close()


def test_death_after_a_refused_pipeline_write_does_not_send_again(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same window on the pipeline write path: the node's failed step is final, so a
    recovering worker only finishes the run."""
    fired = die_before_finalize(monkeypatch)
    srv = Scripted([409, 200, 200])
    try:
        w, run = pipeline(client, world, owner_session, rest_server, sample_db_url, srv)
        with thread_worker(settings, lease=2, reap=0.3):
            wait_for(lambda: fired, timeout=60)
            done = wait_run(w.client, w.h, run["id"], DONE, timeout=90)
        assert srv.deliveries == 1, (srv.deliveries, done)
        assert done["status"] == "failed", done
        assert node_steps(owner_session, run["id"], "post") == [(1, "failed")]
    finally:
        srv.close()


# ------------------------------------------------------------------ 3: a strict schema after an unknown delivery
def test_a_2xx_failing_a_strict_output_schema_after_a_cut_counts_as_delivered(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strict_schema(monkeypatch)
    srv = Scripted(["cut", 200])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=RETRY)
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, h, run["id"], ENDED, timeout=60)
        assert done["status"] == "failed" and done["error_class"] == state.SCHEMA_VIOLATION, done
        assert done["outcome_unknown"] is False
        assert srv.deliveries == 2
        assert invocation_states(owner_session, run["id"]) == ["succeeded"]
        assert pending_unknown(ah, run["id"]) == []
    finally:
        srv.close()


# ------------------------------------------------------------------ 11: a stopped back-off
def test_a_stopped_backoff_leaves_the_step_unknown_too(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(steps_mod, "backoff_seconds", lambda *a, **k: 30.0)
    srv = Scripted([500, 200])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=RETRY)
        with thread_worker(settings, lease=5, reap=1000):
            wait_for(
                lambda: "retry_scheduled" in [e["type"] for e in events(client, h, run["id"])], 60
            )
            cancel(client, h, run["id"])
            done = wait_run(client, h, run["id"], DONE, timeout=60)
        assert done["status"] == "cancelled" and done["outcome_unknown"] is True, done
        assert srv.deliveries == 1  # the resend never started
        # the attempt that was cut read `failed` while it waited to be retried; once the run
        # ended it is the unknown delivery, not a plain failure
        assert [s for k, s in step_states(owner_session, run["id"]) if k == "tool_call"] == [
            "indeterminate"
        ]
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        srv.close()
