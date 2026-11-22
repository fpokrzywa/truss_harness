"""SETH 3ba0e06 `backend/tests/test_r1_critic.py`, moved with assertions unchanged.

R1 critic round: every write class is flagged, the tree shows an unknown outcome, journaled
arguments, wording, verdicts in all cases, the guard, filters, the invoker-rights path, the reaper
branches, and the same destination rule for identity providers.

Not here: the three database-free path and header tests of section 9 are in
`tests/adapters/test_r1_critic_adapters.py`. Setup change (B-2115): the python settle path test
calls `repository.get_run` / `repository.transition` with the client second. The `env` fixture is
SETH's (the full app wired to the fake IdP), built as in `tests/app/test_oidc.py`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core import db as dbm
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime.settle import settle_terminal
from tests._support.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeOidcProvider
from tests._support.fullapp import World
from tests._support.p2util import login, new_user, post, system_role, tenant_id
from tests._support.p3util import ADMIN, REST_TOKEN, A, activate, connection, secret
from tests._support.p5util import (
    Stack,
    SubprocessWorker,
    audit_actions,
    get_run,
    limited_user,
    rest_tool_spec,
    start_run,
    thread_worker,
    user_session,
    wait_for,
    wait_run,
)
from tests.app.oidc_helpers import DISCOVERY, oidc_app
from tests.app.test_p5_worker import SCRIPT, HangServer
from tests.app.test_r1_unknown_outcome import (
    UNKNOWN,
    cancel,
    decide,
    expire_leases,
    invocation_states,
    pending_unknown,
    reap,
    run_row,
    step_states,
)


@pytest.fixture
def provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest.fixture
def env(settings: Settings, world: World, provider: FakeOidcProvider) -> Iterator[TestClient]:
    """App wired to the fake IdP (no network); JWKS refresh is unthrottled."""
    with oidc_app(settings, provider) as c:
        yield c


# ------------------------------------------------------------------ builders
def build2(
    client: TestClient,
    world: World,
    owner: Session,
    server: HangServer,
    side: str,
    *,
    idempotency: str = "none",
    args_item: str = "x",
    method: str = "POST",
    retry: dict[str, Any] | None = None,
) -> tuple[dict[str, str], Stack, Any]:
    h = login(client, world.passwords, ADMIN)
    conn = connection(
        client,
        h,
        {
            "name": "hang",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "access_mode": "read_write",
            "auth_method": "bearer",
            "secret_id": secret(client, h, "hang-token", REST_TOKEN),
            "config": {"base_url": server.base_url, "health_path": "/hang-health"},
        },
    )
    activate(client, h, conn["id"])
    script = [SCRIPT[0], {"match": "go", "reply": _call(args_item)}]
    stack = Stack(
        client,
        h,
        owner,
        script=script,
        tool_key="target",
        tool=rest_tool_spec(
            conn["id"],
            method,
            "/hang",
            side,
            body=method == "POST",
            idempotency=idempotency,
            retry=retry,
        ),
    )
    limited_user(
        client,
        owner,
        login(client, world.passwords, ADMIN),
        world.passwords,
        "approver@demo-a.local",
        ["workspace:read", "project:read", "run:read", "approval:read", "approval:decide"],
    )
    return (
        login(client, world.passwords, ADMIN),
        stack,
        user_session(client, world.passwords, "approver@demo-a.local"),
    )


def _call(item: str) -> str:
    from tests._support.p5util import tool_call_reply

    return tool_call_reply("target", {"item": item})


def crash(
    client: TestClient,
    world: World,
    owner: Session,
    settings: Settings,
    server: HangServer,
    side: str = "write_non_idempotent",
    idempotency: str = "none",
) -> tuple[dict[str, str], Any, dict[str, Any]]:
    """The write reaches the target, then the worker process is killed."""
    h, stack, ah = build2(client, world, owner, server, side, idempotency=idempotency)
    run = start_run(client, h, stack.agent["id"], "go")
    w1 = SubprocessWorker(settings, "w1")
    try:
        if side != "write_idempotent":  # an idempotent write needs no approval first
            wait_run(client, h, run["id"], {"waiting_for_approval"}, timeout=90)
            [appr] = ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
            assert decide(ah, appr["id"], "approve", "").status_code == 200
        assert server.first_started.wait(60), "the write never started"
        w1.kill()
    finally:
        if w1.proc.poll() is None:
            w1.kill()
    assert get_run(client, h, run["id"])["status"] == "running"
    return h, ah, run


def end_by_cancel(owner: Session, settings: Settings, client: TestClient, h: Any, run: Any) -> None:
    cancel(client, h, run["id"])
    expire_leases(owner)
    reap(settings)


def end_by_deadline(owner: Session, settings: Settings, run: Any) -> None:
    owner.execute(
        text("UPDATE shifde.runs SET deadline_at = now() - interval '1 second' WHERE id = :r"),
        {"r": run["id"]},
    )
    owner.commit()
    expire_leases(owner)
    reap(settings)


# ------------------------------------------------------------------ 1: write_idempotent
@pytest.mark.parametrize("how", ["cancel", "deadline"])
def test_a_killed_idempotent_write_is_flagged_too(
    client: TestClient, world: World, owner_session: Session, settings: Settings, how: str
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(
            client, world, owner_session, settings, server, "write_idempotent", "key_supported"
        )
        if how == "cancel":
            end_by_cancel(owner_session, settings, client, h, run)
        else:
            end_by_deadline(owner_session, settings, run)
        row = run_row(owner_session, run["id"])
        assert row["error_class"] == UNKNOWN, row
        assert row["status"] == ("cancelled" if how == "cancel" else "timed_out")
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert all(st != "running" for _, st in step_states(owner_session, run["id"]))
        # 3: no earlier approval exists for this class, yet the decision shows the call
        [appr] = pending_unknown(ah, run["id"])
        assert appr["arguments"] == {"item": "x"} and appr["tool"]["key"] == "target"
        assert appr["tool"]["side_effect_class"] == "write_idempotent"
        assert decide(ah, appr["id"], "mark_succeeded").status_code == 200
        assert server.count("POST") == 1
    finally:
        server.close()


def test_journaled_arguments_equal_what_the_approval_shows(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        owner_session.rollback()
        stored = owner_session.execute(
            text("SELECT arguments FROM shifde.tool_invocations WHERE run_id = :r"),
            {"r": run["id"]},
        ).scalar_one()
        earlier = owner_session.execute(
            text(
                "SELECT arguments FROM shifde.approvals WHERE run_id = :r "
                "AND reason = 'tool_side_effect'"
            ),
            {"r": run["id"]},
        ).scalar_one()
        assert stored == earlier == {"item": "x"}
        # reads never journal arguments
    finally:
        server.close()


def test_reads_do_not_store_arguments_in_the_journal(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, stack, _ = build2(client, world, owner_session, server, "read", method="GET")
        run = start_run(client, h, stack.agent["id"], "go")
        w = SubprocessWorker(settings, "w1")
        try:
            assert server.first_started.wait(60)
            w.kill()
        finally:
            if w.proc.poll() is None:
                w.kill()
        owner_session.rollback()
        rows = owner_session.execute(
            text("SELECT arguments FROM shifde.tool_invocations WHERE run_id = :r"),
            {"r": run["id"]},
        ).all()
        assert rows == [(None,)]
    finally:
        server.close()


# ------------------------------------------------------------------ 2: the tree
PARENT = "01a1af3d-0000-7000-8000-00000000fa22"


def make_parent(owner: Session, child_id: str) -> None:
    owner.execute(
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
        {"p": PARENT, "c": child_id},
    )
    owner.execute(
        text("UPDATE shifde.runs SET parent_run_id = :p, root_run_id = :p WHERE id = :c"),
        {"p": PARENT, "c": child_id},
    )
    owner.commit()


def test_a_childs_unknown_write_is_visible_at_the_top_and_in_the_filter(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        make_parent(owner_session, run["id"])
        runs = f"{A}/runs?outcome_unknown=true"
        assert client.get(runs, headers=h).json()["items"] == []  # negative: nothing open yet
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        # the parent was woken and is a plain pending run; the child ended with an open decision
        top = get_run(client, h, PARENT)
        assert top["outcome_unknown"] is True, top
        assert get_run(client, h, run["id"])["outcome_unknown"] is True
        listed = client.get(runs, headers=h).json()["items"]
        assert [r["id"] for r in listed] == [PARENT]  # top-level runs only, by their tree
        assert client.get(f"{A}/runs", headers=h).json()["items"][0]["outcome_unknown"] is True
        [appr] = pending_unknown(ah, run["id"])
        assert decide(ah, appr["id"], "fail", "never reached the target").status_code == 200
        assert get_run(client, h, PARENT)["outcome_unknown"] is False
        assert client.get(runs, headers=h).json()["items"] == []  # negative again
    finally:
        server.close()


def test_approvals_can_be_listed_by_run_tree_so_a_parent_can_show_its_childs_decision(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        make_parent(owner_session, run["id"])
        cancel(client, h, run["id"])
        expire_leases(owner_session)
        reap(settings)
        [appr] = pending_unknown(ah, run["id"])

        def in_tree(run_id: str) -> list[str]:
            url = f"{A}/approvals?status=pending&tree_run_id={run_id}"
            res = client.get(url, headers=ah)
            assert res.status_code == 200, res.text
            return [a["id"] for a in res.json()["items"]]

        assert in_tree(PARENT) == [appr["id"]]  # the child's decision, found from the parent
        assert in_tree(run["id"]) == [appr["id"]]  # a run's tree includes the run itself
        assert in_tree("00000000-0000-7000-8000-00000000abcd") == []  # negative: other tree
        own = client.get(f"{A}/approvals?status=pending&run_id={PARENT}", headers=ah)
        assert own.json()["items"] == []  # run_id still means that run alone
    finally:
        server.close()


# ------------------------------------------------------------------ 4: wording
def test_the_cause_survives_and_the_wording_fits_several_decisions(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        end_by_deadline(owner_session, settings, run)
        owner_session.rollback()
        events = owner_session.execute(
            text(
                "SELECT details FROM shifde.run_events WHERE run_id = :r "
                "AND type = 'outcome_unknown'"
            ),
            {"r": run["id"]},
        ).scalar_one()
        assert events["previous_message"] == "the run deadline passed"  # the original error is kept
        assert events["previous_error_class"] == "timeout"
        assert "(the run deadline passed)" in run_row(owner_session, run["id"])["error_message"]
        # a second open decision on the same run (two writes in flight)
        owner_session.execute(
            text(
                "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, project_id, "
                "step_seq, call_ref, epoch, reason, tool_version_id, tool_key, tool_version_no, "
                "side_effect_class, arguments, requested_by, expires_at) "
                "SELECT gen_random_uuid(), tenant_id, run_id, workspace_id, project_id, step_seq, "
                "'call-2', 0, reason, tool_version_id, tool_key, tool_version_no, "
                "side_effect_class, arguments, requested_by, expires_at FROM shifde.approvals "
                "WHERE run_id = :r AND reason = 'indeterminate_side_effect'"
            ),
            {"r": run["id"]},
        )
        owner_session.commit()
        first, second = sorted(pending_unknown(ah, run["id"]), key=lambda a: a["id"])
        assert decide(ah, first["id"], "mark_succeeded", "seen").status_code == 200
        mid = get_run(client, h, run["id"])
        assert mid["outcome_unknown"] is True and "unknown" in mid["error_message"]
        assert decide(ah, second["id"], "fail", "absent").status_code == 200
        done = get_run(client, h, run["id"])
        assert done["outcome_unknown"] is False
        msg = done["error_message"]
        assert "the run deadline passed" in msg, msg
        assert "1 confirmed" in msg and "1 declared" in msg, msg
        assert done["status"] == "timed_out"
    finally:
        server.close()


# ------------------------------------------------------------------ 5/7: the guard
def test_runs_guard_allows_only_the_flag_and_refuses_clearing_it(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        end_by_cancel(owner_session, settings, client, h, run)
        o = owner_session

        def attempt(sql: str) -> None:
            with pytest.raises(DBAPIError, match="terminal state and immutable"):
                o.execute(text(sql), {"r": run["id"]})
            o.rollback()

        attempt("UPDATE shifde.runs SET error_class = 'cancelled' WHERE id = :r")  # clear the flag
        attempt("UPDATE shifde.runs SET error_class = NULL WHERE id = :r")
        attempt("UPDATE shifde.runs SET attempt_count = 99 WHERE id = :r")  # other columns
        attempt(
            "UPDATE shifde.runs SET attempt_count = 99, error_message = 'x' WHERE id = :r"
        )  # the flag does not license other changes
        o.execute(
            text("UPDATE shifde.runs SET error_message = 'reworded' WHERE id = :r"),
            {"r": run["id"]},
        )
        o.commit()  # rewording while flagged is the one allowed change
        assert run_row(o, run["id"])["error_message"] == "reworded"
        # a completed run can never be flagged
        done = "01a1af3d-0000-7000-8000-00000000fa33"
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
        o.commit()
        with pytest.raises(DBAPIError, match="terminal state and immutable"):
            o.execute(
                text(
                    "UPDATE shifde.runs SET error_class = 'indeterminate_side_effect' WHERE id = :d"
                ),
                {"d": done},
            )
        o.rollback()
    finally:
        server.close()


# ------------------------------------------------------------------ 5: decide past the deadline
def park_live(
    client: TestClient, world: World, owner: Session, settings: Settings, server: HangServer
) -> tuple[dict[str, str], Any, dict[str, Any]]:
    """Park a run on an unknown outcome with a live worker, then stop the worker."""
    h, stack, ah = build2(client, world, owner, server, "write_non_idempotent")
    run = start_run(client, h, stack.agent["id"], "go")
    with thread_worker(settings, lease=3, reap=1000):
        appr = wait_for(
            lambda: ah.client.get(f"{A}/approvals?status=pending", headers=ah).json()["items"]
        )
        assert decide(ah, appr[0]["id"], "approve", "").status_code == 200
        assert server.first_started.wait(60)
        server.release.set()
        wait_run(client, h, run["id"], {"waiting_for_approval"})
    return h, ah, run


def test_deciding_on_a_run_parked_past_its_deadline(
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
        retry = decide(ah, appr["id"], "retry")
        assert retry.status_code == 422, retry.text
        assert get_run(client, h, run["id"])["status"] == "waiting_for_approval"  # nothing changed
        assert len(pending_unknown(ah, run["id"])) == 1
        ok = decide(ah, appr["id"], "mark_succeeded", "seen in the target")
        assert ok.status_code == 200, ok.text
        done = get_run(client, h, run["id"])
        assert done["status"] == "timed_out" and done["outcome_unknown"] is False
        assert invocation_states(owner_session, run["id"]) == ["resolved_succeeded"]
        assert "confirmed" in done["error_message"]
    finally:
        server.close()


# ------------------------------------------------------------------ 5: separation of duties, 409
def test_separation_of_duties_and_409_on_an_ended_run(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        end_by_cancel(owner_session, settings, client, h, run)
        [appr] = pending_unknown(ah, run["id"])
        own = client.post(
            f"{A}/approvals/{appr['id']}/decide",
            headers=h,
            json={"decision": "fail", "comment": "mine"},
        )  # the initiator is the admin
        assert own.status_code == 403 and "separation_of_duties" in own.text, own.text
        assert len(pending_unknown(ah, run["id"])) == 1  # nothing was recorded
        assert decide(ah, appr["id"], "fail", "absent").status_code == 200
        again = decide(ah, appr["id"], "fail", "again")
        assert again.status_code == 409, again.text
    finally:
        server.close()


# ------------------------------------------------------------------ 8: verdicts in every case
def test_fail_on_a_parked_run_is_journaled(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)
        [appr] = pending_unknown(ah, run["id"])
        assert decide(ah, appr["id"], "fail", "the target has no record").status_code == 200
        assert get_run(client, h, run["id"])["status"] == "failed"
        assert invocation_states(owner_session, run["id"]) == ["resolved_failed"]
        assert ("tool_call", "failed") in step_states(owner_session, run["id"])
    finally:
        server.close()


def test_mark_succeeded_then_cancel_before_the_run_resumes_is_journaled(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = park_live(client, world, owner_session, settings, server)  # worker stopped
        [appr] = pending_unknown(ah, run["id"])
        assert decide(ah, appr["id"], "mark_succeeded", "seen").status_code == 200
        assert get_run(client, h, run["id"])["status"] == "pending"  # not resumed: no worker
        cancel(client, h, run["id"])
        assert get_run(client, h, run["id"])["status"] == "cancelled"
        assert invocation_states(owner_session, run["id"]) == ["resolved_succeeded"]
        assert ("tool_call", "succeeded") in step_states(owner_session, run["id"])
        assert get_run(client, h, run["id"])["outcome_unknown"] is False
    finally:
        server.close()


# ------------------------------------------------------------------ 5: python path under RLS
def test_the_python_settle_path_runs_as_the_app_role_and_opens_the_approval(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    app_db: Database,
) -> None:
    """Audit ARCHITECTURE_REVIEW:243: the outcome commit fails, the process survives and the
    generic handler makes the run `failed/internal`; the journal still says `started`."""
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        rid = UUID(run["id"])
        tid = tenant_id(owner_session, "demo-a")
        with app_db.session() as s:
            dbm.init_context(s, "r1-test")
            dbm.set_tenant(s, tid)
            assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
            cur = repo.get_run(s, tid, rid)
            assert cur is not None
            row = repo.transition(
                s,
                tid,
                rid,
                "running",
                "failed",
                token=cur["lease_token"],
                clear_lease=True,
                finished_at=utcnow(),
                error_class="internal",
                error_message="internal error (OperationalError)",
            )
            assert row is not None
            settle_terminal(s, row, "failed", settings)
            s.commit()
        row2 = run_row(owner_session, run["id"])
        assert row2["status"] == "failed" and row2["error_class"] == UNKNOWN
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        [appr] = pending_unknown(ah, run["id"])
        assert appr["arguments"] == {"item": "x"}
        # the audit block of settle_terminal
        owner_session.rollback()
        ev = owner_session.execute(
            text(
                "SELECT outcome, actor_id, details FROM shifde.audit_events "
                "WHERE action = 'run.indeterminate_side_effect' AND target_id = :r"
            ),
            {"r": run["id"]},
        ).one()
        assert ev[0] == "failure" and ev[1] is None
        assert ev[2]["ended_as"] == "failed" and ev[2]["open_decisions"] == 1
        assert ev[2]["run_id"] == run["id"]
    finally:
        server.close()


def test_api_cancel_of_a_pending_run_after_lease_loss(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        expire_leases(owner_session)
        reap(settings)  # lease lost: the run is pending again, nobody holds it
        assert get_run(client, h, run["id"])["status"] == "pending"
        cancel(client, h, run["id"])  # the API finalizes it in-process (settle_terminal)
        done = get_run(client, h, run["id"])
        assert done["status"] == "cancelled" and done["error_class"] == UNKNOWN
        assert done["outcome_unknown"] is True
        assert "run.indeterminate_side_effect" in audit_actions(owner_session, run["id"])
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        server.close()


# ------------------------------------------------------------------ 5: attempts exhausted
def test_attempts_exhausted_while_the_run_is_running(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        owner_session.execute(
            text(
                "UPDATE shifde.jobs SET attempts = max_attempts, "
                "lease_expires_at = now() - interval '1 second' WHERE status = 'leased'"
            )
        )
        owner_session.commit()
        reap(settings)
        row = run_row(owner_session, run["id"])
        assert row["status"] == "failed" and row["error_class"] == UNKNOWN, row
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert all(st != "running" for _, st in step_states(owner_session, run["id"]))
        assert len(pending_unknown(ah, run["id"])) == 1
        assert "run.indeterminate_side_effect" in audit_actions(owner_session, run["id"])
    finally:
        server.close()


def test_attempts_exhausted_before_the_run_was_claimed_again(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    server = HangServer()
    try:
        h, ah, run = crash(client, world, owner_session, settings, server)
        expire_leases(owner_session)
        reap(settings)  # pending again, job queued
        assert get_run(client, h, run["id"])["status"] == "pending"
        owner_session.execute(
            text(
                "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'gone', "
                "attempts = max_attempts, lease_expires_at = now() - interval '1 second' "
                "WHERE run_id = :r"
            ),
            {"r": run["id"]},
        )
        owner_session.commit()
        reap(settings)  # the worker died after claiming the job, attempts are gone
        row = run_row(owner_session, run["id"])
        assert row["status"] == "failed" and row["error_class"] == UNKNOWN, row
        assert invocation_states(owner_session, run["id"]) == ["indeterminate"]
        assert len(pending_unknown(ah, run["id"])) == 1
    finally:
        server.close()


# ------------------------------------------------------------------ 9: paths and headers
# `test_join_url_refuses_dot_segments_in_any_encoding`, `test_join_url_still_joins_ordinary_paths`
# and `test_rest_config_refuses_dot_segment_paths_and_forbidden_header_names` are in
# `tests/adapters/test_r1_critic_adapters.py` (moved earlier, same assertions).


# ------------------------------------------------------------------ 10: scoped administrators
def test_a_workspace_administrator_cannot_repoint_a_connection_on_a_tenant_scoped_secret(
    client: TestClient, world: World, owner_session: Session, rest_server: Any
) -> None:
    from tests._support.p3util import rest_body

    admin = login(client, world.passwords, ADMIN)
    sid = secret(client, admin, "tenant-secret", REST_TOKEN)  # tenant scope
    main = next(
        w["id"]
        for w in client.get(f"{A}/workspaces", headers=admin).json()["items"]
        if w["slug"] == "main"
    )
    ws_sid = str(
        post(
            client,
            f"{A}/secrets",
            admin,
            {
                "name": "ws-secret",
                "value": REST_TOKEN,
                "scope": {"type": "workspace", "id": main},
            },
        )["id"]
    )
    conn = connection(
        client,
        admin,
        {**rest_body(rest_server, ws_sid, "ws-conn"), "scope": {"workspace": "main"}},
    )
    # B-2784: connections_0002 (#156 L1) refuses a workspace connection on a tenant-scoped secret,
    # so the API can no longer create this state. The schema owner makes it here (the guard off
    # for this one transaction only) to keep proving the API's own refusal (FR-CON-011) as
    # defense in depth behind the database guard; the refusal and its assertions are unchanged.
    owner_session.execute(
        text("SELECT pg_catalog.set_config('app.tenant_id', :t, true)"),
        {"t": str(tenant_id(owner_session, "demo-a"))},
    )
    owner_session.execute(
        text("ALTER TABLE shifde.connections DISABLE TRIGGER trg_connections_guard")
    )
    owner_session.execute(
        text("UPDATE shifde.connections SET secret_ref_id = :s WHERE id = :c"),
        {"s": sid, "c": conn["id"]},
    )
    owner_session.execute(
        text("ALTER TABLE shifde.connections ENABLE TRIGGER trg_connections_guard")
    )
    owner_session.commit()
    uid = new_user(
        client,
        login(client, world.passwords, ADMIN),
        "demo-a",
        "wsadmin@demo-a.local",
        "Passw0rd!-test-1",
    )
    world.passwords["wsadmin@demo-a.local"] = "Passw0rd!-test-1"
    ws = client.get(f"{A}/workspaces", headers=login(client, world.passwords, ADMIN)).json()
    ws_id = ws["items"][0]["id"]
    bound = client.post(
        f"{A}/role-bindings",
        headers=login(client, world.passwords, ADMIN),
        json={
            "subject_type": "user",
            "subject_id": uid,
            "role_id": str(system_role(owner_session, "workspace_administrator")),
            "scope": {"type": "workspace", "id": ws_id},
        },
    )
    assert bound.status_code == 201, bound.text
    hw = login(client, world.passwords, "wsadmin@demo-a.local")
    row = client.get(f"{A}/connections/{conn['id']}", headers=hw).json()
    r = client.patch(
        f"{A}/connections/{conn['id']}",
        headers=hw,
        json={"config": {"base_url": "http://127.0.0.1:9", "health_path": "/h"},
              "row_version": row["row_version"]},
    )  # fmt: skip
    assert r.status_code == 403, r.text
    assert "needs secret:use on the bound secret" in r.text
    renamed = client.patch(
        f"{A}/connections/{conn['id']}",
        headers=hw,
        json={"name": "ws-conn-2", "row_version": row["row_version"]},
    )
    assert renamed.status_code == 200  # the rest of the administrator's job is unaffected


# ------------------------------------------------------------------ 6: identity providers
IDP = "/api/v1/orgs/demo-a/idps"
NEW_ISSUER = "https://other-idp.example.test/realms/t"
NEW_DISCOVERY = NEW_ISSUER + "/.well-known/openid-configuration"


def _idp(env: TestClient, h: dict[str, str]) -> dict[str, Any]:
    return post(
        env,
        IDP,
        h,
        {
            "display_name": "Corp",
            "issuer": ISSUER,
            "discovery_url": DISCOVERY,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )


def _patch_idp(
    env: TestClient, h: dict[str, str], idp: dict[str, Any], body: dict[str, Any]
) -> Any:
    cur = env.get(f"{IDP}/{idp['id']}", headers=h).json()
    return env.patch(
        f"{IDP}/{idp['id']}", headers=h, json={**body, "row_version": cur["row_version"]}
    )


def test_changing_the_issuer_or_discovery_url_needs_secret_use_on_the_bound_secret(
    env: TestClient, world: World, owner_session: Session
) -> None:
    admin = login(env, world.passwords, ADMIN)
    idp = _idp(env, admin)
    limited_user(
        env,
        owner_session,
        login(env, world.passwords, ADMIN),
        world.passwords,
        "idpman@demo-a.local",
        ["idp:manage"],
    )
    h = login(env, world.passwords, "idpman@demo-a.local")
    for body in (
        {"issuer": NEW_ISSUER},
        {"discovery_url": NEW_DISCOVERY},
        {"issuer": NEW_ISSUER, "discovery_url": NEW_DISCOVERY},
    ):
        r = _patch_idp(env, h, idp, body)
        assert r.status_code == 403 and "secret:use" in r.text, (body, r.text)
    got = env.get(f"{IDP}/{idp['id']}", headers=h).json()
    assert got["issuer"] == ISSUER and got["client_secret_set"] is True
    # other fields are the idp:manage holder's business
    assert _patch_idp(env, h, idp, {"display_name": "Renamed"}).status_code == 200
    same = _patch_idp(env, h, idp, {"issuer": ISSUER, "discovery_url": DISCOVERY})
    assert same.status_code == 200  # not a change
    # replacing the secret in the same request is allowed (the old one stays where it was)
    swap = _patch_idp(env, h, idp, {"issuer": NEW_ISSUER, "client_secret": "brand-new-secret-9"})
    assert swap.status_code == 200 and swap.json()["issuer"] == NEW_ISSUER


def test_idp_destination_change_with_secret_use_and_for_the_tenant_admin(
    env: TestClient, world: World, owner_session: Session
) -> None:
    admin = login(env, world.passwords, ADMIN)
    idp = _idp(env, admin)
    limited_user(
        env,
        owner_session,
        login(env, world.passwords, ADMIN),
        world.passwords,
        "idpuse@demo-a.local",
        ["idp:manage", "secret:use", "secret:read"],
    )
    h = login(env, world.passwords, "idpuse@demo-a.local")
    assert _patch_idp(env, h, idp, {"issuer": NEW_ISSUER}).status_code == 200
    back = _patch_idp(env, login(env, world.passwords, ADMIN), idp, {"issuer": ISSUER})
    assert back.status_code == 200  # the tenant administrator holds idp:manage and secret:use
