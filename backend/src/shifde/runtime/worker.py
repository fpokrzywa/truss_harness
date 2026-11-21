"""`shifde-worker`: the execution plane (SYSTEM_ARCHITECTURE 6.3).

A separate process that claims jobs from the PostgreSQL queue (`shifde_claim_jobs`, FOR UPDATE SKIP
LOCKED, ids only), runs each run on a worker thread, heartbeats the leases it holds, runs the
reaper (guarded by an advisory lock so one reaper acts at a time), and wakes on LISTEN / NOTIFY or
by polling. The API never executes model or tool calls.

Shutdown (SIGINT / SIGTERM / CTRL_BREAK): stop claiming; running runs notice at their next
checkpoint and are handed back (running -> pending, job released) so another worker continues
them; whatever does not finish within the grace period is left to lease expiry + the reaper.
Each job runs on its own daemon thread, so a thread abandoned as wedged is never waited for at
shutdown and never holds up process exit.

Client isolation (RT-12, B-2291..B-2294): the worker's database is the application role (refused
at startup otherwise, like the API: `run` exits 3, and `Database` itself refuses a privileged role,
B-2164); it is also the `RunContext.db` of durable usage rows. Claiming and reaping go through the
cross-client definers `shifde_claim_jobs` / `shifde_reap_expired_leases` on a session without a
client context (B-2103); every other statement runs in the claimed (or reaped) row's own client:
the `(job_id, tenant_id, run_id)` the claim returned is what `lifecycle.advance` gets, and every job
heartbeat, release and finish names that client. A job wedged past its run's deadline is no longer
heartbeated, so its lease expires and the reaper settles the run (ARCH M3, B-2294). The worker
abandons that job: its thread (which Python cannot kill) no longer counts against `concurrency`,
so the worker keeps claiming new work, and whatever it does when it returns late is fenced by the
run's lease token (B-2590).

Capture-off sweep (F-12, issue #34): on a tick where this worker won the reaper lock, the reaper
also sweeps every capture-off client for ended runs (run and root terminal) whose content scrub
never ran (the reaper's follow-up transaction failed, or a worker died first) and scrubs them:
per client, in a transaction of its own under that client's context and a per-client advisory
lock. A failure is logged and the sweep moves on to the next client.

Jobs are dispatched by `kind` (ARCH M2, B-2610, B-2720): `run.advance` advances its run;
`connection.test` (no run) runs `connections.service.run_test_job`, and `model.health_check` and
`model.discover` (no run) run `models_admin.service.run_provider_job`, in the job's own client as
its requester, re-derived from the database, and end `done`, or `dead` with the refusal's error
code; any other kind ends `dead` without running.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shifde import __version__, ledger
from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.connections import service as connections
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.dbrole import UnsafeDatabaseRole, check_database_url
from shifde.core.egress import default_resolver
from shifde.core.settings import Settings, settings_or_exit
from shifde.models_admin import service as models_admin
from shifde.observability import tracing
from shifde.observability.logging import configure_logging
from shifde.runtime import lifecycle
from shifde.runtime import repository as repo
from shifde.runtime.governance import GovernanceHooks, TokenBudgetHooks
from shifde.runtime.settle import capture_full, scrub_ended

log = structlog.get_logger("shifde.worker")
REAPER_LOCK = "shifde:reaper"
CAPTURE_SWEEP_LOCK = "shifde:capture-sweep:{}"  # per client
_CLIENTS = text("SELECT id FROM shifde.tenants ORDER BY id")
EXIT_UNSAFE_ROLE = 3  # as `shifde-api` (shifde.main)
ADVANCE_KIND = "run.advance"
UNKNOWN_KIND = "unknown job kind"
REASON_ACTION = {
    "lease_lost": "run.lease_lost",
    "worker_lost": "run.failed",
    "timed_out": "run.timed_out",
    "approval_expired": "approval.expired",
    "cancelled": "run.cancelled",
    "children_finished": "run.resumed",
    "unknown_outcome": "run.indeterminate_side_effect",
}


@dataclass
class _Job:
    id: UUID
    tenant_id: UUID
    run_id: UUID | None  # None: a run-less job (`connection.test`, `model.*`, ARCH M2)
    kind: str = ADVANCE_KIND
    started: datetime = field(default_factory=utcnow)  # dispatched by this worker
    error: str | None = None  # `last_error` of a job finished `dead`
    lease_lost: threading.Event = field(default_factory=threading.Event)
    # wedged past its run's deadline: no longer heartbeated, waited for or counted (ARCH M3)
    abandoned: threading.Event = field(default_factory=threading.Event)


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        worker_id: str | None = None,
        concurrency: int | None = None,
        lease_seconds: int | None = None,
        poll_ms: int | None = None,
        reap_seconds: float | None = None,
        runtime: IntegrationRuntime | None = None,
        hooks: GovernanceHooks | None = None,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        self.concurrency = concurrency or settings.worker_concurrency
        self.lease = lease_seconds or settings.worker_lease_seconds
        self.poll = (poll_ms or settings.worker_poll_ms) / 1000
        self.reap_every = reap_seconds if reap_seconds is not None else max(self.lease / 4, 1.0)
        # the application role only (`Database` refuses a privileged role, B-2164); a statement
        # (a lock wait included) is bounded, so a thread cannot block forever on the database
        # (ARCH M3)
        self.db = Database(
            settings.database_url,
            pool_size=self.concurrency + 3,
            max_overflow=4,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
        # a job still running this long after its run's deadline is wedged: stop heartbeating it
        # so the lease expires and the reaper settles the run, and abandon its thread: it is never
        # killed, no longer counts against `concurrency`, is not waited for at shutdown, and its
        # late return is fenced by the run's lease token (ARCH M3, B-2590)
        self.wedged_after = timedelta(seconds=max(2 * self.lease, 60))
        self.runtime = runtime or IntegrationRuntime(
            settings=settings, registry=AdapterRegistry.load(), resolver=default_resolver
        )
        self.hooks = hooks or TokenBudgetHooks()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._inflight: dict[UUID, _Job] = {}
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)  # a job ended or was abandoned

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> None:
        with self.db.session() as s:
            repo.upsert_worker(
                s, self.worker_id, socket.gethostname(), os.getpid(), __version__, self.concurrency
            )
            s.commit()
        log.info("worker.started", worker_id=self.worker_id, concurrency=self.concurrency)
        for target in (self._heartbeat_loop, self._listen_loop):
            threading.Thread(target=target, daemon=True, name=target.__name__).start()
        next_reap = 0.0
        try:
            while not self.stop.is_set():
                self._claim_and_dispatch()
                if time.monotonic() >= next_reap:
                    self._reap()
                    next_reap = time.monotonic() + self.reap_every
                self.wake.wait(self.poll)
                self.wake.clear()
        finally:
            self._shutdown()

    def request_stop(self) -> None:
        self.stop.set()
        self.wake.set()

    def _shutdown(self) -> None:
        log.info("worker.stopping", worker_id=self.worker_id)
        with self._idle:
            self._idle.wait_for(lambda: not self._active())
        try:
            with self.db.session() as s:
                repo.beat_worker(s, self.worker_id, "stopped")
                s.commit()
        finally:
            self.db.dispose()
        log.info("worker.stopped", worker_id=self.worker_id)

    # ------------------------------------------------------------------ claiming
    def _claim_and_dispatch(self) -> None:
        with self._lock:
            free = self.concurrency - len(self._active())
        if free <= 0:
            return
        try:
            with self.db.session() as s:
                rows = repo.claim_jobs(
                    s, self.worker_id, free, self.lease, self.settings.queue_tenant_max_inflight
                )
                s.commit()
        except Exception:  # noqa: BLE001 - the database may be briefly unavailable
            log.error("worker.claim_failed", exc_info=True)
            return
        dispatched = self._dispatch(rows)
        # Only a full batch of NEW work suggests more is queued. A handed-back duplicate claim must
        # not count, or the worker would re-claim the same job in a busy loop.
        if dispatched == free:
            self.wake.set()

    def _dispatch(self, rows: Sequence[Mapping[str, Any]]) -> int:  # Any: claim rows
        """Start each claimed job; return how many were actually started."""
        started = 0
        for row in rows:
            job = _Job(row["job_id"], row["tenant_id"], row["run_id"], row["kind"])
            with self._lock:
                busy = job.id in self._inflight
                if not busy:
                    self._inflight[job.id] = job
            if busy:
                # The reaper requeued a job this worker is still executing (its heartbeat could not
                # reach the database) and we claimed it again. Never run one job twice here and
                # never replace the live entry: hand the claim back untouched.
                log.warning("worker.reclaimed_inflight_job", job_id=str(job.id))
                self._release_claim(job)
                continue
            threading.Thread(
                target=self._work, args=(job,), daemon=True, name=f"shifde-run-{job.id}"
            ).start()
            started += 1
        return started

    def _release_claim(self, job: _Job) -> None:
        try:
            with self.db.session() as s:
                db.set_tenant(s, job.tenant_id)
                # Back off one lease period so another worker (or this one, once the stale call
                # returns) gets the job instead of an immediate re-claim here.
                repo.release_job(
                    s, job.tenant_id, job.id, self.worker_id, delay_seconds=float(self.lease)
                )
                s.commit()
        except Exception:  # noqa: BLE001 - the lease expires and the reaper requeues it
            log.error("worker.release_failed", job_id=str(job.id), exc_info=True)

    def _work(self, job: _Job) -> None:
        outcome = "error"
        try:
            outcome = self._execute(job)
        except Exception:  # noqa: BLE001 - leave the lease to expire; the reaper requeues it
            log.error("worker.job_failed", job_id=str(job.id), exc_info=True)
        finally:
            self._settle_job(job, outcome)
            with self._lock:
                if self._inflight.get(job.id) is job:  # never drop a newer entry for this id
                    del self._inflight[job.id]
                self._idle.notify_all()
            self.wake.set()

    def _execute(self, job: _Job) -> str:
        """Run the job by its kind; an unknown kind is never run (it ends `dead`)."""
        if job.kind == ADVANCE_KIND and job.run_id is not None:
            return lifecycle.advance(
                self.db,
                self.settings,
                self.runtime,
                self.hooks,
                job.tenant_id,
                job.run_id,
                self.worker_id,
                self.stop,
                job.lease_lost,
                job.id,
            )
        if job.kind == connections.TEST_JOB_KIND:
            # in the job's own client, as its requester re-derived from the database (B-2610)
            job.error = connections.run_test_job(self.db, self.runtime, job.tenant_id, job.id)
        elif job.kind in (models_admin.HEALTH_JOB_KIND, models_admin.DISCOVER_JOB_KIND):
            # the same, for a provider health check or model discovery (B-2720)
            job.error = models_admin.run_provider_job(self.db, self.runtime, job.tenant_id, job.id)
        else:
            job.error = UNKNOWN_KIND
        return "done" if job.error is None else "dead"

    def _settle_job(self, job: _Job, outcome: str) -> None:
        try:
            with self.db.session() as s:
                db.set_tenant(s, job.tenant_id)
                if outcome in ("lease_lost", "error"):
                    if job.run_id is None:
                        return  # a run-less job: the lease expires and the reaper closes it
                    # Finish our own job only when the run no longer depends on it (settled, or
                    # executed under another job's lease). A pending / running-under-us run keeps
                    # the lease: it expires and the reaper requeues the job.
                    cur = repo.run_lease(s, job.tenant_id, job.run_id)
                    settled = cur is None or cur["status"] in (
                        "completed",
                        "failed",
                        "cancelled",
                        "timed_out",
                        "waiting_for_approval",
                    )
                    foreign = (
                        cur is not None
                        and cur["status"] == "running"
                        and cur["lease_job_id"] not in (None, job.id)
                    )
                    if settled or foreign:
                        repo.finish_job(s, job.tenant_id, job.id, self.worker_id)
                        s.commit()
                    return
                if outcome == "released":
                    repo.release_job(s, job.tenant_id, job.id, self.worker_id)
                elif outcome == "dead":
                    repo.finish_job(
                        s, job.tenant_id, job.id, self.worker_id, "dead", last_error=job.error
                    )
                else:
                    repo.finish_job(s, job.tenant_id, job.id, self.worker_id)
                s.commit()
        except Exception:  # noqa: BLE001 - the lease expires and the run is already settled
            log.error("worker.job_settle_failed", job_id=str(job.id), exc_info=True)

    def _active(self) -> list[_Job]:
        """The in-flight jobs not abandoned as wedged (call with `self._lock` held)."""
        return [job for job in self._inflight.values() if not job.abandoned.is_set()]

    # ------------------------------------------------------------------ heartbeat
    def _heartbeat_loop(self) -> None:
        interval = max(self.lease / 4, 0.2)
        while not self.stop.is_set() or self._has_active():
            self._beat()
            if self.stop.wait(interval) and not self._has_active():
                return

    def _has_active(self) -> bool:
        with self._lock:
            return bool(self._active())

    def _beat(self) -> None:
        with self._lock:
            jobs = self._active()
        try:
            with self.db.session() as s:
                repo.beat_worker(s, self.worker_id)
                s.commit()
            for job in jobs:
                with self.db.session() as s:
                    db.set_tenant(s, job.tenant_id)
                    if self._wedged(s, job):
                        # no heartbeat: the lease expires and the reaper settles the run
                        s.commit()
                        if not job.lease_lost.is_set():
                            log.warning("worker.wedged_job_abandoned", job_id=str(job.id))
                            job.lease_lost.set()
                        with self._lock:
                            job.abandoned.set()
                            self._idle.notify_all()
                        continue
                    alive = repo.heartbeat_job(s, job.tenant_id, job.id, self.worker_id, self.lease)
                    s.commit()
                if not alive and not job.lease_lost.is_set():
                    log.warning("worker.lease_lost", job_id=str(job.id))
                    job.lease_lost.set()
        except Exception:  # noqa: BLE001 - retry at the next beat
            log.error("worker.heartbeat_failed", exc_info=True)

    def _wedged(self, s: Session, job: _Job) -> bool:
        """The job's run (read in the job's own client) is past its deadline by `wedged_after`;
        a run-less job, `wedged_after` after this worker dispatched it."""
        if job.run_id is None:
            return utcnow() > job.started + self.wedged_after
        cur = repo.control_state(s, job.tenant_id, job.run_id)
        deadline = cur["deadline_at"] if cur is not None else None
        return deadline is not None and utcnow() > deadline + self.wedged_after

    # ------------------------------------------------------------------ LISTEN / NOTIFY
    def _listen_loop(self) -> None:
        url = make_url(self.settings.database_url).set(drivername="postgresql")
        while not self.stop.is_set():
            try:
                with psycopg.connect(
                    url.render_as_string(hide_password=False), autocommit=True
                ) as c:
                    c.execute("LISTEN shifde_jobs")
                    while not self.stop.is_set():
                        for _ in c.notifies(timeout=1.0):
                            self.wake.set()
            except Exception:  # noqa: BLE001 - polling continues to work without NOTIFY
                if self.stop.is_set():
                    return
                log.warning("worker.listen_failed", exc_info=True)
                self.stop.wait(2.0)

    # ------------------------------------------------------------------ reaper
    def _reap(self) -> None:
        try:
            with self.db.session() as s:
                got = s.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": REAPER_LOCK},
                ).scalar_one()
                rows = repo.reap(s) if got else []
                if got:
                    repo.mark_reap(s, self.worker_id)
                s.commit()
        except Exception:  # noqa: BLE001
            log.error("worker.reap_failed", exc_info=True)
            return
        if rows:
            self.wake.set()
        for row in rows:
            self._audit_reaped(row["tenant_id"], row["run_id"], row["reason"])
        if got:
            self._sweep_capture_off()

    def _audit_reaped(self, tenant_id: UUID, run_id: UUID, reason: str) -> None:
        self._settle_reaped_holds(tenant_id, run_id)
        try:
            with self.db.session() as s:
                db.init_context(s, "reaper")
                db.set_tenant(s, tenant_id)
                run = repo.get_run(s, tenant_id, run_id)
                if run is None:
                    return
                audit.record(
                    s,
                    RequestContext(request_id="reaper"),
                    action=REASON_ACTION.get(reason, "run.reaped"),
                    outcome="failure"
                    if reason in ("worker_lost", "timed_out", "unknown_outcome")
                    else "success",
                    tenant_id=tenant_id,
                    actor_id=None,
                    target_type="run",
                    target_id=run_id,
                    target_label=f"run:{run_id}",
                    workspace_id=run["workspace_id"],
                    project_id=run["project_id"],
                    details={"run_id": str(run_id), "reason": reason, "by": "reaper"},
                )
                # capture off: the reaper ended the run inside the database (B-2514)
                scrub_ended(s, run, self.settings)
                s.commit()
        except Exception:  # noqa: BLE001 - auditing the reaper must never stop it
            log.error("worker.reap_audit_failed", exc_info=True)

    def _sweep_capture_off(self) -> None:
        """Capture off (F-12): scrub the ended runs (run and root terminal) whose scrub never ran
        (a follow-up transaction failed, or a worker died first). Clients are listed under the
        platform-administrator context; each one is then swept in a transaction of its own with
        only that client set, under a per-client advisory lock. Idempotent; writes no audit."""
        try:
            with self.db.session() as s:
                with db.platform_admin_context(s):
                    clients = list(s.execute(_CLIENTS).scalars())
                s.commit()
        except Exception:  # noqa: BLE001 - the sweep must never stop the reaper
            log.error("worker.capture_sweep_failed", exc_info=True)
            return
        for tenant_id in clients:
            self._sweep_client(tenant_id)

    def _sweep_client(self, tenant_id: UUID) -> None:
        with self.db.session() as s:
            try:
                db.init_context(s, "reaper")
                db.set_tenant(s, tenant_id)
                got = s.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": CAPTURE_SWEEP_LOCK.format(tenant_id)},
                ).scalar_one()
                if not got or capture_full(s, tenant_id, self.settings):
                    s.rollback()
                    return
                run_ids = repo.unscrubbed_ended_runs(s, tenant_id)
                for run_id in run_ids:
                    repo.scrub_run_content(s, tenant_id, run_id)
                s.commit()
                if run_ids:
                    log.info("worker.capture_swept", tenant_id=str(tenant_id), runs=len(run_ids))
            except Exception:  # noqa: BLE001 - one client's failure must not stop the others
                s.rollback()
                log.error("worker.capture_sweep_failed", tenant_id=str(tenant_id), exc_info=True)
            finally:
                db.forget_tenant(s)

    def _settle_reaped_holds(self, tenant_id: UUID, run_id: UUID) -> None:
        """The reaped run's orphaned holds, in the run's own client and its own transaction
        (`settle_ended_run` left the ones whose call may have reached the provider `held`): a
        failure here leaves them held, still counting and never expiring."""
        try:
            with self.db.session() as s:
                db.init_context(s, "reaper")
                db.set_tenant(s, tenant_id)
                ledger.reservations.settle_orphaned(s, tenant_id, run_id)
                s.commit()
        except Exception:  # noqa: BLE001 - settling holds must never stop the reaper
            log.error("worker.reap_holds_failed", exc_info=True)


def _install_signals(worker: Worker) -> None:
    def handler(_signum: int, _frame: object) -> None:
        worker.request_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread / unsupported here
                log.warning("worker.signal_unavailable", signal=name)


def refuse_unsafe_role(settings: Settings) -> None:
    """Exit 3 with one line on stderr unless `SHIFDE_DATABASE_URL` connects as a safe application
    role, before anything is claimed (as `shifde-api`, B-2164). Fails closed: a role that cannot
    be checked is refused too."""
    try:
        check_database_url(settings.database_url)
    except UnsafeDatabaseRole as exc:
        print(f"shifde-worker: refusing to start: {exc.reason}", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_UNSAFE_ROLE) from None
    except SQLAlchemyError as exc:
        # the driver message can echo connection details; report the error class only
        print(
            "shifde-worker: refusing to start: cannot verify the database role "
            f"({exc.__class__.__name__})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(EXIT_UNSAFE_ROLE) from None


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shifde-worker", description="SHIFDE execution worker")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument("--poll-ms", type=int, default=None)
    parser.add_argument("--reap-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    settings = settings_or_exit()
    refuse_unsafe_role(settings)
    configure_logging(settings.log_level, settings.log_format)
    tracing.configure_from_settings(settings, "shifde-worker")
    worker = Worker(
        settings,
        worker_id=args.worker_id,
        concurrency=args.concurrency,
        lease_seconds=args.lease_seconds,
        poll_ms=args.poll_ms,
        reap_seconds=args.reap_seconds,
    )
    _install_signals(worker)
    worker.run()


if __name__ == "__main__":
    run()
