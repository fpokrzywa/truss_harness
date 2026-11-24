"""`shifde-worker`: the execution plane (SYSTEM_ARCHITECTURE 6.3).

A separate process that claims jobs from the PostgreSQL queue (`shifde_claim_jobs`, FOR UPDATE SKIP
LOCKED, ids only), runs each run on a worker thread, heartbeats the leases it holds, runs the
reaper (guarded by an advisory lock so one reaper acts at a time), and wakes on LISTEN / NOTIFY or
by polling. The API never executes model or tool calls.

Shutdown (SIGINT / SIGTERM / CTRL_BREAK): stop claiming; running runs notice at their next
checkpoint and are handed back (running -> pending, job released) so another worker continues
them; whatever does not finish within `worker_shutdown_grace_seconds` is left to lease expiry +
the reaper, and the worker still exits 0. Each job runs on its own daemon thread, so a thread
abandoned as wedged (or still running when the grace ends) never holds up process exit.

Process control (SP4 design 6.2): exit 0 stopped on request, 1 crashed (or stopped at the
abandoned cap, below: restart it), 2 configuration refused, 3 unsafe database role, 4 database
schema not at the code's head (checked at startup, before anything is claimed). With
`worker_health_port` set, `GET /healthz` is 200 while the heartbeat loop ticked within two of its
intervals and `GET /readyz` while the database answers, a main-loop tick (a claim pass or a
reaper sweep step) happened within three polls and no stop was requested
(`shifde.core.healthserver`). A stop request ends a reaper sweep early, after the client in
progress; the sweeps are idempotent, so the next reap finishes them.

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

Abandoned threads are capped (issue #44): a client with `worker_max_abandoned_per_client` (at most
`worker_max_abandoned`) abandoned jobs in flight gets every job of it this worker claims handed
back (released, one lease of backoff), while other clients' jobs run as usual; once the worker
holds `worker_max_abandoned` abandoned jobs it claims nothing more, hands back what it had just
claimed, stops (running jobs are handed back at their next checkpoint, abandoned threads are not
waited for) and `shifde-worker` exits 1, so a supervisor is expected to restart it with a fresh
pool and fresh threads. The caps only gate claiming and dispatch: a wedged run is still settled
by lease expiry and the reaper (C1), never cancelled.

Capture-off sweep (F-12, issue #34): on a tick where this worker won the reaper lock, the reaper
also sweeps every capture-off client for ended runs (run and root terminal) whose content scrub
never ran (the reaper's follow-up transaction failed, or a worker died first) and scrubs them:
per client, in a transaction of its own under that client's context and a per-client advisory
lock. A failure is logged and the sweep moves on to the next client.

Invocation-hold sweep (issue #91): on the same ticks the reaper also settles, per client in a
transaction of its own under that client's context, every direct-invocation hold whose provider
call may have started (marker set) and whose TTL has passed
(`ledger.reservations.settle_orphaned_invocations`): it becomes `settled_unknown` at its full
amount with one unknown-usage row attributed from the budgets and quotas it names. A failure is
rolled back and logged, and the sweep moves on to the next client.

Jobs are dispatched by `kind` (ARCH M2, B-2610, B-2720, B-2971, issue #190): `run.advance`
advances its run; `connection.test` and `connection.discover` (no run) run
`connections.service.run_test_job` and `run_discover_job`, `connection.invoke` (no run) runs
`connections.service.run_invoke_job`, and `model.health_check` and `model.discover` (no run) run
`models_admin.service.run_provider_job`, in the job's own client as its requester, re-derived
from the database, and end `done`, or `dead` with the refusal's error code (an invoke records its
refusal on its invocation and ends `done`). The built-in kinds are checked first, then the
`JobKindRegistry` the worker was given (`runtime.jobkinds`, built by the composition root
`worker_main`, so `runtime` never imports the modules above it): a registered handler runs
the job in its own client and returns None (`done`) or an error code (`dead` with that code), as
the built-in run-less handlers do; any other kind ends `dead` without running.

Beside the `JobKindRegistry`, the composition root hands the worker an `InternalToolRegistry`
(`runtime.internal_tools`): the `seth.*` functions a `builtin.function` tool may bind. The worker
passes it through `lifecycle.advance` to the run's `RunContext`, where `steps.py` runs such a tool
in the run, in the step's session under the run principal; without a registry, or without the key
in it, the call fails with `internal_function_unavailable`.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
from collections import Counter
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
from shifde.core.health import database_reachable, migration_state
from shifde.core.healthserver import HealthServer
from shifde.core.settings import Settings, settings_or_exit
from shifde.models_admin import service as models_admin
from shifde.observability import tracing
from shifde.observability.logging import configure_logging
from shifde.runtime import lifecycle
from shifde.runtime import repository as repo
from shifde.runtime.governance import GovernanceHooks, TokenBudgetHooks
from shifde.runtime.internal_tools import InternalToolRegistry
from shifde.runtime.jobkinds import JobKindRegistry
from shifde.runtime.settle import capture_full, scrub_ended

log = structlog.get_logger("shifde.worker")
REAPER_LOCK = "shifde:reaper"
CAPTURE_SWEEP_LOCK = "shifde:capture-sweep:{}"  # per client
_CLIENTS = text("SELECT id FROM shifde.tenants ORDER BY id")
EXIT_CONFIG = 2  # as `settings_or_exit`: a configuration a restart cannot fix
EXIT_UNSAFE_ROLE = 3  # as `shifde-api` (shifde.main)
EXIT_SCHEMA_MISMATCH = 4  # the database schema is not at the code's head
EXIT_ABANDONED_CAP = 1  # stopped at `worker_max_abandoned` abandoned threads: restart the worker
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
    run_id: UUID | None  # None: a run-less job (`connection.*`, `model.*`, ARCH M2)
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
        job_handlers: JobKindRegistry | None = None,
        internal_tools: InternalToolRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.job_handlers = job_handlers or JobKindRegistry.of({})
        # None (no registry): a `seth.*` tool call fails with `internal_function_unavailable`
        self.internal_tools = internal_tools
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        self.concurrency = concurrency or settings.worker_concurrency
        self.lease = lease_seconds or settings.worker_lease_seconds
        self.poll = (poll_ms or settings.worker_poll_ms) / 1000
        self.reap_every = reap_seconds if reap_seconds is not None else max(self.lease / 4, 1.0)
        # the application role only (`Database` refuses a privileged role, B-2164); a statement
        # (a lock wait included) is bounded, so a thread cannot block forever on the database
        # (ARCH M3). An abandoned thread holds no pooled connection while its call blocks: the step
        # commits its write-ahead journal (`ctx.txn`) and `open_for_use` commits the advance
        # session before the adapter call, so the session has no transaction across it; the
        # sizing therefore does not depend on `max_abandoned`.
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
        # caps on abandoned threads: the worker stops at `max_abandoned`; a client at its share has
        # its claimed jobs handed back (issue #44)
        self.max_abandoned = settings.worker_max_abandoned
        self.max_abandoned_per_client = min(
            settings.worker_max_abandoned_per_client, self.max_abandoned
        )
        self.exit_code = 0
        self.grace = settings.worker_shutdown_grace_seconds
        self.health: HealthServer | None = None
        # monotonic times of the last heartbeat-loop tick and main-loop claim tick (`/healthz`,
        # `/readyz`)
        self.beat_interval = max(self.lease / 4, 0.2)
        self.heartbeat_tick: float | None = None
        self.claim_tick: float | None = None
        self.runtime = runtime or IntegrationRuntime(
            settings=settings,
            registry=AdapterRegistry.load(include_fake=settings.env != "production"),
            resolver=default_resolver,
        )
        self.hooks = hooks or TokenBudgetHooks()
        self.stop = threading.Event()
        self.closed = threading.Event()  # shut down: the heartbeat loop ends
        self.wake = threading.Event()
        self._inflight: dict[UUID, _Job] = {}
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)  # a job ended or was abandoned

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> int:
        """Run until stopped; return the process exit code (`EXIT_ABANDONED_CAP` after the cap
        stop, else 0, also when the shutdown grace ran out). A health port that cannot be bound
        exits `EXIT_CONFIG`."""
        with self.db.session() as s:
            repo.upsert_worker(
                s, self.worker_id, socket.gethostname(), os.getpid(), __version__, self.concurrency
            )
            s.commit()
        next_reap = 0.0
        try:
            self._start_health()
            log.info("worker.started", worker_id=self.worker_id, concurrency=self.concurrency)
            for target in (self._heartbeat_loop, self._listen_loop):
                threading.Thread(target=target, daemon=True, name=target.__name__).start()
            while not self.stop.is_set():
                self.claim_tick = time.monotonic()
                self._claim_and_dispatch()
                if time.monotonic() >= next_reap:
                    self._reap()
                    next_reap = time.monotonic() + self.reap_every
                self.wake.wait(self.poll)
                self.wake.clear()
        finally:
            self._shutdown()
        return self.exit_code

    def request_stop(self) -> None:
        self.stop.set()
        self.wake.set()

    def _shutdown(self) -> None:
        """Wait up to the grace for the running jobs, then mark the worker stopped: a job still
        running is left to lease expiry and the reaper (its thread is a daemon)."""
        log.info("worker.stopping", worker_id=self.worker_id)
        with self._idle:
            drained = self._idle.wait_for(lambda: not self._active(), timeout=self.grace)
            active = len(self._active())
        if not drained:
            log.warning(
                "worker.shutdown_grace_expired",
                worker_id=self.worker_id,
                grace_seconds=self.grace,
                active=active,
            )
        self.closed.set()  # no more heartbeats: the leases of what still runs expire
        try:
            with self.db.session() as s:
                s.execute(text("SET LOCAL statement_timeout = '2s'"))
                repo.beat_worker(s, self.worker_id, "stopped")
                s.commit()
        except Exception:  # noqa: BLE001 - a missed beat is left to lease expiry
            log.error("worker.stop_beat_failed", worker_id=self.worker_id, exc_info=True)
        finally:
            if self.health is not None:
                self.health.stop()
            self.db.dispose()
        log.info("worker.stopped", worker_id=self.worker_id)

    # ------------------------------------------------------------------ health port
    def _start_health(self) -> None:
        port = self.settings.worker_health_port
        if port == 0:
            return
        try:
            self.health = HealthServer(
                self.settings.worker_health_host, port, live=self.live, ready=self.ready
            ).start()
        except OSError as exc:
            print(
                f"shifde-worker: refusing to start: cannot bind the health port {port} "
                f"({exc.__class__.__name__})",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(EXIT_CONFIG) from None

    def live(self) -> bool:
        """`/healthz`: the heartbeat loop ticked within two of its intervals."""
        tick = self.heartbeat_tick
        return tick is not None and time.monotonic() - tick <= 2 * self.beat_interval

    def ready(self) -> bool:
        """`/readyz`: no stop requested, a main-loop tick (a claim pass or a reaper sweep step)
        within three polls, the database answers."""
        tick = self.claim_tick
        if self.stop.is_set() or tick is None or time.monotonic() - tick > 3 * self.poll:
            return False
        with self.db.session() as s:
            return database_reachable(s)

    # ------------------------------------------------------------------ claiming
    def _claim_and_dispatch(self) -> None:
        with self._lock:
            free = self.concurrency - len(self._active())
            abandoned = self._abandoned()[0]
        if abandoned >= self.max_abandoned:
            self._stop_at_cap(abandoned)
            return
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
                abandoned, per_client = self._abandoned()
                busy = job.id in self._inflight
                capped = abandoned >= self.max_abandoned
                client_capped = per_client[job.tenant_id] >= self.max_abandoned_per_client
                if not (busy or capped or client_capped):
                    self._inflight[job.id] = job
            if busy:
                # The reaper requeued a job this worker is still executing (its heartbeat could not
                # reach the database) and we claimed it again. Never run one job twice here and
                # never replace the live entry: hand the claim back untouched.
                log.warning("worker.reclaimed_inflight_job", job_id=str(job.id))
                self._release_claim(job)
                continue
            if capped:
                self._stop_at_cap(abandoned)
                self._release_claim(job)
                continue
            if client_capped:
                # this client already holds its share of abandoned threads: another worker (or
                # this one, once they return) runs the job
                log.warning(
                    "worker.client_abandoned_cap",
                    tenant_id=str(job.tenant_id),
                    job_id=str(job.id),
                    abandoned=per_client[job.tenant_id],
                )
                self._release_claim(job)
                continue
            threading.Thread(
                target=self._work, args=(job,), daemon=True, name=f"shifde-run-{job.id}"
            ).start()
            started += 1
        return started

    def _stop_at_cap(self, abandoned: int) -> None:
        """Stop claiming and stop the worker, which then exits `EXIT_ABANDONED_CAP`."""
        if self.exit_code != EXIT_ABANDONED_CAP:
            log.warning(
                "worker.abandoned_cap_reached",
                worker_id=self.worker_id,
                abandoned=abandoned,
                max_abandoned=self.max_abandoned,
            )
            self.exit_code = EXIT_ABANDONED_CAP
        self.request_stop()

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
                internal_tools=self.internal_tools,
            )
        if job.kind == connections.TEST_JOB_KIND:
            # in the job's own client, as its requester re-derived from the database (B-2610)
            job.error = connections.run_test_job(self.db, self.runtime, job.tenant_id, job.id)
        elif job.kind == connections.DISCOVER_JOB_KIND:
            # the same, for a connection discovery (B-2971)
            job.error = connections.run_discover_job(self.db, self.runtime, job.tenant_id, job.id)
        elif job.kind == connections.INVOKE_JOB_KIND:
            # the same, at most once: the outcome is recorded on its invocation (issue #190)
            job.error = connections.run_invoke_job(self.db, self.runtime, job.tenant_id, job.id)
        elif job.kind in (models_admin.HEALTH_JOB_KIND, models_admin.DISCOVER_JOB_KIND):
            # the same, for a provider health check or model discovery (B-2720)
            job.error = models_admin.run_provider_job(self.db, self.runtime, job.tenant_id, job.id)
        elif job.kind in self.job_handlers:
            # a kind of a module above `runtime`, registered by the composition root
            job.error = self.job_handlers[job.kind](self.db, self.runtime, job.tenant_id, job.id)
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
                # an ended invoke job keeps its ids and hash, never the request (issue #224)
                drop = job.kind == connections.INVOKE_JOB_KIND
                if outcome == "released":
                    repo.release_job(s, job.tenant_id, job.id, self.worker_id)
                elif outcome == "dead":
                    repo.finish_job(
                        s,
                        job.tenant_id,
                        job.id,
                        self.worker_id,
                        "dead",
                        last_error=job.error,
                        drop_request=drop,
                    )
                else:
                    repo.finish_job(s, job.tenant_id, job.id, self.worker_id, drop_request=drop)
                s.commit()
        except Exception:  # noqa: BLE001 - the lease expires and the run is already settled
            log.error("worker.job_settle_failed", job_id=str(job.id), exc_info=True)

    def _active(self) -> list[_Job]:
        """The in-flight jobs not abandoned as wedged (call with `self._lock` held)."""
        return [job for job in self._inflight.values() if not job.abandoned.is_set()]

    def _abandoned(self) -> tuple[int, Counter[UUID]]:
        """The in-flight jobs abandoned as wedged, in total and per client (call with
        `self._lock` held)."""
        per_client = Counter(j.tenant_id for j in self._inflight.values() if j.abandoned.is_set())
        return sum(per_client.values()), per_client

    # ------------------------------------------------------------------ heartbeat
    def _heartbeat_loop(self) -> None:
        interval = self.beat_interval
        while (not self.stop.is_set() or self._has_active()) and not self.closed.is_set():
            self._beat()
            self.heartbeat_tick = time.monotonic()
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
                            with self._lock:
                                abandoned, per_client = self._abandoned()
                            log.warning(
                                "worker.wedged_job_abandoned",
                                job_id=str(job.id),
                                tenant_id=str(job.tenant_id),
                                abandoned=abandoned + 1,  # this job included
                                client_abandoned=per_client[job.tenant_id] + 1,
                            )
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
        self.claim_tick = time.monotonic()
        if rows:
            self.wake.set()
        for row in rows:
            self._audit_reaped(row["tenant_id"], row["run_id"], row["reason"])
        if got and not self.stop.is_set():
            self._sweep_capture_off()
            self._sweep_invocation_holds()

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
        only that client set, under a per-client advisory lock. Idempotent; writes no audit. Ends
        early on a stop request; each client swept is a main-loop tick for readiness."""
        clients = self._clients("worker.capture_sweep_failed")
        for tenant_id in clients or []:
            if self.stop.is_set():
                return
            self._sweep_client(tenant_id)
            self.claim_tick = time.monotonic()

    def _clients(self, event: str) -> list[UUID] | None:
        """Every client's id, listed under the platform-administrator context; None (logged as
        `event`) when the listing fails."""
        try:
            with self.db.session() as s:
                with db.platform_admin_context(s):
                    clients = list(s.execute(_CLIENTS).scalars())
                s.commit()
        except Exception:  # noqa: BLE001 - the sweep must never stop the reaper
            log.error(event, exc_info=True)
            return None
        return clients

    def _sweep_invocation_holds(self) -> None:
        """Settle every client's marked direct-invocation holds past their TTL (issue #91), each
        client in a transaction of its own with only that client set. Idempotent. Ends early on a
        stop request; each client swept is a main-loop tick for readiness."""
        for tenant_id in self._clients("worker.invocation_sweep_failed") or []:
            if self.stop.is_set():
                return
            self._sweep_client_invocations(tenant_id)
            self.claim_tick = time.monotonic()

    def _sweep_client_invocations(self, tenant_id: UUID) -> None:
        with self.db.session() as s:
            try:
                db.init_context(s, "reaper")
                db.set_tenant(s, tenant_id)
                settled = ledger.reservations.settle_orphaned_invocations(s, tenant_id)
                s.commit()
                if settled:
                    log.info("worker.invocations_swept", tenant_id=str(tenant_id), holds=settled)
            except Exception:  # noqa: BLE001 - one client's failure must not stop the others
                s.rollback()
                log.error("worker.invocation_sweep_failed", tenant_id=str(tenant_id), exc_info=True)
            # invoke jobs the reaper ended (`stale lease`) still hold their request (issue #224);
            # their own transaction, so a failure never undoes the holds' settlement
            try:
                db.set_tenant(s, tenant_id)
                scrubbed = connections.scrub_ended_invoke_jobs(s, tenant_id)
                s.commit()
                if scrubbed:
                    log.info(
                        "worker.invoke_payloads_swept", tenant_id=str(tenant_id), jobs=scrubbed
                    )
            except Exception:  # noqa: BLE001 - one client's failure must not stop the others
                s.rollback()
                log.error(
                    "worker.invoke_payload_sweep_failed", tenant_id=str(tenant_id), exc_info=True
                )
            finally:
                db.forget_tenant(s)

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


def refuse_schema_mismatch(settings: Settings) -> None:
    """Exit 4 with one line on stderr unless the database schema is at the code's head, read on a
    short-lived application-role connection before a `Worker` is built (SP4 design 6.2). The line
    names revision ids only; a driver error, its class only."""
    try:
        database = Database(settings.database_url, pool_size=1, max_overflow=0)
        try:
            with database.session() as s:
                state = migration_state(s)
        finally:
            database.dispose()
    except SQLAlchemyError as exc:
        print(
            "shifde-worker: refusing to start: cannot read the database schema revision "
            f"({exc.__class__.__name__})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(EXIT_SCHEMA_MISMATCH) from None
    if state.status != "ok":
        print(
            f"shifde-worker: refusing to start: database schema is {state.status} "
            f"(revision {state.revision}, code head {state.head})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(EXIT_SCHEMA_MISMATCH)


def run(
    argv: list[str] | None = None,
    job_handlers: JobKindRegistry | None = None,
    internal_tools: InternalToolRegistry | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="shifde-worker", description="SHIFDE execution worker")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument("--poll-ms", type=int, default=None)
    parser.add_argument("--reap-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    settings = settings_or_exit()
    refuse_unsafe_role(settings)
    refuse_schema_mismatch(settings)
    configure_logging(settings.log_level, settings.log_format)
    tracing.configure_from_settings(settings, "shifde-worker")
    worker = Worker(
        settings,
        worker_id=args.worker_id,
        concurrency=args.concurrency,
        lease_seconds=args.lease_seconds,
        poll_ms=args.poll_ms,
        reap_seconds=args.reap_seconds,
        job_handlers=job_handlers,
        internal_tools=internal_tools,
    )
    _install_signals(worker)
    return worker.run()


if __name__ == "__main__":
    sys.exit(run())
