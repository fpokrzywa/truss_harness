"""Cross-client isolation of the runtime tables and queue functions (runtime_0001).

Client A's context as `shifde_app` neither reads, writes, updates, claims nor reaps client B's
runs, events, artifacts, steps, invocations, approvals or jobs. Composite keys refuse a row of A
that names B's run, workspace, project, tool version, child run or invocation with the same answer
as an unknown id (23503), the schema owner included, and the client-keyed unique keys never let
A's insert collide with B's row (B-2101, B-2102). The definer queue functions refuse a client
context; without one they return each job with its own client id, and only that client's context
then sees the run (B-2103).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.runtime.conftest import TABLES, Two, app_session, insert


def _refused(s: Session, table: str, values: dict[str, Any]) -> str | None:  # Any: columns
    with pytest.raises(DBAPIError) as err, s.begin_nested():
        insert(s, table, **values)
    return getattr(err.value.orig, "sqlstate", None)


def test_a_reads_none_of_bs_runtime_rows(app_db: Database, two: Two) -> None:
    with app_session(app_db, two.a.tenant) as s:
        for table in TABLES:
            seen = {r[0] for r in s.execute(text(f"SELECT tenant_id FROM shifde.{table}"))}
            assert seen == {two.a.tenant}, table
    with app_session(app_db, None) as s:  # no client context: nothing at all
        for table in TABLES:
            assert s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one() == 0


def test_a_updates_none_of_bs_rows(app_db: Database, owner_db: Database, two: Two) -> None:
    b = two.b
    updates = [
        ("UPDATE shifde.runs SET cancel_requested_at = now() WHERE id = :b RETURNING id", b.run),
        ("UPDATE shifde.run_steps SET status = 'failed' WHERE id = :b RETURNING id", b.step),
        (
            "UPDATE shifde.tool_invocations SET status = 'failed' WHERE id = :b RETURNING id",
            b.invocation,
        ),
        ("UPDATE shifde.approvals SET status = 'cancelled' WHERE id = :b RETURNING id", b.approval),
        ("UPDATE shifde.jobs SET status = 'done' WHERE id = :b RETURNING id", b.job),
    ]
    with app_session(app_db, two.a.tenant) as s:
        for sql, target in updates:
            assert s.execute(text(sql), {"b": target}).all() == [], sql
        s.commit()
    with owner_db.session() as o:
        run = o.execute(
            text("SELECT cancel_requested_at FROM shifde.runs WHERE id = :r"), {"r": b.run}
        ).scalar_one()
        job = o.execute(
            text("SELECT status FROM shifde.jobs WHERE id = :j"), {"j": b.job}
        ).scalar_one()
    assert run is None and job == "queued"


def _rows_naming(two: Two, run: UUID, *, tenant: UUID) -> dict[str, dict[str, Any]]:
    a = two.a
    exp = utcnow() + timedelta(hours=1)
    return {
        "run_events": {"id": uuid7(), "tenant_id": tenant, "run_id": run, "seq": 1, "type": "x"},
        "run_artifacts": {"id": uuid7(), "tenant_id": tenant, "run_id": run, "kind": "input",
                          "size_bytes": 2, "content": "{}", "sha256": "x"},
        "run_steps": {"id": uuid7(), "tenant_id": tenant, "run_id": run, "seq": 1,
                      "kind": "model_call", "name": "m"},
        "tool_invocations": {"id": uuid7(), "tenant_id": tenant, "run_id": run, "call_ref": "c",
                             "tool_version_id": a.tool_version, "side_effect_class": "read",
                             "idempotency_key": f"x-{uuid7()}", "status": "started",
                             "request_hash": "h"},
        "approvals": {"id": uuid7(), "tenant_id": tenant, "run_id": run,
                      "workspace_id": a.workspace, "project_id": a.project, "call_ref": "c",
                      "reason": "approval_rule", "requested_by": uuid7(), "expires_at": exp},
        "jobs": {"id": uuid7(), "tenant_id": tenant, "kind": "run.advance", "run_id": run,
                 "dedupe_key": f"d-{uuid7()}"},
        "budget_reservations": {"id": uuid7(), "tenant_id": tenant, "run_id": run,
                                "step_key": "k", "budget_ids": [], "amount": 1,
                                "expires_at": exp},
    }  # fmt: skip


def test_a_cannot_write_rows_for_b(app_db: Database, two: Two) -> None:
    with app_session(app_db, two.a.tenant) as s:
        for table, values in _rows_naming(two, two.b.run, tenant=two.b.tenant).items():
            assert _refused(s, table, values) == "42501", table  # row-level security
        s.rollback()


@pytest.mark.parametrize("as_owner", [False, True], ids=["app-role", "schema-owner"])
def test_a_row_of_a_naming_bs_run_gets_the_unknown_id_answer(
    app_db: Database, owner_db: Database, two: Two, as_owner: bool
) -> None:
    """Every table keyed to a run, the deferred ledger key included: B's run id and an unknown id
    give one answer, 23503, for both roles (constraints ignore BYPASSRLS)."""
    s = owner_db.session() if as_owner else app_session(app_db, two.a.tenant)
    with s:
        for run in (two.b.run, uuid7()):
            for table, values in _rows_naming(two, run, tenant=two.a.tenant).items():
                assert _refused(s, table, values) == "23503", (table, run == two.b.run)
        fk = s.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'fk_ontology_lineage__run'"
            )
        ).scalar_one()
        assert fk == "FOREIGN KEY (tenant_id, run_id) REFERENCES shifde.runs(tenant_id, id)"
        s.rollback()


@pytest.mark.parametrize("as_owner", [False, True], ids=["app-role", "schema-owner"])
def test_a_row_of_a_cannot_name_bs_workspace_project_tool_child_run_or_invocation(
    app_db: Database, owner_db: Database, two: Two, as_owner: bool
) -> None:
    a, b = two.a, two.b
    exp = utcnow() + timedelta(hours=1)
    approval = {"tenant_id": a.tenant, "run_id": a.run, "workspace_id": a.workspace,
                "project_id": a.project, "call_ref": "c", "reason": "approval_rule",
                "requested_by": uuid7(), "expires_at": exp}  # fmt: skip
    step = {"tenant_id": a.tenant, "run_id": a.run, "seq": 7, "kind": "tool_call", "name": "t"}
    run = {"tenant_id": a.tenant, "workspace_id": a.workspace, "project_id": a.project,
           "kind": "agent", "initiated_by_principal_id": uuid7(), "principal_type": "user",
           "trace_id": "t", "correlation_id": "c", "deadline_at": exp}  # fmt: skip
    a_agent = s_agent = None
    s = owner_db.session() if as_owner else app_session(app_db, a.tenant)
    with s:
        a_artifact, a_agent = s.execute(
            text("SELECT agent_id, agent_version_id FROM shifde.runs WHERE id = :r"), {"r": a.run}
        ).one()
        run["agent_id"] = a_artifact  # an agent run names its agent (ck_runs__target, B-2172)
        cases = [
            ("approvals", {**approval, "workspace_id": b.workspace}),
            ("approvals", {**approval, "project_id": b.project}),
            ("approvals", {**approval, "tool_version_id": b.tool_version}),
            ("run_steps", {**step, "child_run_id": b.run}),
            ("run_steps", {**step, "tool_invocation_id": b.invocation}),
            ("run_steps", {**step, "tool_version_id": b.tool_version}),
            ("runs", {**run, "agent_version_id": b.tool_version, "root_run_id": a.run}),
            ("runs", {**run, "agent_version_id": a_agent, "root_run_id": b.run}),
            ("runs", {**run, "agent_version_id": a_agent, "root_run_id": a.run,
                      "parent_run_id": b.run}),
        ]  # fmt: skip
        for table, values in cases:
            assert _refused(s, table, {"id": uuid7(), **values}) == "23503", (table, values)
        # control: the same rows with A's own ids are accepted
        insert(s, "approvals", id=uuid7(), **{**approval, "tool_version_id": a.tool_version})
        insert(s, "run_steps", id=uuid7(), **{**step, "tool_invocation_id": a.invocation})
        insert(s, "runs", id=uuid7(), **{**run, "agent_version_id": a_agent, "root_run_id": a.run})
        s_agent = a_agent
        s.rollback()
    assert s_agent is not None


def test_unique_keys_never_collide_with_bs_rows(app_db: Database, two: Two) -> None:
    """B has event seq 1 and step (1, 1) on its run: A's insert naming B's run with the same key
    gets 23503 like an unknown run, not 23505 (SETH's keys omitted tenant_id, B-2102)."""
    with app_session(app_db, two.a.tenant) as s:
        for run in (two.b.run, uuid7()):
            for table in ("run_events", "run_steps"):
                values = _rows_naming(two, run, tenant=two.a.tenant)[table]
                assert _refused(s, table, values) == "23503", (table, run == two.b.run)
        s.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM shifde.shifde_claim_jobs('w', 10, 30, 0)",
        "SELECT * FROM shifde.shifde_reap_expired_leases()",
        "SELECT shifde.shifde_ops_stats(60)",
    ],
)
def test_the_queue_definers_refuse_a_client_context(
    app_db: Database, owner_db: Database, two: Two, sql: str
) -> None:
    with app_session(app_db, two.a.tenant) as s:
        with pytest.raises(DBAPIError) as err:
            s.execute(text(sql))
        assert getattr(err.value.orig, "sqlstate", None) == "42501"
        s.rollback()
    with owner_db.session() as o:  # nothing was claimed
        leased = o.execute(text("SELECT count(*) FROM shifde.jobs WHERE status <> 'queued'"))
        assert leased.scalar_one() == 0


def test_a_claim_returns_each_job_with_its_own_client(
    app_db: Database, owner_db: Database, two: Two
) -> None:
    """The claim (no client context) sees every client's queued jobs: that is the worker's design.
    It returns (job_id, tenant_id, kind, run_id) only, the tenant id is the job's own, and the run
    is visible under that client's context and under no other."""
    with app_session(app_db, None) as s:
        rows = s.execute(text("SELECT * FROM shifde.shifde_claim_jobs('w1', 10, 30, 0)")).all()
        s.commit()
    assert {(r.job_id, r.tenant_id, r.run_id) for r in rows} == {
        (two.a.job, two.a.tenant, two.a.run),
        (two.b.job, two.b.tenant, two.b.run),
    }
    assert set(rows[0]._fields) == {"job_id", "tenant_id", "kind", "run_id"}
    for row in rows:
        mine, other = (two.a, two.b) if row.tenant_id == two.a.tenant else (two.b, two.a)
        q = text("SELECT tenant_id FROM shifde.runs WHERE id = :r")
        with app_session(app_db, mine.tenant) as s:
            assert s.execute(q, {"r": row.run_id}).scalar_one() == mine.tenant
        with app_session(app_db, other.tenant) as s:
            assert s.execute(q, {"r": row.run_id}).first() is None
    with owner_db.session() as o:
        leased = o.execute(text("SELECT lease_owner FROM shifde.jobs")).scalars().all()
    assert leased == ["w1", "w1"]


def test_ops_stats_returns_aggregates_only(app_db: Database, two: Two) -> None:
    with app_session(app_db, None) as s:
        stats = s.execute(text("SELECT shifde.shifde_ops_stats(60)")).scalar_one()
    assert set(stats) == {"window_seconds", "queue", "runs", "models", "budget_denials"}
    assert stats["queue"]["queued"] == 2
    flat = str(stats)
    for c in (two.a, two.b):
        assert str(c.tenant) not in flat and str(c.run) not in flat


@pytest.mark.parametrize("fn", ["settle_ended_run", "land_verdicts"])
def test_settle_functions_do_nothing_to_bs_run_under_as_context(
    app_db: Database, owner_db: Database, two: Two, fn: str
) -> None:
    """The invoker-rights functions under A's context with B's ended run id see no run."""
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'failed', state_version = state_version + 1, "
                "finished_at = now() WHERE id = :r"
            ),
            {"r": two.b.run},
        )
        o.commit()
    with app_session(app_db, two.a.tenant) as s:
        s.execute(text(f"SELECT shifde.{fn}(:r)"), {"r": two.b.run})
        text_b = s.execute(
            text("SELECT shifde.unknown_outcome_text(:r, 'x')"), {"r": two.b.run}
        ).scalar_one()
        s.commit()
    assert text_b is None
    with owner_db.session() as o:
        inv = o.execute(
            text("SELECT status FROM shifde.tool_invocations WHERE id = :i"),
            {"i": two.b.invocation},
        ).scalar_one()
        approvals = o.execute(
            text("SELECT count(*) FROM shifde.approvals WHERE run_id = :r"), {"r": two.b.run}
        ).scalar_one()
        error = o.execute(
            text("SELECT error_class FROM shifde.runs WHERE id = :r"), {"r": two.b.run}
        ).scalar_one()
    assert (inv, approvals, error) == ("started", 1, None)


def test_unknown_outcome_text_reads_only_the_runs_own_client(owner_db: Database, two: Two) -> None:
    """F4: the approvals, run_events and runs lookups name the run's own client (the reaper calls
    the function as the schema owner, outside row-level security). The composite keys already
    refuse B's rows naming A's run; with them dropped in this rolled-back transaction, B's open
    decision and B's cause still do not reach A's run's message."""
    a, b = two.a, two.b
    with owner_db.session() as o:
        for table, fk in (("approvals", "fk_approvals__run"), ("run_events", "fk_run_events__run")):
            o.execute(text(f"ALTER TABLE shifde.{table} DROP CONSTRAINT {fk}"))
        for c, tool in ((a, "a-tool"), (b, "b-secret-tool")):
            insert(o, "approvals", id=uuid7(), tenant_id=c.tenant, run_id=a.run,
                   workspace_id=c.workspace, project_id=c.project, call_ref=f"u-{tool}",
                   reason="indeterminate_side_effect", tool_key=tool, requested_by=uuid7(),
                   expires_at=utcnow() + timedelta(hours=1))  # fmt: skip
        insert(o, "run_events", id=uuid7(), tenant_id=b.tenant, run_id=a.run, seq=99,
               type="outcome_unknown", details='{"previous_message": "b-secret-cause"}')  # fmt: skip
        got = o.execute(
            text("SELECT shifde.unknown_outcome_text(:r, NULL)"), {"r": a.run}
        ).scalar_one()
        unknown = o.execute(
            text("SELECT shifde.unknown_outcome_text(:r, NULL)"), {"r": uuid7()}
        ).scalar_one()
        o.rollback()
    assert got == (
        "a write may have been delivered before this run ended (pending); its outcome is "
        "unknown until an operator resolves the approval (a-tool)"
    ), got
    assert unknown is None
