"""Terminal-state housekeeping shared by the lifecycle, the cancel path and the pipeline executor
(kept free of runner imports so the executors can use it without an import cycle).

Every repository call names the client: the run row's own `tenant_id` (a row the caller read
under its client) or the `tenant_id` the caller passes (B-2115). Another client's run id answers
as an unknown one: nothing is woken, settled or cancelled."""

from __future__ import annotations

from typing import Any  # Any: run row
from uuid import UUID

from sqlalchemy.orm import Session

from shifde import ledger
from shifde.audit import service as audit
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime import state
from shifde.tenancy import service as tenancy

__all__ = ["cancel_open_children", "scrub_ended", "settle_terminal", "wake_parent"]


def wake_parent(session: Session, run: Any) -> None:  # Any: run row
    """Called in the transaction in which a child run became terminal: when it was the last open
    child of a parent waiting for its children, the parent goes back to `pending` (+ job). A
    PIPELINE parent is woken by every finished child: it schedules a downstream node as soon as
    its input is ready (and, under fail_fast, cancels the siblings of a failed child instead of
    waiting for them)."""
    parent_id = run["parent_run_id"]
    if parent_id is None:
        return
    tenant_id: UUID = run["tenant_id"]
    parent = repo.lock_run(session, tenant_id, parent_id)
    if parent is None or parent["status"] != state.WAITING:
        return
    reason = parent["waiting_reason"]
    pipeline = parent["kind"] == "pipeline"
    # a pipeline parent parked on an approval is woken too: a failed child must be able to
    # cancel its siblings (fail_fast) without waiting for a human
    wake = reason == "child_runs" and (
        pipeline or repo.active_children(session, tenant_id, parent_id) == 0
    )
    wake = wake or (pipeline and reason in ("approval", "indeterminate_side_effect"))
    if wake:
        moved = repo.transition(
            session, tenant_id, parent_id, state.WAITING, state.PENDING, waiting_reason=None
        )
        if moved is not None:
            repo.enqueue(session, tenant_id, parent_id)


def capture_full(session: Session, tenant_id: UUID, settings: Settings) -> bool:
    """Whether run content is captured for the client: its tenant-level `capture_content`;
    unset means "full" in development and "metadata" otherwise; only "full" is on (the rule of
    `RunService._capture_full`, B-2275)."""
    mode = tenancy.tenant_settings(session, tenant_id).get("capture_content")
    if mode is None:
        mode = "full" if settings.is_development else "metadata"
    return bool(mode == "full")


def scrub_ended(session: Session, run: Any, settings: Settings) -> None:  # Any: run row
    """Capture off (F-12, B-2514): called in the transaction in which `run` became terminal.
    A run's content is scrubbed once both it and its root run are terminal: parents read a
    child's output after the child ended, so a child that ends under a running root keeps its
    content until the root's own terminal transition scrubs the whole family. The run is
    re-read with its own client (another client's run id scrubs nothing)."""
    tenant_id: UUID = run["tenant_id"]
    cur = repo.get_run(session, tenant_id, run["id"])
    if cur is None or cur["status"] not in state.TERMINAL:
        return
    if capture_full(session, tenant_id, settings):
        return
    root_id: UUID = cur["root_run_id"]
    if root_id != cur["id"]:
        root = repo.get_run(session, tenant_id, root_id)
        if root is None or root["status"] not in state.TERMINAL:
            return
    for run_id in repo.ended_runs_of_root(session, tenant_id, root_id):
        repo.scrub_run_content(session, tenant_id, run_id)


def settle_terminal(
    session: Session,
    run: Any,  # Any: run row
    to_status: str,
    settings: Settings,
) -> None:
    """Housekeeping that belongs to the transition into a terminal state.

    First the run's held budget holds are settled as orphans (`ledger.reservations`): one whose
    provider call may have started becomes `settled_unknown` with its usage row, any other is
    released. The database side (`shifde.settle_ended_run`, one implementation shared with the
    reaper) cancels the pending approvals, releases the unstarted budget reservations and closes
    the `running` steps.

    For a run that did not complete it also finds the writes that were in flight: a `started` write
    invocation (any write class) or a pipeline write node becomes `indeterminate` and the run
    is flagged `error_class=indeterminate_side_effect` with its approval left open (R1: a
    cancel or a timeout must not read as a plain cancel or timeout while a write's outcome is
    unknown).

    C1 (B-2151): a run that ends (cancel, deadline, failure) while an approved write was in
    flight is not reported as a plain cancel: the database side marks the write `indeterminate`,
    sets `error_class=indeterminate_side_effect`, appends an `outcome_unknown` event and opens one
    unknown-outcome approval that cancel and deadline do not close; this side audits it. The
    settle is keyed by the run row's own client (`repo.settle_ended_run`), so a row naming
    another client's run settles nothing.

    With capture off the run's content is scrubbed after the settle, which reads the write
    arguments the scrub clears (`scrub_ended`)."""
    ledger.reservations.settle_orphaned(session, run["tenant_id"], run["id"])
    flagged = repo.settle_ended_run(session, run["tenant_id"], run["id"])
    if flagged > 0:
        audit.record(
            session,
            RequestContext(request_id=str(run["trace_id"])),
            action="run.indeterminate_side_effect",
            outcome="failure",
            tenant_id=run["tenant_id"],
            actor_id=None,
            target_type="run",
            target_id=run["id"],
            target_label=f"run:{run['id']}",
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            details={
                "run_id": str(run["id"]),
                "kind": run["kind"],
                "ended_as": to_status,
                "open_decisions": flagged,
            },
        )
    scrub_ended(session, run, settings)
    wake_parent(session, run)


def cancel_open_children(
    session: Session,
    tenant_id: UUID,
    run_id: UUID,
    by: Any,  # Any: UUID | None, the principal recorded as the canceller
    settings: Settings,
    still_leader: Any = None,  # Any: Callable[[], None] that raises when the lease is lost
) -> None:
    """Cancel the open child runs of `run_id`, each in its OWN transaction (child row first, then
    its parent inside `settle_terminal`, the order of the API cancel path). The caller must not
    hold the parent row lock. `still_leader` is the lease check of a worker. Only the children of
    `tenant_id`'s run are read (another client's run id has none)."""
    session.commit()
    for child in repo.children_of(session, tenant_id, run_id):
        if child["status"] in state.TERMINAL:
            continue
        if still_leader is not None:
            still_leader()
        cur = repo.lock_run(session, tenant_id, child["id"])
        if cur is None or cur["status"] in state.TERMINAL:
            session.rollback()
            continue
        repo.request_cancel(session, tenant_id, child["id"], by)
        if cur["status"] in (state.PENDING, state.WAITING):
            row = repo.transition(
                session,
                tenant_id,
                child["id"],
                cur["status"],
                state.CANCELLED,
                clear_lease=True,
                finished_at=utcnow(),
                error_class=state.CANCELLED_CLASS,
                error_message="cancelled with the parent run",
            )
            if row is not None:
                settle_terminal(session, row, state.CANCELLED, settings)
        session.commit()
        repo.finish_run_jobs(session, tenant_id, child["id"])
        session.commit()
