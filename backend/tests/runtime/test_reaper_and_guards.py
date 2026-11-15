"""SQL half of C1 (charter section 5 item 3) and the run guards (runtime_0001, SETH p11_0001).

A worker that dies while an approved write is in flight leaves the write journaled `started` and
its step `running`. When the reaper then ends the run (cancel requested while the lease was lost,
deadline passed, or attempts exhausted) the run must not read as a plain cancel / timeout /
failure: `settle_ended_run` marks the invocation and the step `indeterminate`, opens an
`indeterminate_side_effect` approval and records the unknown outcome on the run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.core.db import Database
from shifde.runtime import state
from tests.runtime.conftest import Two, app_session

SETUPS = {
    # the lease expires with attempts left (run -> pending), then the cancel finalizes it
    "cancel": (
        "UPDATE shifde.runs SET cancel_requested_at = now() WHERE id = :r",
        "cancelled",
        "cancelled",
    ),
    # the lease expires with attempts left, then the deadline ends the run
    "deadline": (
        "UPDATE shifde.runs SET deadline_at = now() - interval '1 second' WHERE id = :r",
        "timed_out",
        "the run deadline passed",
    ),
    # the lease expires with no attempt left: the worker was lost repeatedly
    "attempts": (
        "UPDATE shifde.jobs SET attempts = max_attempts WHERE run_id = :r",
        "failed",
        "the worker was lost repeatedly",
    ),
}


def _crash_during_approved_write(owner_db: Database, two: Two, setup: str) -> None:
    """A's run is `running` under job `two.a.job`, its write invocation `started`, its tool step
    `running` (the approval for it was granted), and the worker died: the lease has expired."""
    a = two.a
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.approvals SET status = 'approved', decision = 'approve', "
                "decided_by = requested_by, decided_at = now() WHERE id = :p"
            ),
            {"p": a.approval},
        )
        o.execute(
            text(
                "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'dead-worker', "
                "attempts = 1, lease_expires_at = now() - interval '1 second' WHERE id = :j"
            ),
            {"j": a.job},
        )
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'running', state_version = state_version + 1, "
                "lease_job_id = :j, started_at = now() WHERE id = :r"
            ),
            {"j": a.job, "r": a.run},
        )
        o.execute(text(SETUPS[setup][0]), {"r": a.run})
        o.commit()


@pytest.mark.parametrize("setup", sorted(SETUPS))
def test_a_crash_during_an_approved_write_is_an_unknown_outcome_not_a_plain_end(
    app_db: Database, owner_db: Database, two: Two, setup: str
) -> None:
    a = two.a
    _crash_during_approved_write(owner_db, two, setup)
    with app_session(app_db, None) as s:  # the worker's reaper: no client context
        reaped = s.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        s.commit()
    status, cause = SETUPS[setup][1], SETUPS[setup][2]
    assert (a.tenant, a.run, "unknown_outcome") in {tuple(r) for r in reaped}
    assert {r.run_id for r in reaped} == {a.run}  # B's healthy run is untouched
    with app_session(app_db, a.tenant) as s:
        run = s.execute(
            text("SELECT status, error_class, error_message FROM shifde.runs WHERE id = :r"),
            {"r": a.run},
        ).one()
        inv = s.execute(
            text("SELECT status, error_class FROM shifde.tool_invocations WHERE id = :i"),
            {"i": a.invocation},
        ).one()
        step = s.execute(
            text("SELECT status FROM shifde.run_steps WHERE id = :i"), {"i": a.step}
        ).scalar_one()
        opened = s.execute(
            text(
                "SELECT call_ref, status, tool_key, context_summary FROM shifde.approvals "
                "WHERE run_id = :r AND reason = 'indeterminate_side_effect'"
            ),
            {"r": a.run},
        ).all()
        events = (
            s.execute(
                text("SELECT type FROM shifde.run_events WHERE run_id = :r ORDER BY seq"),
                {"r": a.run},
            )
            .scalars()
            .all()
        )
    assert run.status == status  # the transition that happened
    assert run.error_class == state.INDETERMINATE  # reported: unknown outcome, not the plain end
    assert run.error_message.startswith(
        f"a write may have been delivered before this run ended ({cause})"
    ), run.error_message
    assert "its outcome is unknown until an operator resolves the approval" in run.error_message
    assert tuple(inv) == ("indeterminate", state.INDETERMINATE)
    assert step == "indeterminate"
    assert [(o.call_ref, o.status, o.context_summary) for o in opened] == [
        ("c1", "pending", {"after_run_end": True})
    ]
    assert opened[0].tool_key is not None
    assert events[-1] == "outcome_unknown"
    with owner_db.session() as o:  # B's run, job and invocation are as they were
        b = o.execute(
            text(
                "SELECT r.status, r.error_class, j.status, t.status FROM shifde.runs r "
                "JOIN shifde.jobs j ON j.run_id = r.id JOIN shifde.tool_invocations t "
                "ON t.run_id = r.id WHERE r.id = :r"
            ),
            {"r": two.b.run},
        ).one()
    assert tuple(b) == ("pending", None, "queued", "started")


def test_reaping_again_changes_nothing(app_db: Database, owner_db: Database, two: Two) -> None:
    _crash_during_approved_write(owner_db, two, "cancel")
    with app_session(app_db, None) as s:
        s.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        s.commit()
        again = s.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        settled = s.execute(text("SELECT count(*) FROM shifde.approvals")).scalar_one()
        s.commit()
    assert again == [] and settled == 0  # no client context: the app role sees no approvals
    with owner_db.session() as o:
        n = o.execute(
            text("SELECT count(*) FROM shifde.approvals WHERE reason = 'indeterminate_side_effect'")
        ).scalar_one()
    assert n == 1


def test_an_expired_unknown_outcome_approval_does_not_time_the_run_out(
    app_db: Database, owner_db: Database, two: Two
) -> None:
    """J-M3: a run waiting on an "outcome unknown" decision past its `expires_at` stays waiting
    and the decision stays open (a human decides it); an ordinary approval would time it out."""
    a = two.a
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'running', state_version = state_version + 1 "
                "WHERE id = :r"
            ),
            {"r": a.run},
        )
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'waiting_for_approval', "
                "state_version = state_version + 1 WHERE id = :r"
            ),
            {"r": a.run},
        )
        o.execute(
            text("UPDATE shifde.approvals SET status = 'cancelled' WHERE id = :p"),
            {"p": a.approval},
        )
        o.execute(
            text(
                "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, project_id, "
                "call_ref, reason, requested_by, expires_at) SELECT gen_random_uuid(), tenant_id, "
                "id, workspace_id, project_id, 'c1', 'indeterminate_side_effect', "
                "initiated_by_principal_id, now() - interval '1 second' FROM shifde.runs "
                "WHERE id = :r"
            ),
            {"r": a.run},
        )
        o.execute(text("UPDATE shifde.jobs SET status = 'done' WHERE run_id = :r"), {"r": a.run})
        o.commit()
    with app_session(app_db, None) as s:
        reaped = s.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        s.commit()
    assert a.run not in {r.run_id for r in reaped}
    with owner_db.session() as o:
        status = o.execute(
            text("SELECT status FROM shifde.runs WHERE id = :r"), {"r": a.run}
        ).scalar_one()
        pending = o.execute(
            text(
                "SELECT count(*) FROM shifde.approvals WHERE run_id = :r AND status = 'pending' "
                "AND reason = 'indeterminate_side_effect'"
            ),
            {"r": a.run},
        ).scalar_one()
    assert (status, pending) == ("waiting_for_approval", 1)


def test_allowed_transitions_match_the_table(app_db: Database) -> None:
    with app_session(app_db, None) as s:
        rows = s.execute(text("SELECT from_status, to_status FROM shifde.run_state_transitions"))
        table = {(a, b) for a, b in rows}
    assert table == {(a, b) for a, targets in state.ALLOWED.items() for b in targets}


@pytest.mark.parametrize("as_owner", [False, True], ids=["app-role", "schema-owner"])
def test_the_run_guard_holds_for_every_role(
    app_db: Database, owner_db: Database, two: Two, as_owner: bool
) -> None:
    """An illegal transition, a status change without a new state_version and any change to a
    terminal run are refused (23514), the BYPASSRLS schema owner included (B-1940)."""
    a = two.a
    s = owner_db.session() if as_owner else app_session(app_db, a.tenant)
    with s:
        for sql in (
            "UPDATE shifde.runs SET status = 'completed', state_version = state_version + 1 "
            "WHERE id = :r",  # pending -> completed is not a legal transition
            "UPDATE shifde.runs SET status = 'running' WHERE id = :r",
        ):
            with pytest.raises(DBAPIError) as err, s.begin_nested():
                s.execute(text(sql), {"r": a.run})
            assert getattr(err.value.orig, "sqlstate", None) == "23514", sql
        s.execute(
            text(
                "UPDATE shifde.runs SET status = 'failed', state_version = state_version + 1, "
                "error_class = 'internal' WHERE id = :r"
            ),
            {"r": a.run},
        )
        with pytest.raises(DBAPIError) as err, s.begin_nested():
            s.execute(text("UPDATE shifde.runs SET error_class = 'x' WHERE id = :r"), {"r": a.run})
        assert "terminal state" in str(err.value)
        s.rollback()
