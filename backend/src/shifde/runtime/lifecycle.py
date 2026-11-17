"""One job = one *advance* of a run: claim the run (pending -> running with a lease token), execute
until the run completes, fails, waits, is cancelled or times out, and persist that outcome.

Locking discipline (no deadlocks with the reaper, which locks job -> run): a run row lock is never
held while a job row is touched; job bookkeeping happens in a separate transaction AFTER the run
transaction committed. If the worker dies in between, the job lease expires and the next claimer
finds the run already settled.

Client isolation (RT-10, B-2240..B-2246): the worker calls `advance` with the `(tenant_id,
run_id, job_id)` the claim definer returned (B-2103). Every repository call names that client
(B-2115); the job must be a `run.advance` job of that very run of that client, otherwise the
advance touches nothing and answers `skipped` (another client's job, another run's job and an
unknown id answer alike), and `runs.lease_job_id` is set only to that job while this worker holds
its lease (review 2 F2, B-2124). The run handed to the executors (`RunContext.run`) is the claimed
row re-read with the client id (review 7 item 1). The database of the advance, which is also the
`RunContext.db` of durable usage rows (Item 11), must be the application role: a privileged role
is refused before anything runs (review 5 item 1, B-2164).
"""

from __future__ import annotations

import threading
import weakref
from typing import Any  # Any: output documents
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ExecutionDenied
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import (
    RequestContext,
    bind_log_context,
    init_log_context,
    reset_log_context,
)
from shifde.core.db import Database
from shifde.core.dbrole import check_app_role
from shifde.core.hooks import run_close_hooks
from shifde.core.ids import uuid7
from shifde.core.redaction import scrub, scrub_text
from shifde.core.settings import Settings
from shifde.observability import tracing
from shifde.runtime import repository as repo
from shifde.runtime import state
from shifde.runtime.action import OntologyActionRunner
from shifde.runtime.agent import AgentRunner
from shifde.runtime.context import RunContext, RunIdentity, tenant_header
from shifde.runtime.governance import GovernanceHooks
from shifde.runtime.graph import GraphRunner
from shifde.runtime.pipeline import PipelineRunner
from shifde.runtime.settle import settle_terminal

log = structlog.get_logger("shifde.runtime")

AUDIT_ACTION = {
    state.COMPLETED: "run.completed",
    state.FAILED: "run.failed",
    state.CANCELLED: "run.cancelled",
    state.TIMED_OUT: "run.timed_out",
    state.WAITING: "run.waiting",
    state.PENDING: "run.released",
}

# databases whose role passed `check_app_role` (checked once per `Database` object)
_CHECKED: weakref.WeakSet[Database] = weakref.WeakSet()
_CHECK_LOCK = threading.Lock()


def require_app_role(database: Database) -> None:
    """Refuse (`UnsafeDatabaseRole`) a database whose role could bypass row-level security: the
    worker's database runs the advance and is the `RunContext.db` of durable usage rows, so it
    must be the application role, like the API's (review 5 item 1, B-2164)."""
    with _CHECK_LOCK:
        if database in _CHECKED:
            return
    check_app_role(database.engine)
    with _CHECK_LOCK:
        _CHECKED.add(database)


def finalize(
    ctx: RunContext,
    to_status: str,
    *,
    error_class: str | None = None,
    message: str | None = None,
    output: Any = None,  # Any: output document
    waiting_reason: str | None = None,
) -> None:
    """Persist the outcome of an advance (fenced by the lease token)."""
    tenant_id = ctx.tenant_id
    with ctx.txn():
        if to_status == state.WAITING:
            # B-2452: a cancel request that landed after the run's last checkpoint (the API, or
            # `cancel_open_children` of a fail_fast pipeline, which flags a running child and
            # finishes its jobs) ends the run cancelled instead of parking it with nobody left
            # to cancel it. `ctx.txn()` holds the run row lock, so a canceller either committed
            # before this read or finds the run ended (this client's own run row).
            ctl = repo.control_state(ctx.session, tenant_id, ctx.run_id)
            if ctl is not None and ctl["cancel_requested_at"] is not None:
                to_status, waiting_reason = state.CANCELLED, None
                error_class, message = state.CANCELLED_CLASS, "cancelled"
        terminal = to_status in state.TERMINAL
        values: dict[str, Any] = {}  # Any: run columns
        if terminal:
            values["finished_at"] = utcnow()
        if error_class:
            values["error_class"] = error_class
        if message:
            values["error_message"] = scrub_text(message)[:1000]
        if output is not None:
            # the artifact just inserted for this run of this client (B-2124)
            values["output_ref"] = repo.insert_artifact(
                ctx.session, tenant_id, ctx.run_id, "output", scrub({"output": output})
            )
        if to_status == state.WAITING:
            values["waiting_reason"] = waiting_reason
        row = repo.transition(
            ctx.session,
            tenant_id,
            ctx.run_id,
            state.RUNNING,
            to_status,
            token=ctx.token,
            clear_lease=True,
            **values,
        )
        if row is None:
            raise state.LeaseLost()
        ctx.run = row
        if terminal:
            settle_terminal(ctx.session, row, to_status)
        if (
            to_status == state.WAITING
            and waiting_reason in ("approval", "indeterminate_side_effect")
            and repo.pending_approvals(ctx.session, tenant_id, ctx.run_id) == 0
        ):
            # the approval was decided before this worker parked the run: do not park it
            back = repo.transition(
                ctx.session, tenant_id, ctx.run_id, state.WAITING, state.PENDING,
                waiting_reason=None,
            )  # fmt: skip
            if back is not None:
                repo.enqueue(ctx.session, tenant_id, ctx.run_id)
        if (
            to_status == state.WAITING
            and waiting_reason in ("child_runs", "approval", "indeterminate_side_effect")
            and row["kind"] == "pipeline"
            and repo.finished_awaited_children(ctx.session, tenant_id, ctx.run_id) > 0
        ):
            # a child finished between this advance reading it and parking the run: its wake-up
            # saw the run RUNNING and did nothing, so do not park (lost wake-up)
            back = repo.transition(
                ctx.session, tenant_id, ctx.run_id, state.WAITING, state.PENDING,
                waiting_reason=None,
            )  # fmt: skip
            if back is not None:
                repo.enqueue(ctx.session, tenant_id, ctx.run_id)
        ctx.audit(
            AUDIT_ACTION[to_status],
            {
                "error_class": error_class,
                "waiting_reason": waiting_reason,
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
            },
            outcome="success" if to_status in (state.COMPLETED, state.WAITING) else "failure",
            commit=False,
        )


def _claim(
    session: Session,
    tenant_id: UUID,
    run: Any,  # Any: run row
    token: UUID,
    job_id: UUID,
    worker_id: str,
    recovered: bool,
) -> Any:  # Any: run row
    """pending -> running under a new lease token. `lease_job_id` is set only to `job_id` while it
    is a job of this very run of this client leased by this worker (B-2124); otherwise nothing is
    claimed. A `recovered` event is written only when the run comes back from a lost or handed
    back lease (E2E m3: not on the ordinary re-advance after a wait)."""
    job = repo.job_of_run(session, tenant_id, run["id"], job_id)
    if job is None or job["status"] != "leased" or job["lease_owner"] != worker_id:
        return None
    row = repo.transition(
        session,
        tenant_id,
        run["id"],
        state.PENDING,
        state.RUNNING,
        lease_token=token,
        lease_job_id=job_id,
        started_at=run["started_at"] or utcnow(),
        attempt_count=int(run["attempt_count"]) + 1,
    )
    if row is not None and recovered:
        repo.add_event(session, row, "recovered", details={"attempt": int(row["attempt_count"])})
    return row


def _came_back_from_a_lease(session: Session, tenant_id: UUID, run_id: UUID) -> bool:
    last = repo.last_status_change(session, tenant_id, run_id)
    return (
        last is not None
        and last["from_status"] == state.RUNNING
        and last["to_status"] == state.PENDING
    )


def _fail_before_start(session: Session, run: Any, error_class: str, message: str) -> None:
    """Terminal failure of a run this worker holds (no RunContext could be built)."""
    row = repo.transition(
        session,
        run["tenant_id"],
        run["id"],
        state.RUNNING,
        state.FAILED,
        token=run["lease_token"],
        clear_lease=True,
        finished_at=utcnow(),
        error_class=error_class,
        error_message=message,
    )
    if row is not None:
        settle_terminal(session, row, state.FAILED)
        audit.record(
            session,
            RequestContext(request_id=run["trace_id"]),
            action="run.failed",
            outcome="denied" if error_class == state.AUTHZ_DENIED else "failure",
            tenant_id=run["tenant_id"],
            actor_id=run["initiated_by_principal_id"],
            target_type="run",
            target_id=run["id"],
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            details={"run_id": str(run["id"]), "error_class": error_class, "message": message},
        )
    session.commit()


def _parent_pipeline_ended(session: Session, tenant_id: UUID, run: Any) -> bool:  # Any: run row
    parent = repo.get_run(session, tenant_id, run["parent_run_id"])
    return (
        parent is not None
        and parent["kind"] == "pipeline"
        and parent["status"] in (state.FAILED, state.CANCELLED, state.TIMED_OUT)
    )


def _trace_of(
    session: Session, tenant_id: UUID, run_id: UUID, job_id: UUID
) -> tuple[str | None, str | None, str | None]:
    """(trace id, parent span id, correlation id) of the run's trace: the run row fixes the trace
    id (set from the API request); the job's `traceparent` names the span that queued it. Both
    read with the client id; the job only when it is a job of this run (B-2240)."""
    db.set_tenant(session, tenant_id)
    run = repo.get_run(session, tenant_id, run_id)
    job = repo.job_of_run(session, tenant_id, run_id, job_id)
    session.commit()
    parent = tracing.parse_traceparent(job["traceparent"] if job else None)
    trace_id = run["trace_id"] if run else None
    if trace_id is not None and tracing.parse_traceparent(f"00-{trace_id}-{'1' * 16}-01") is None:
        trace_id = None
    return (
        trace_id,
        parent[1] if parent and parent[0] == trace_id else None,
        (run["correlation_id"] if run else None),
    )


def advance(
    database: Database,
    settings: Settings,
    runtime: IntegrationRuntime,
    hooks: GovernanceHooks,
    tenant_id: UUID,
    run_id: UUID,
    worker_id: str,
    stop: threading.Event,
    lease_lost: threading.Event,
    job_id: UUID,
) -> str:
    """Advance one run inside a span of the run's trace (the API request that created the run
    started that trace). Returns what happened (`done`, `released`, `lease_lost`, `skipped`).
    `database` must be the application role (refused otherwise, before any statement)."""
    require_app_role(database)
    session = database.session()
    db.init_context(session, f"advance-{run_id}")
    try:
        return _advance_in(
            session, database, settings, runtime, hooks, tenant_id, run_id, worker_id, stop,
            lease_lost, job_id,
        )  # fmt: skip
    finally:
        try:
            run_close_hooks(session)
        finally:
            session.close()


def _advance_in(
    session: Session,
    usage_db: Database | None,
    settings: Settings,
    runtime: IntegrationRuntime,
    hooks: GovernanceHooks,
    tenant_id: UUID,
    run_id: UUID,
    worker_id: str,
    stop: threading.Event,
    lease_lost: threading.Event,
    job_id: UUID,
) -> str:
    """`advance` on a session the caller owns (private: it does not check the role behind
    `session`; the worker calls `advance`, runtime isolation review 9 finding 3). `usage_db`
    becomes `RunContext.db` (durable usage rows, Item 11) and is refused unless it is the
    application role; None writes usage rows in the step's transaction."""
    if usage_db is not None:
        require_app_role(usage_db)
    trace_id, parent_span, correlation = _trace_of(session, tenant_id, run_id, job_id)
    log_token = init_log_context(correlation or f"advance-{run_id}")
    bind_log_context(tenant_id=str(tenant_id), run_id=str(run_id), correlation_id=correlation)
    try:
        with tracing.start_span(
            "run.advance",
            {"run_id": str(run_id), "tenant_id": str(tenant_id), "worker_id": worker_id},
            trace_id=trace_id or tracing.new_trace_id(),
            parent_span_id=parent_span,
            bind_logs=True,
        ):
            outcome = _advance(
                session, usage_db, settings, runtime, hooks, tenant_id, run_id, worker_id, stop,
                lease_lost, job_id,
            )  # fmt: skip
            # carries trace_id, span_id, tenant_id, run_id and correlation_id from the log context
            log.info("run.advance_finished", outcome=outcome, worker_id=worker_id)
            return outcome
    finally:
        reset_log_context(log_token)


def _advance(
    session: Session,
    usage_db: Database | None,
    settings: Settings,
    runtime: IntegrationRuntime,
    hooks: GovernanceHooks,
    tenant_id: UUID,
    run_id: UUID,
    worker_id: str,
    stop: threading.Event,
    lease_lost: threading.Event,
    job_id: UUID,
) -> str:
    db.set_tenant(session, tenant_id)
    job = repo.job_of_run(session, tenant_id, run_id, job_id)
    if job is None:
        # not a job of this run of this client (another client's job, another run's, unknown):
        # it drives nothing
        session.commit()
        return "skipped"
    run = repo.get_run(session, tenant_id, run_id)
    if run is None or run["status"] in state.TERMINAL or run["status"] == state.WAITING:
        session.commit()
        return "skipped"
    recovered = False
    if run["status"] == state.RUNNING:
        if run["lease_job_id"] not in (None, job_id):
            # the run is executed under ANOTHER job's lease (this job is stale): hands off;
            # the reaper deals with that lease if its owner dies
            session.commit()
            return "skipped"
        if job["status"] != "leased" or job["lease_owner"] != worker_id:
            # recovery resets the run only for the job this worker holds (runtime isolation
            # review 9 finding 1): a done, queued or foreign-leased job of the run recovers nothing
            session.commit()
            return "skipped"
        # the lease belongs to this very job (or to none): recover the abandoned run
        repo.transition(session, tenant_id, run_id, state.RUNNING, state.PENDING, clear_lease=True)
        session.commit()
        run = repo.get_run(session, tenant_id, run_id)
        assert run is not None
        recovered = True
    if run["parent_run_id"] is not None and _parent_pipeline_ended(session, tenant_id, run):
        # the pipeline failed / was cancelled but its cancel sweep never reached this child
        # (the API died between the decision and the sweep): finish it here
        row = repo.transition(
            session,
            tenant_id,
            run_id,
            state.PENDING,
            state.CANCELLED,
            clear_lease=True,
            finished_at=utcnow(),
            error_class=state.CANCELLED_CLASS,
            error_message="its pipeline ended",
        )
        if row is not None:
            settle_terminal(session, row, state.CANCELLED)
            audit.record(
                session,
                RequestContext(request_id=f"advance-{run_id}"),
                action=AUDIT_ACTION[state.CANCELLED],
                outcome="success",
                tenant_id=tenant_id,
                actor_id=None,
                target_type="run",
                target_id=run_id,
                target_label=f"run:{run_id}",
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                details={
                    "run_id": str(run_id),
                    "kind": row["kind"],
                    "reason": "parent_ended",
                    "by": "worker",
                },
            )
        session.commit()
        return "done"
    if utcnow() >= run["deadline_at"]:
        # also the deadline a child inherited from its parent (RT-8/RT-9 copy it at creation)
        row = repo.transition(
            session,
            tenant_id,
            run_id,
            state.PENDING,
            state.TIMED_OUT,
            finished_at=utcnow(),
            error_class=state.TIMEOUT,
            error_message="the run deadline passed before it started",
        )
        if row is not None:
            settle_terminal(session, row, state.TIMED_OUT)
            # audited in the same transaction as the transition (no unaudited terminal state)
            audit.record(
                session,
                RequestContext(request_id=f"advance-{run_id}"),
                action=AUDIT_ACTION[state.TIMED_OUT],
                outcome="failure",
                tenant_id=tenant_id,
                actor_id=None,
                target_type="run",
                target_id=run_id,
                target_label=f"run:{run_id}",
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                details={
                    "run_id": str(run_id),
                    "kind": row["kind"],
                    "error_class": state.TIMEOUT,
                    "reason": "deadline_before_start",
                    "by": "worker",
                },
            )
        session.commit()
        return "done"
    token = uuid7()
    recovered = recovered or _came_back_from_a_lease(session, tenant_id, run_id)
    claimed = _claim(session, tenant_id, run, token, job_id, worker_id, recovered)
    session.commit()
    if claimed is None:
        return "skipped"
    # the executors get the claimed row re-read with the client id (review 7 item 1)
    held = repo.get_run(session, tenant_id, run_id)
    session.commit()
    if held is None or held["status"] != state.RUNNING or held["lease_token"] != token:
        return "lease_lost"
    try:
        identity = RunIdentity(
            tenant_id,
            held["initiated_by_principal_id"],
            held["principal_type"],
            held["api_key_id"],
        )
        principal = authz.execution_principal(session, identity)
        header = tenant_header(session, tenant_id)
    except ExecutionDenied as exc:
        _fail_before_start(session, held, state.AUTHZ_DENIED, str(exc))
        return "done"
    except state.RunFailed as exc:  # the client is not available: fail it, never leave it held
        session.rollback()
        _fail_before_start(session, held, exc.error_class, exc.message)
        return "done"
    ctx = RunContext(
        session=session,
        settings=settings,
        runtime=runtime,
        hooks=hooks,
        run=held,
        token=token,
        worker_id=worker_id,
        stop=stop,
        lease_lost=lease_lost,
        principal=principal,
        tenant_header=header,
        db=usage_db,
    )
    return _run(ctx)


def _run(ctx: RunContext) -> str:
    try:
        return _run_inner(ctx)
    except state.LeaseLost:  # also from the outcome handlers themselves
        ctx.session.rollback()
        log.warning("runtime.lease_lost", run_id=str(ctx.run_id))
        return "lease_lost"


def _run_inner(ctx: RunContext) -> str:
    try:
        ctx.audit("run.started", {"attempt": int(ctx.run["attempt_count"])})
        runner: AgentRunner | GraphRunner | PipelineRunner | OntologyActionRunner
        if ctx.run["kind"] == "agent":
            runner = AgentRunner(ctx)
        elif ctx.run["kind"] == "pipeline":
            runner = PipelineRunner(ctx)
        elif ctx.run["kind"] == "ontology_action":
            runner = OntologyActionRunner(ctx)
        else:
            runner = GraphRunner(ctx)
        output = runner.run()
        finalize(ctx, state.COMPLETED, output=output)
        return "done"
    except state.LeaseLost:
        ctx.session.rollback()
        log.warning("runtime.lease_lost", run_id=str(ctx.run_id))
        return "lease_lost"
    except state.Shutdown:
        ctx.session.rollback()
        try:
            # a cancel requested before the shutdown (the API, or `cancel_open_children` of a
            # fail_fast pipeline, which has already finished the child's jobs) ends the run
            # cancelled; handing it back would leave it `pending` (B-2440)
            ctx.control()
        except state.Cancelled:
            finalize(ctx, state.CANCELLED, error_class=state.CANCELLED_CLASS, message="cancelled")
            return "done"
        except state.TimedOut:
            pass  # the next worker times it out
        _release(ctx)
        return "released"
    except state.Waiting as exc:
        ctx.session.rollback()
        # an executor that parked the run itself (the graph's `child_runs`) is not moved again;
        # the last child's `wake_parent` wakes it
        if not exc.transitioned:
            finalize(ctx, state.WAITING, waiting_reason=exc.reason)
        return "done"
    except state.Cancelled:
        ctx.session.rollback()
        finalize(ctx, state.CANCELLED, error_class=state.CANCELLED_CLASS, message="cancelled")
        return "done"
    except state.TimedOut as exc:
        ctx.session.rollback()
        finalize(ctx, state.TIMED_OUT, error_class=state.TIMEOUT, message=exc.message)
        return "done"
    except state.RunFailed as exc:
        ctx.session.rollback()
        finalize(ctx, state.FAILED, error_class=exc.error_class, message=exc.message)
        return "done"
    except Exception as exc:  # noqa: BLE001 - a bug must fail ONE run, never the worker
        # C1 (B-2151): the failure goes through `finalize` -> `settle_terminal`, so a write that
        # was in flight is marked `indeterminate`, the run's class becomes
        # `indeterminate_side_effect` and the unknown-outcome approval is opened (never a plain
        # `failed/internal`)
        ctx.session.rollback()
        log.error("runtime.internal_error", run_id=str(ctx.run_id), exc_info=True)
        try:
            finalize(
                ctx,
                state.FAILED,
                error_class=state.INTERNAL,
                message=f"internal error ({type(exc).__name__})",
            )
        except state.LeaseLost:
            return "lease_lost"
        return "done"


def _release(ctx: RunContext) -> None:
    """Graceful shutdown: hand the run back (running -> pending) so another worker continues it."""
    with ctx.txn():
        row = repo.transition(
            ctx.session,
            ctx.tenant_id,
            ctx.run_id,
            state.RUNNING,
            state.PENDING,
            token=ctx.token,
            clear_lease=True,
        )
        if row is None:
            raise state.LeaseLost()
