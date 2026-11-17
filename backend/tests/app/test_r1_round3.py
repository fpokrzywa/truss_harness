"""R1 critic round 3: counted deliveries at a real scripted target (no double delivery, unknown
stays unknown), decisions for every unknown state, per-node unknown outcome, ordinary paths.

Moved from SETH 3ba0e06 `backend/tests/test_r1_round3.py` (APP-2 part 2e), assertions unchanged.
Not here: `test_a_tool_argument_with_a_space_still_reaches_the_target` (carry_forward owner build,
`build.tool_runtime.fill_path`); `test_ordinary_paths_join_under_the_base`,
`test_whitespace_is_refused_raw_and_dot_segments_hidden_by_it_are_caught` and
`test_a_base_url_with_a_query_or_fragment_is_refused` (already in
`tests/adapters/test_r1_round3_adapters.py`). Setup change (B-2115): `repo.verdict_summary` takes
the client second.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime import state, steps
from shifde.runtime.pipeline import PipelineRunner
from tests._support.fullapp import World
from tests._support.p5util import (
    SubprocessWorker,
    art,
    get_run,
    publish,
    rest_connection,
    rest_tool_spec,
    start_run,
    thread_worker,
    wait_for,
    wait_run,
)
from tests._support.p6util import HangServer as PipelineHang
from tests._support.p6util import World6, mini
from tests.app.test_p5_worker import HangServer
from tests.app.test_p6_exec2 import hang_pipeline
from tests.app.test_r1_critic import build2, crash
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


# ------------------------------------------------------------------ a scripted, counting target
class Scripted:
    """POST /hang answers per script: an int is an HTTP status, "cut" closes without answering.
    Every delivery and its Idempotency-Key are recorded."""

    def __init__(self, script: list[Any], *, hold_first: bool = False) -> None:
        self.script = list(script)
        self.hold_first = hold_first
        self.deliveries = 0
        self.keys: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                return

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                act: Any = 200
                if self.path == "/hang":
                    outer.deliveries += 1
                    outer.keys.append(self.headers.get("Idempotency-Key"))
                    if outer.hold_first and outer.deliveries == 1:
                        outer.first_started.set()  # the first delivery hangs; its client is killed
                        outer.release.wait(60)
                        return
                    act = outer.script.pop(0) if outer.script else 200
                if act == "cut":
                    self.close_connection = True
                    return
                raw = json.dumps({"ok": act == 200}).encode()
                self.send_response(act)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # the pipeline test helpers call these on their servers
        self.first_started = threading.Event()
        self.release = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def count(self, _method: str) -> int:
        return self.deliveries

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


RETRY = {"max_attempts": 3, "backoff_s": 0}
ONE = {"max_attempts": 1, "backoff_s": 0}
PARKED = {"waiting_for_approval"}
ENDED = {"waiting_for_approval", "completed", "failed"}


def start(
    client: TestClient,
    world: World,
    owner: Session,
    srv: Scripted,
    *,
    side: str = "write_idempotent",
    retry: dict[str, Any] = RETRY,
    method: str = "POST",
) -> tuple[dict[str, str], Any, dict[str, Any]]:
    h, stack, ah = build2(
        client, world, owner, srv, side, idempotency="key_supported", retry=retry, method=method
    )
    return h, ah, start_run(client, h, stack.agent["id"], "go")


# ------------------------------------------------------------------ 5: the delivery table
@pytest.mark.parametrize(
    ("script", "retry", "status", "deliveries"),
    [
        ([500, 200], RETRY, "completed", 2),
        (["cut", 200], RETRY, "completed", 2),
        ([429, 200], RETRY, "completed", 2),
        ([400], RETRY, "completed", 1),  # a 4xx answer is an output, not a failure
        # the answer to a resend says nothing about the delivery that may have landed
        ([500, 400], RETRY, "waiting_for_approval", 2),
        # a 429 is the target's definite refusal: three of them end plainly, no decision
        ([429, 429, 429], RETRY, "failed", 3),
        # a 500 left the delivery unknown; later refusals do not make it known
        ([500, 429, 429], RETRY, "waiting_for_approval", 3),
        ([429, 500, 500], RETRY, "waiting_for_approval", 3),
        # the retry budget runs out on an unknown delivery
        ([500, 500, 500], RETRY, "waiting_for_approval", 3),
        ([500], ONE, "waiting_for_approval", 1),
    ],
    ids=[
        "500-then-200",
        "cut-then-200",
        "429-then-200",
        "400-is-an-output",
        "500-then-400-parks",
        "429x3-plain-failure",
        "500-429-429-parks",
        "429-500-500-parks",
        "500x3-budget-exhausted-parks",
        "500-single-attempt-parks",
    ],
)
def test_live_idempotent_writes_never_double_deliver_and_unknown_stays_unknown(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    script: list[Any],
    retry: dict[str, Any],
    status: str,
    deliveries: int,
) -> None:
    srv = Scripted(script)
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=retry)
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, h, run["id"], ENDED, timeout=60)
        assert (done["status"], srv.deliveries) == (status, deliveries), done
        assert done["outcome_unknown"] is (status == "waiting_for_approval")
        assert len(set(srv.keys)) == 1 and srv.keys[0]  # one key for the whole call
        assert len(pending_unknown(ah, run["id"])) == (1 if status == "waiting_for_approval" else 0)
    finally:
        srv.close()


@pytest.mark.parametrize(
    "script",
    [[200], [500, 200], [500, 500, 500], [500, 404], ["cut", 302], [500, 500, 400]],
)
def test_reads_never_park(
    client: TestClient, world: World, owner_session: Session, settings: Settings, script: list[Any]
) -> None:
    srv = Scripted(script)
    try:
        h, ah, run = start(client, world, owner_session, srv, side="read", method="GET")
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, h, run["id"], ENDED, timeout=60)
        assert done["outcome_unknown"] is False
        # a read's 3xx / 4xx answer is its output; only a 5xx run of the whole budget fails it
        last = script[-1]
        answered = last == 200 or (isinstance(last, int) and 300 <= last < 500)
        assert done["status"] == ("completed" if answered else "failed")
        assert pending_unknown(ah, run["id"]) == []
    finally:
        srv.close()


@pytest.mark.parametrize(
    ("verdict", "after", "status", "deliveries"),
    [
        ("retry", [200], "completed", 4),
        ("retry", [429, 429, 429], "failed", 6),
        ("retry", [500, 500, 500], "waiting_for_approval", 6),
        ("mark_succeeded", [], "completed", 3),
        ("fail", [], "failed", 3),
    ],
)
def test_a_parked_idempotent_write_resumed_after_each_verdict(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    verdict: str,
    after: list[Any],
    status: str,
    deliveries: int,
) -> None:
    srv = Scripted([500, 500, 500, *after])
    try:
        h, ah, run = start(client, world, owner_session, srv)
        with thread_worker(settings, lease=5, reap=1000):
            wait_run(client, h, run["id"], PARKED, timeout=60)
            [first] = pending_unknown(ah, run["id"])
            assert decide(ah, first["id"], verdict).status_code == 200

            def settled() -> bool:
                now = get_run(client, h, run["id"])
                fresh = [a for a in pending_unknown(ah, run["id"]) if a["id"] != first["id"]]
                return now["status"] in {"completed", "failed"} or bool(fresh)

            wait_for(settled, 60)
            final = get_run(client, h, run["id"])
        assert final["status"] == status, final
        assert srv.deliveries == deliveries
        if verdict == "retry":  # a NEW key for the authorized retry, one key within it
            assert len(set(srv.keys[:3])) == 1 and srv.keys[3] != srv.keys[0]
            assert len(set(srv.keys[3:])) == 1
        assert not [s for k, s in step_states(owner_session, run["id"]) if s == "running"] or (
            status == "waiting_for_approval"
        )
    finally:
        srv.close()


def test_the_approval_step_is_closed_once_the_decision_is_applied(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    srv = Scripted([500])
    try:
        h, ah, run = start(client, world, owner_session, srv, retry=ONE)
        with thread_worker(settings, lease=5, reap=1000):
            wait_run(client, h, run["id"], PARKED, timeout=60)
            [appr] = pending_unknown(ah, run["id"])
            assert decide(ah, appr["id"], "mark_succeeded", "seen").status_code == 200
            done = wait_run(client, h, run["id"], {"completed", "failed"}, timeout=60)
        assert done["status"] == "completed"
        states = step_states(owner_session, run["id"])
        assert all(st != "running" for _, st in states), states  # no approval step left running
        assert ("approval", "succeeded") in states
    finally:
        srv.close()


# ------------------------------------------------------------------ 1: a decision for every unknown
def test_a_failed_approval_insert_after_delivery_still_opens_a_decision(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv = Scripted(["cut"])
    try:
        h, stack, ah = build2(
            client,
            world,
            owner_session,
            srv,
            "write_idempotent",
            idempotency="key_supported",
            retry=ONE,
        )
        real = steps._insert_approval

        def boom(*a: Any, **k: Any) -> None:
            if a[2] == "indeterminate_side_effect":
                raise RuntimeError("database hiccup")
            real(*a, **k)

        monkeypatch.setattr(steps, "_insert_approval", boom)
        run = start_run(client, h, stack.agent["id"], "go")
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, h, run["id"], {"failed", "waiting_for_approval"}, timeout=60)
        assert srv.deliveries == 1
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        srv.close()


def test_state_without_a_decision_gets_one_when_the_run_ends(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    """Defence in depth: whatever left an `indeterminate` invocation with no approval (a death
    between two commits in an older release, a lost row), the end of the run opens it."""
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        [appr] = pending_unknown(ah, run["id"])
        o = owner_session
        o.execute(text("DELETE FROM shifde.approvals WHERE id = :a"), {"a": appr["id"]})
        o.commit()
        assert pending_unknown(ah, run["id"]) == []
        o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
        o.commit()
        [again] = pending_unknown(ah, run["id"])
        assert again["arguments"] == {"item": "x"}
        # and a decided one is never reopened: a decided retry at that epoch stays decided
        o.execute(
            text(
                "UPDATE shifde.approvals SET status = 'approved', decision = 'retry', "
                "decided_by = requested_by, decided_at = now(), decision_note = 'x' WHERE id = :a"
            ),
            {"a": again["id"]},
        )
        o.execute(
            text("UPDATE shifde.tool_invocations SET status = 'indeterminate' WHERE run_id = :r"),
            {"r": run["id"]},
        )
        o.commit()
        o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
        o.commit()
        # the decided retry is not reopened; since it never ran, the delivery it was meant to
        # replace is unknown again and a NEW decision (epoch 1) is open
        [new] = pending_unknown(ah, run["id"])
        assert new["id"] != again["id"]
        assert invocation_states(o, run["id"]) == ["resolved_retry"]
    finally:
        server.close()


def test_pipeline_write_recovered_then_gate_denied_opens_a_decision(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    hang = PipelineHang()
    try:
        pipe = hang_pipeline(w, hang, write=True)
        run = w.run(pipe["id"], {"doc": {"a": 1}})
        w1 = SubprocessWorker(settings, "w1")
        try:
            assert hang.first_started.wait(60)
            w1.kill()
        finally:
            if w1.proc.poll() is None:
                w1.kill()
        expire_leases(owner_session)
        reap(settings)
        real = PipelineRunner._node_gate

        def denied(self: PipelineRunner, nid: str) -> None:
            if nid == "post":
                raise state.GateDenied("authz_denied", "permission was revoked", "revoked")
            real(self, nid)

        monkeypatch.setattr(PipelineRunner, "_node_gate", denied)
        with thread_worker(settings, lease=5, reap=1000):
            done = wait_run(client, w.h, run["id"], {"failed", "waiting_for_approval"}, timeout=90)
        assert hang.count("POST") == 1
        assert done["outcome_unknown"] is True and done["error_class"] == UNKNOWN, done
        [appr] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert appr["arguments"] == {"node": "post", "type": "sink"}
        assert node_steps(owner_session, run["id"], "post") == [(1, "indeterminate")]
    finally:
        hang.close()


def test_a_decided_pipeline_retry_is_not_reopened_when_the_run_ends(
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
        [first] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        first_id = first["id"]
        o.execute(
            text(
                "UPDATE shifde.approvals SET status = 'approved', decision = 'retry', "
                "decided_by = requested_by, decided_at = now(), decision_note = 'x' "
                "WHERE run_id = :r"
            ),
            {"r": run["id"]},
        )
        o.commit()
        o.execute(text("SELECT shifde.settle_ended_run(:r)"), {"r": run["id"]})
        o.commit()
        [new] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]  # epoch 1, a new one
        assert new["id"] != first_id
        assert node_steps(o, run["id"], "post") == [(1, "indeterminate")]
    finally:
        hang.close()


def test_a_retried_pipeline_node_denied_before_sending_did_not_run(
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
        # attempt 2: the retried node was denied before it sent (no idempotency key) ...
        o.execute(
            text(
                "INSERT INTO shifde.run_steps (id, tenant_id, run_id, seq, attempt, kind, name, "
                "node_id, status) SELECT gen_random_uuid(), tenant_id, run_id, seq, 2, kind, "
                "name, node_id, 'failed' FROM shifde.run_steps WHERE run_id = :r "
                "AND node_id = 'post'"
            ),
            {"r": run["id"]},
        )
        o.commit()
        # B-2115: the repository takes the client second (setup only)
        tenant = o.execute(
            text("SELECT tenant_id FROM shifde.runs WHERE id = :r"), {"r": run["id"]}
        ).scalar_one()
        summary = repo.verdict_summary(o, tenant, run["id"])
        assert "resend succeeded" not in summary and "no verdict recorded" in summary
        o.rollback()
        # ... and when it SUCCEEDED it answered the question
        o.execute(
            text(
                "UPDATE shifde.run_steps SET idempotency_key = 'k', status = 'succeeded' "
                "WHERE run_id = :r AND attempt = 2"
            ),
            {"r": run["id"]},
        )
        o.commit()
        assert "1 resent with an operator's approval and the resend succeeded" in (
            repo.verdict_summary(o, tenant, run["id"])
        )
        o.rollback()
    finally:
        hang.close()


# ------------------------------------------------------------------ 3/4: unknown outcome per node
def test_a_sink_cut_live_then_cancelled_reports_its_node_as_unknown(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    srv = Scripted(["cut"])
    try:
        pipe = hang_pipeline(w, srv, write=True)  # type: ignore[arg-type]
        run = w.run(pipe["id"], {"doc": {"a": 1}})
        with thread_worker(settings, lease=5, reap=1000):
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
            parked = w.nodes(run["id"])
            assert parked["post"]["status"] == "waiting_for_approval"
            assert parked["post"]["outcome_unknown"] is True
            cancel(client, w.h, run["id"])
        nodes = w.nodes(run["id"])
        assert nodes["post"]["outcome_unknown"] is True
        assert nodes["post"]["status"] == "failed"
        assert nodes["post"]["error_class"] == UNKNOWN
        assert nodes["t"]["outcome_unknown"] is False  # the other node is not touched
        assert nodes["t"]["status"] == "succeeded"
        [appr] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
        assert w.decide(appr["id"], "mark_succeeded", "seen").status_code == 200
        after = w.nodes(run["id"])
        assert after["post"]["outcome_unknown"] is False
        assert srv.deliveries == 1
    finally:
        srv.close()


def test_a_tool_node_with_an_unknown_write_is_reported_as_such(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    rest_server: Any,
    sample_db_url: str,
) -> None:
    w = World6(client, world, owner_session, rest_server, sample_db_url)
    srv = Scripted(["cut"])
    try:
        conn = rest_connection(client, w.h, srv.base_url, name="hang")
        spec = rest_tool_spec(
            conn,
            "POST",
            "/hang",
            "write_idempotent",
            body=True,
            idempotency="key_supported",
            retry=ONE,
        )
        tool = art(client, w.h, "tools", "wtool", spec, w.scope)
        publish(client, w.h, "tools", tool["id"])
        params = {"type": "object", "properties": {"echo": {"type": "object"}}}
        nodes_spec = [
            {
                "id": "tl",
                "type": "tool",
                "config": {"tool_version_id": "latest:" + tool["id"]},
                "inputs": {"args": "$params.echo"},
            }
        ]
        pipe = w.pipeline("wtool-p", mini(nodes_spec, [], params=params))
        run = w.run(pipe["id"], {"echo": {"item": "x"}})
        with thread_worker(settings, lease=5, reap=1000):
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
            cancel(client, w.h, run["id"])
        n = w.nodes(run["id"])["tl"]
        assert n["outcome_unknown"] is True
        assert (n["status"], n["error_class"]) == ("failed", UNKNOWN), n  # not "cancelled", null
        assert srv.deliveries == 1
    finally:
        srv.close()
