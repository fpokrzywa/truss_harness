"""`shifde.runtime.repository` (RT-3, B-2115..B-2119): every function that takes a run, job, step,
invocation, approval or artifact id gives client A naming client B's id the answer of an unknown
id, as `shifde_app` with A's context (row-level security) and as the schema owner (no row-level
security: only the function's own `tenant_id` filter keeps B's rows away). Inserts choose their
own ids; enqueue and the reaper name their conflict target."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, NoResultFound
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.ledger import pricing
from shifde.runtime import repository as repo
from tests._support.runs import add_run
from tests.build.env_support import add_published
from tests.runtime.conftest import TABLES, Client, Two, app_session, insert
from tests.tenancy.conftest import owner_member, owner_person

TOKEN = UUID("00000000-0000-7000-8000-00000000a7e3")
WORKER = "rt3-worker"


@dataclass(frozen=True)
class Ids:
    tenant: UUID
    run: UUID
    job: UUID  # leased by WORKER
    queued_job: UUID
    step: UUID
    invocation: UUID
    inv_key: str
    approval: UUID  # pending, tool_side_effect, call_ref c0
    artifact: UUID
    child_pending: UUID
    child_done: UUID  # failed, flagged indeterminate_side_effect, a started write on it
    pipeline_run: UUID
    workspace: UUID
    principal: UUID
    idem: str
    service_account: UUID
    api_key: UUID
    user: UUID  # a person who is a member of this client only
    project: UUID  # the run's project (in `workspace`)


def _seed_client(o: Session, c: Client) -> Ids:
    t = c.tenant
    principal = uuid7()
    o.execute(
        text(
            "UPDATE shifde.jobs SET status = 'leased', lease_owner = :w, attempts = 1, "
            "lease_expires_at = now() + interval '1 hour' WHERE id = :j"
        ),
        {"w": WORKER, "j": c.job},
    )
    o.execute(
        text(
            "UPDATE shifde.runs SET status = 'running', state_version = state_version + 1, "
            "lease_token = :tok, lease_job_id = :j, started_at = now(), "
            "initiated_by_principal_id = :p, idempotency_key = 'idem' WHERE id = :r"
        ),
        {"tok": TOKEN, "j": c.job, "p": principal, "r": c.run},
    )
    # the claim's status event (`last_status_change`, RT-10)
    insert(o, "run_events", id=uuid7(), tenant_id=t, run_id=c.run, seq=2, type="state_change",
           from_status="pending", to_status="running")  # fmt: skip
    o.execute(text("UPDATE shifde.runs SET event_seq = 2 WHERE id = :r"), {"r": c.run})
    queued_job = uuid7()
    insert(o, "jobs", id=queued_job, tenant_id=t, kind="run.advance", run_id=c.run,
           dedupe_key=f"run:{c.run}:99")  # fmt: skip
    o.execute(
        text("UPDATE shifde.tool_invocations SET status = 'indeterminate' WHERE id = :i"),
        {"i": c.invocation},
    )
    insert(o, "approvals", id=uuid7(), tenant_id=t, run_id=c.run, workspace_id=c.workspace,
           project_id=c.project, call_ref="c1", reason="indeterminate_side_effect",
           requested_by=uuid7(), expires_at=utcnow() + timedelta(hours=1), status="approved",
           decision="mark_succeeded", decided_by=uuid7(), decided_at=utcnow())  # fmt: skip
    insert(o, "approvals", id=uuid7(), tenant_id=t, run_id=c.run, workspace_id=c.workspace,
           project_id=c.project, call_ref="c2", reason="indeterminate_side_effect",
           requested_by=uuid7(), expires_at=utcnow() + timedelta(hours=1))  # fmt: skip
    family = {"workspace_id": c.workspace, "project_id": c.project, "parent_run_id": c.run,
              "root_run_id": c.run, "depth": 1}  # fmt: skip
    child_pending = add_run(o, t, **family)
    child_done = add_run(o, t, **family, status="failed", finished_at=utcnow(),
                         error_class="indeterminate_side_effect", error_message="m")  # fmt: skip
    insert(o, "tool_invocations", id=uuid7(), tenant_id=t, run_id=child_done, call_ref="c9",
           tool_version_id=c.tool_version, side_effect_class="write_non_idempotent",
           idempotency_key=f"k9-{child_done}", status="started", request_hash="h",
           arguments='{"item": "x"}')  # fmt: skip
    insert(o, "run_steps", id=uuid7(), tenant_id=t, run_id=c.run, seq=2, kind="sub_agent",
           name="child", child_run_id=child_done)  # fmt: skip
    # a classification raised by the child: its root's tree reads it (`tree_classifications`)
    repo.add_event(o, {"tenant_id": t, "id": child_done}, "data_classification_raised",
                   details={"classification": "confidential"})  # fmt: skip
    pipeline, pipeline_version = add_published(o, t, "pipelines", f"pl-{uuid7().hex}", {})
    pipeline_run = add_run(o, t, kind="pipeline", pipeline_id=pipeline,
                           pipeline_version_id=pipeline_version)  # fmt: skip
    service_account, api_key = uuid7(), uuid7()
    insert(o, "service_accounts", id=service_account, tenant_id=t, name="sa-rt3",
           owner_principal_id=uuid7())  # fmt: skip
    insert(o, "api_keys", id=api_key, tenant_id=t, name="key-rt3", principal_type="user",
           principal_id=principal, prefix=api_key.hex[-8:], key_hash=b"h" * 32,
           scopes=["read"], expires_at=utcnow() + timedelta(days=1),
           created_by=uuid7())  # fmt: skip
    artifact = o.execute(
        text("SELECT id FROM shifde.run_artifacts WHERE run_id = :r"), {"r": c.run}
    ).scalar_one()
    user = owner_person(o, "rt3-user")
    owner_member(o, t, user)
    # a priced event of the run (`run_cost`, RT-6)
    ledger.record(o, {"tenant_id": t, "event_key": f"cost-{c.run}", "event_type": "tool_call",
                      "run_id": c.run, "root_run_id": c.run, "call_count": 1,
                      "costed": pricing.Costed("calculated", Decimal("1.25"), "USD", None, None)})  # fmt: skip
    return Ids(t, c.run, c.job, queued_job, c.step, c.invocation, f"k-{c.invocation}",
               c.approval, artifact, child_pending, child_done, pipeline_run, c.workspace,
               principal, "idem", service_account, api_key, user, c.project)  # fmt: skip


@pytest.fixture
def seeded(owner_db: Database, two: Two) -> tuple[Ids, Ids]:
    with owner_db.session() as o:
        pair = (_seed_client(o, two.a), _seed_client(o, two.b))
        o.commit()
    return pair


def _unknown(x: Ids) -> Ids:
    """Every id fresh and known to no client; the other arguments as `x`'s."""
    fresh: dict[str, Any] = {  # Any: field values
        f: uuid7() for f, v in vars(x).items() if isinstance(v, UUID) and f != "tenant"
    }
    return replace(x, **fresh, inv_key=f"k-{uuid7()}")


Case = Callable[[Session, UUID, Ids], Any]  # Any: whatever the function returns

LIST_RUNS = {"after": None, "limit": 50, "status": None, "kind": None, "agent_id": None,
             "project_id": None, "initiated_by": None, "since": None, "until": None}  # fmt: skip


def _sa_row(t: UUID, x: Ids) -> dict[str, Any]:  # Any: run row values
    """A run row as a caller builds it: A's client id with another run's principal ids."""
    return {"tenant_id": t, "id": x.run, "principal_type": "service_account",
            "initiated_by_principal_id": x.service_account, "api_key_id": x.api_key}  # fmt: skip


CASES: dict[str, Case] = {
    "get_run": lambda s, t, x: repo.get_run(s, t, x.run),
    "run_by_idempotency": lambda s, t, x: repo.run_by_idempotency(s, t, x.principal, x.idem),
    "run_totals": lambda s, t, x: repo.run_totals(s, t, [x.run]),
    "list_runs": lambda s, t, x: repo.list_runs(s, t, workspace_id=x.workspace, **LIST_RUNS),
    "list_runs_outcome_unknown": lambda s, t, x: repo.list_runs(
        s, t, workspace_id=x.workspace, outcome_unknown=True, **LIST_RUNS
    ),
    "fence": lambda s, t, x: repo.fence(s, t, x.run, TOKEN),
    "run_lease": lambda s, t, x: repo.run_lease(s, t, x.run),
    "principal_type_of": lambda s, t, x: repo.principal_type_of(s, t, x.service_account),
    "initiator_controllers": lambda s, t, x: repo.initiator_controllers(s, _sa_row(t, x)),
    "control_state": lambda s, t, x: repo.control_state(s, t, x.run),
    "transition": lambda s, t, x: repo.transition(
        s, t, x.run, "running", "failed", token=TOKEN, clear_lease=True, finished_at=utcnow()
    ),
    "add_event": lambda s, t, x: repo.add_event(s, {"tenant_id": t, "id": x.run}, "probe"),
    "add_usage": lambda s, t, x: repo.add_usage(s, t, x.run, input_tokens=7),
    "list_events": lambda s, t, x: repo.list_events(s, t, x.run),
    "children_of": lambda s, t, x: repo.children_of(s, t, x.run),
    "lock_run": lambda s, t, x: repo.lock_run(s, t, x.run),
    "active_children": lambda s, t, x: repo.active_children(s, t, x.run),
    "get_artifact": lambda s, t, x: repo.get_artifact(s, t, x.artifact),
    "scrub_run_content": lambda s, t, x: repo.scrub_run_content(s, t, x.child_done),
    "unscrubbed_counts": lambda s, t, x: repo.unscrubbed_counts(s, t, x.child_done),
    "ended_runs_of_root": lambda s, t, x: tuple(repo.ended_runs_of_root(s, t, x.run)),
    "next_seq": lambda s, t, x: repo.next_seq(s, t, x.run),
    "update_step": lambda s, t, x: repo.update_step(s, t, x.step, status="failed"),
    "list_steps": lambda s, t, x: repo.list_steps(s, t, x.run),
    "get_invocation": lambda s, t, x: repo.get_invocation(s, t, x.inv_key),
    "get_invocation_by_id": lambda s, t, x: repo.get_invocation_by_id(s, t, x.invocation),
    "update_invocation": lambda s, t, x: repo.update_invocation(s, t, x.invocation, "failed"),
    "get_approval": lambda s, t, x: repo.get_approval(s, t, x.approval),
    "get_approval_lock": lambda s, t, x: repo.get_approval(s, t, x.approval, lock=True),
    "find_approval": lambda s, t, x: repo.find_approval(s, t, x.run, "c0", "tool_side_effect", 0),
    "list_approvals_run": lambda s, t, x: repo.list_approvals(
        s, t, statuses=["pending"], run_id=x.run, after=None, limit=50
    ),
    "list_approvals_tree": lambda s, t, x: repo.list_approvals(
        s, t, statuses=["pending"], run_id=None, after=None, limit=50, tree_run_id=x.run
    ),
    "decide_approval": lambda s, t, x: repo.decide_approval(
        s, t, x.approval, "approved", "approve", uuid7(), None
    ),
    "step_id_of": lambda s, t, x: repo.step_id_of(s, t, x.run, 1),
    "pending_approvals": lambda s, t, x: repo.pending_approvals(s, t, x.run),
    "unknown_outcome_runs": lambda s, t, x: repo.unknown_outcome_runs(s, t, [x.run]),
    "settle_ended_run": lambda s, t, x: repo.settle_ended_run(s, t, x.child_done),
    "land_verdicts": lambda s, t, x: repo.land_verdicts(s, t, x.run),
    "verdict_summary": lambda s, t, x: repo.verdict_summary(s, t, x.run),
    "open_decisions_text": lambda s, t, x: repo.open_decisions_text(s, t, x.run),
    "set_ended_run_message": lambda s, t, x: repo.set_ended_run_message(
        s, t, x.child_done, "reworded"
    ),
    "enqueue": lambda s, t, x: repo.enqueue(s, t, x.run),
    "heartbeat_job": lambda s, t, x: repo.heartbeat_job(s, t, x.job, WORKER, 60),
    "finish_job": lambda s, t, x: repo.finish_job(s, t, x.job, WORKER),
    "release_job": lambda s, t, x: repo.release_job(s, t, x.job, WORKER),
    "finish_run_jobs": lambda s, t, x: repo.finish_run_jobs(s, t, x.run),
    "root_pipeline_id": lambda s, t, x: repo.root_pipeline_id(s, t, x.pipeline_run),
    "tree_classifications": lambda s, t, x: tuple(repo.tree_classifications(s, t, x.run)),
    "run_cost": lambda s, t, x: repo.run_cost(s, t, x.run, "USD"),
    "family_cost": lambda s, t, x: repo.family_cost(s, t, x.run, "USD"),
    # an unknown principal is displayed as its id: the answer is "fell back to the id"
    "principal_display": lambda s, t, x: (
        repo.principal_display(s, t, "service_account", x.service_account) == str(x.service_account)
    ),
    # a person of another client only is displayed as the id, like an unknown one (review 2 F1)
    "principal_display_user": lambda s, t, x: (
        repo.principal_display(s, t, "user", x.user) == str(x.user)
    ),
    "pending_approval_refs": lambda s, t, x: repo.pending_approval_refs(s, t, x.run),
    "pending_unknown_refs": lambda s, t, x: repo.pending_unknown_refs(s, t, x.run),
    "request_cancel": lambda s, t, x: repo.request_cancel(s, t, x.run, uuid7()),
    "get_artifact_row": lambda s, t, x: repo.get_artifact_row(s, t, x.artifact),
    "finished_awaited_children": lambda s, t, x: repo.finished_awaited_children(s, t, x.run),
    "job_of_run": lambda s, t, x: repo.job_of_run(s, t, x.run, x.job),
    "last_status_change": lambda s, t, x: repo.last_status_change(s, t, x.run),
    "scope_statuses": lambda s, t, x: repo.scope_statuses(s, t, x.workspace, x.project),
}

_ROWS_OF = " UNION ALL ".join(
    f"SELECT row_to_json(x)::text AS j FROM shifde.{table} x WHERE tenant_id = :t"
    for table in TABLES
)


def _rows_of(s: Session, tenant: UUID) -> list[str]:
    """Every runtime row of `tenant` this session can see, as text."""
    return [
        r[0] for r in s.execute(text(f"SELECT j FROM ({_ROWS_OF}) q ORDER BY j"), {"t": tenant})
    ]


def _answer(s: Session, case: Case, caller: UUID, target: Ids, watched: UUID) -> tuple[Any, bool]:
    """(what the call returned or raised, whether `watched`'s rows changed); rolled back."""
    savepoint = s.begin_nested()
    before = _rows_of(s, watched)
    try:
        result: Any = case(s, caller, target)  # Any: the function's return value
        if isinstance(result, list):
            result = [dict(r) for r in result]
        elif isinstance(result, dict):
            result = {k: dict(v) for k, v in result.items()}
        elif result is not None and hasattr(result, "keys"):
            result = dict(result)
        changed = _rows_of(s, watched) != before
    except DBAPIError as exc:
        savepoint.rollback()
        return (type(exc.orig).__name__, getattr(exc.orig, "sqlstate", None)), False
    except NoResultFound:
        savepoint.rollback()
        return "NoResultFound", False
    savepoint.rollback()
    return result, changed


@pytest.mark.parametrize("mode", ["app-role", "schema-owner"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_a_naming_bs_id_gets_the_unknown_id_answer(
    app_db: Database, owner_db: Database, seeded: tuple[Ids, Ids], name: str, mode: str
) -> None:
    """A's client id with B's ids answers and changes exactly what fresh ids do; with A's own ids
    the same call answers or changes something else (the case is not vacuous). As the schema
    owner nothing but the function's `tenant_id` filter stands between A's call and B's rows."""
    a, b = seeded
    case = CASES[name]
    session = app_session(app_db, a.tenant) if mode == "app-role" else owner_db.session()
    with session as s:
        unknown = _answer(s, case, a.tenant, _unknown(b), watched=b.tenant)
        assert _answer(s, case, a.tenant, b, watched=b.tenant) == unknown
        assert _answer(s, case, a.tenant, a, watched=a.tenant) != unknown
        s.rollback()


def test_every_id_taking_function_has_a_case() -> None:
    """Each public repository function that reads or writes a client table is in `CASES` (or is
    an insert, covered below, or a definer/global-table function)."""
    inserts = {"insert_run", "create_run", "insert_step", "insert_invocation", "insert_approval",
               "insert_artifact", "insert_usage_event"}  # fmt: skip
    cross_client = {"claim_jobs", "reap", "mark_reap", "upsert_worker", "beat_worker"}
    helpers = {"j", "sha256_hex", "text", "uuid7"}
    public = {
        n for n, v in vars(repo).items()
        if callable(v) and not n.startswith("_") and getattr(v, "__module__", "") == repo.__name__
    }  # fmt: skip
    covered = {
        n.removesuffix("_lock").removesuffix("_outcome_unknown").removesuffix("_user")
        for n in CASES
    }
    covered |= {"list_approvals"}
    assert public - covered - inserts - cross_client - helpers == set()


# ------------------------------------------------------------------ inserts
def _run_values(x: Ids, **over: Any) -> dict[str, Any]:  # Any: column values
    return {"tenant_id": x.tenant, "workspace_id": x.workspace, "kind": "agent",
            "initiated_by_principal_id": uuid7(), "principal_type": "user",
            "agent_version_id": None, "deadline_at": utcnow() + timedelta(hours=1),
            "trace_id": uuid7().hex, "correlation_id": "c", **over}  # fmt: skip


@pytest.mark.parametrize(
    "fn", [repo.insert_run, repo.insert_step, repo.insert_invocation, repo.insert_approval]
)
def test_an_insert_never_takes_the_callers_id(app_db: Database, fn: Any) -> None:
    """The runtime tables are keyed on `id` alone (F5): a caller-chosen id is refused before any
    SQL, so it cannot probe another client's row by collision or lock wait (B-1905)."""
    with app_session(app_db, None) as s, pytest.raises(ValueError, match="generated here"):
        fn(s, {"id": uuid7(), "tenant_id": uuid7()})


def test_create_run_chooses_its_id_and_is_its_own_root(
    app_db: Database, owner_db: Database, seeded: tuple[Ids, Ids]
) -> None:
    a, _ = seeded
    with owner_db.session() as o:
        project, agent, agent_version = o.execute(
            text("SELECT project_id, agent_id, agent_version_id FROM shifde.runs WHERE id = :r"),
            {"r": a.run},
        ).one()
    values = _run_values(a, project_id=project, agent_id=agent, agent_version_id=agent_version)
    with app_session(app_db, a.tenant) as s:
        row = repo.create_run(s, values, {"q": 1})
        assert row["tenant_id"] == a.tenant and row["root_run_id"] == row["id"]
        assert repo.get_artifact(s, a.tenant, row["input_ref"]) == {"q": 1}
        assert [e["type"] for e in repo.list_events(s, a.tenant, row["id"])] == ["state_change"]
        child = repo.insert_run(s, {**values, "parent_run_id": row["id"],
                                    "root_run_id": row["id"], "depth": 1})  # fmt: skip
        assert child["id"] != row["id"] and child["root_run_id"] == row["id"]
        s.rollback()


def _insert_cases(
    caller: UUID, x: Ids, project: UUID, agent: UUID, version: UUID, tool_version: UUID
) -> dict[str, Callable[[Session], Any]]:
    """Inserts of `caller`'s rows that name `x`'s run."""
    on_run = {"tenant_id": caller, "run_id": x.run}
    return {
        "insert_run_parent": lambda s: repo.insert_run(
            s,
            {
                **_run_values(x, project_id=project, agent_id=agent, agent_version_id=version),
                "tenant_id": caller,
                "parent_run_id": x.run,
                "root_run_id": x.run,
                "depth": 1,
            },
        ),
        "insert_step": lambda s: repo.insert_step(
            s, {**on_run, "seq": 9, "kind": "model_call", "name": "m"}
        ),
        "insert_invocation": lambda s: repo.insert_invocation(
            s,
            {
                **on_run,
                "call_ref": "cx",
                "epoch": 0,
                "tool_version_id": tool_version,
                "side_effect_class": "read",
                "idempotency_key": f"kx-{uuid7()}",
                "request_hash": "h",
                "arguments": None,
            },
        ),
        "insert_approval": lambda s: repo.insert_approval(
            s,
            {
                **on_run,
                "workspace_id": x.workspace,
                "project_id": project,
                "call_ref": "cx",
                "epoch": 0,
                "reason": "tool_side_effect",
                "requested_by": uuid7(),
                "expires_at": utcnow() + timedelta(hours=1),
            },
        ),
        "insert_artifact": lambda s: repo.insert_artifact(s, caller, x.run, "output", {}),
    }


@pytest.mark.parametrize(
    "name",
    ["insert_run_parent", "insert_step", "insert_invocation", "insert_approval", "insert_artifact"],
)
def test_an_insert_naming_bs_run_gets_the_unknown_id_answer(
    app_db: Database, owner_db: Database, seeded: tuple[Ids, Ids], name: str
) -> None:
    """Protected by the composite foreign keys `(tenant_id, run_id) -> runs (tenant_id, id)`
    (B-2101), not by a filter: B's run and an unknown run both give 23503; A's own run works."""
    a, b = seeded
    with owner_db.session() as o:
        project, agent, version, tool_version = o.execute(
            text(
                "SELECT r.project_id, r.agent_id, r.agent_version_id, i.tool_version_id "
                "FROM shifde.runs r "
                "JOIN shifde.tool_invocations i ON i.id = :i WHERE r.id = :r"
            ),
            {"r": a.run, "i": a.invocation},
        ).one()

    def answer(target: Ids) -> str | None:
        # A's own workspace and project; only the run is the target's
        x = replace(target, workspace=a.workspace)
        call = _insert_cases(a.tenant, x, project, agent, version, tool_version)[name]
        with app_session(app_db, a.tenant) as s:
            try:
                call(s)
            except DBAPIError as exc:
                return getattr(exc.orig, "sqlstate", None)
            finally:
                s.rollback()
            return "inserted"

    assert answer(b) == answer(_unknown(b)) == "23503"
    assert answer(a) == "inserted"


# ------------------------------------------------------------------ conflict targets
def test_enqueue_dedupes_on_the_clients_own_key(app_db: Database, seeded: tuple[Ids, Ids]) -> None:
    a, _ = seeded
    with app_session(app_db, a.tenant) as s:
        assert repo.enqueue(s, a.tenant, a.run) is True
        assert repo.enqueue(s, a.tenant, a.run) is False  # same state of the run: one job
        s.rollback()


def test_the_reaper_wakes_a_parent_whose_queued_job_already_exists(
    app_db: Database, owner_db: Database, seeded: tuple[Ids, Ids]
) -> None:
    """Reaper branch 4 (children finished) with the wake-up job already queued: the named
    conflict target `(tenant_id, kind, dedupe_key) WHERE status IN ('queued','leased')` absorbs
    the duplicate and the parent is woken once."""
    a, b = seeded
    with owner_db.session() as o:
        for x in (a, b):
            o.execute(
                text(
                    "UPDATE shifde.runs SET status = 'cancelled', state_version = "
                    "state_version + 1, finished_at = now() WHERE id = :c"
                ),
                {"c": x.child_pending},
            )
            sv = o.execute(
                text(
                    "UPDATE shifde.runs SET status = 'waiting_for_approval', waiting_reason = "
                    "'child_runs', state_version = state_version + 1 WHERE id = :r "
                    "RETURNING state_version"
                ),
                {"r": x.run},
            ).scalar_one()
            insert(o, "jobs", id=uuid7(), tenant_id=x.tenant, kind="run.advance", run_id=x.run,
                   dedupe_key=f"run:{x.run}:{sv + 1}")  # fmt: skip
        o.commit()
    with app_session(app_db, None) as s:
        woken = {(r["tenant_id"], r["run_id"]) for r in repo.reap(s)
                 if r["reason"] == "children_finished"}  # fmt: skip
        s.commit()
    assert woken == {(a.tenant, a.run), (b.tenant, b.run)}
    with owner_db.session() as o:
        for x in (a, b):
            status, jobs = o.execute(
                text(
                    "SELECT r.status, (SELECT count(*) FROM shifde.jobs j WHERE j.run_id = r.id "
                    "AND j.tenant_id = r.tenant_id AND j.status = 'queued' "
                    "AND j.dedupe_key = 'run:' || r.id::text || ':' || r.state_version::text) "
                    "FROM shifde.runs r WHERE r.id = :r"
                ),
                {"r": x.run},
            ).one()
            assert (status, jobs) == ("pending", 1)
