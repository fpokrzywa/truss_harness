"""`shifde-worker` process control (SP4 design 6.2): the optional `/healthz` + `/readyz` port, the
bounded shutdown grace and exit 4 when the database schema is not at the code's head.

No socket is opened with the default settings (port 0). With a port, `/healthz` follows the
heartbeat loop's ticks (two intervals) and `/readyz` the main-loop ticks (a claim pass or a
lease reap, three polls) and the worker's own database; a stop request makes readiness
503 and any other path is 404. A job blocked past `worker_shutdown_grace_seconds` does not hold
`Worker.run()` up, a stop request ends a slow reaper sweep early (issue #188) and a failing
"stopped" beat does not fail the exit. The predicates are driven through recorded tick times
rather than real waits.

Client sweeps (issue #363): a due sweep held up by one that has run past `max(2 * lease, 60)` s logs
`worker.sweep_still_running`, and reaps stop recording `last_reap_at` (platform health "stale")
until it ends, while the worker stays ready and reaps leases; a sweep thread that cannot start is
raised without waiting out the grace; a stop ends the sweep before the second sweep's listing.
Isolation: while the real sweep step of client A is held, the same worker runs client B's real job
in B, and B's other rows do not change.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import text

from shifde.adapters.runtime import IntegrationRuntime
from shifde.adapters.testservers import RestTestServer
from shifde.connections import service as connections
from shifde.core import settings as settings_mod
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config, code_heads
from shifde.core.settings import Settings
from shifde.governance.service import health_details
from shifde.runtime import worker as worker_mod
from shifde.runtime.governance import PolicyBudgetHooks
from shifde.runtime.worker import Worker
from tests.build.service_support import REGISTRY
from tests.conftest import DB_PREFIX, _create_database, _drop_database, _with_db
from tests.runtime.conftest import insert
from tests.runtime.test_capture_off import (
    SCRUBBED,
    _approval,
    _arguments,
    _artifacts,
    _client,
    _seed,
    _snapshot,
)
from tests.runtime.test_worker import T, _test_job, _tests_of
from tests.tenancy.conftest import World, world

__all__ = ["world"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _worker(settings: Settings, worker_id: str, **update: Any) -> Worker:
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    return Worker(
        settings.model_copy(update=update),
        worker_id=worker_id,
        runtime=runtime,
        hooks=PolicyBudgetHooks(),
        concurrency=1,
        lease_seconds=2,
        poll_ms=50,
        reap_seconds=3600.0,
    )


def _get(port: int, path: str) -> tuple[int, Any]:  # Any: the JSON body
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _until(check: Callable[[], bool], timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


@pytest.fixture
def running() -> Iterator[Callable[[Worker], threading.Thread]]:
    """Start `Worker.run()` loops in threads; each is stopped and joined at teardown."""
    started: list[tuple[Worker, threading.Thread]] = []

    def start(w: Worker) -> threading.Thread:
        t = threading.Thread(target=w.run, daemon=True, name=f"test-{w.worker_id}")
        t.start()
        started.append((w, t))
        return t

    yield start
    for w, t in started:
        w.request_stop()
        t.join(60)
        assert not t.is_alive(), "a worker loop did not stop"


@pytest.fixture
def probed(settings: Settings) -> Iterator[Worker]:
    """A worker (not running) with its health port served: the tests set its ticks."""
    w = _worker(settings, "w-probed", worker_health_port=_free_port())
    w.poll = 2.0  # a readiness window (3 polls) longer than one probe round trip
    w._start_health()
    try:
        yield w
    finally:
        assert w.health is not None
        w.health.stop()
        w.db.dispose()


def _fresh(w: Worker) -> None:
    w.heartbeat_tick = w.claim_tick = time.monotonic()


def test_the_worker_opens_no_health_socket_by_default(
    settings: Settings, running: Callable[[Worker], threading.Thread]
) -> None:
    assert settings.worker_health_port == 0
    w = _worker(settings, "w-no-health")
    running(w)
    _until(lambda: w.claim_tick is not None and w.heartbeat_tick is not None)
    assert w.health is None


def test_a_running_worker_is_live_and_ready_and_an_unknown_path_is_404(
    settings: Settings, running: Callable[[Worker], threading.Thread]
) -> None:
    port = _free_port()
    w = _worker(settings, "w-health", worker_health_port=port)
    w.poll = 2.0  # a readiness window (3 polls) longer than one probe round trip
    running(w)
    _until(lambda: w.health is not None and w.live() and w.ready())
    assert w.health is not None and w.health.port == port
    assert _get(port, "/healthz") == (200, {"status": "ok"})
    assert _get(port, "/readyz") == (200, {"status": "ok"})
    assert _get(port, "/metrics") == (404, {"status": "not_found"})
    post = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", method="POST")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(post, timeout=10)  # noqa: S310 - loopback
    assert refused.value.code == 404


def test_readiness_follows_claim_ticks_and_the_database(probed: Worker) -> None:
    assert probed.health is not None
    port = probed.health.port
    _fresh(probed)
    assert _get(port, "/readyz") == (200, {"status": "ok"})
    probed.claim_tick = time.monotonic() - 3 * probed.poll - 1  # the main loop stopped ticking
    assert _get(port, "/readyz") == (503, {"status": "unavailable"})
    assert _get(port, "/healthz") == (200, {"status": "ok"})  # liveness is the heartbeat's
    _fresh(probed)
    own = probed.db
    # the database becomes unreachable (nothing listens on port 1)
    unreachable = make_url(probed.settings.database_url).set(host="127.0.0.1", port=1)
    probed.db = Database(
        unreachable.render_as_string(hide_password=False), verify_role=False, connect_timeout=2
    )
    try:
        assert _get(port, "/readyz") == (503, {"status": "unavailable"})
    finally:
        probed.db.dispose()
        probed.db = own
    _fresh(probed)
    assert _get(port, "/readyz") == (200, {"status": "ok"})


def test_liveness_follows_heartbeat_ticks(probed: Worker) -> None:
    assert probed.health is not None
    port = probed.health.port
    assert _get(port, "/healthz") == (503, {"status": "unavailable"})  # no tick yet
    _fresh(probed)
    assert _get(port, "/healthz") == (200, {"status": "ok"})
    probed.heartbeat_tick = time.monotonic() - 2 * probed.beat_interval - 1
    assert _get(port, "/healthz") == (503, {"status": "unavailable"})


def test_readiness_is_503_once_a_stop_is_requested(probed: Worker) -> None:
    assert probed.health is not None
    _fresh(probed)
    assert _get(probed.health.port, "/readyz")[0] == 200
    probed.request_stop()
    _fresh(probed)
    assert _get(probed.health.port, "/readyz") == (503, {"status": "unavailable"})


def test_a_job_blocked_past_the_grace_does_not_hold_the_worker_up(
    world: World,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()

    def blocked(db: Database, runtime: object, tenant_id: UUID, job_id: UUID) -> None:
        started.set()
        release.wait(60)

    monkeypatch.setattr(connections, "run_test_job", blocked)
    job = uuid7()
    with owner_db.session() as o:
        insert(o, "jobs", id=job, tenant_id=world.a.tenant, kind=connections.TEST_JOB_KIND,
               dedupe_key=f"grace:{job}")  # fmt: skip
        o.commit()
    w = _worker(settings, "w-grace", worker_shutdown_grace_seconds=1)
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-grace")
    try:
        loop.start()
        assert started.wait(30), "the job was never dispatched"
        with structlog.testing.capture_logs() as logs:
            stopped = time.monotonic()
            w.request_stop()
            loop.join(30)
            waited = time.monotonic() - stopped
        assert not loop.is_alive(), "the worker waited for the blocked job"
        assert exits == [0]
        assert 0.9 <= waited < 10, waited
        expired = [e for e in logs if e["event"] == "worker.shutdown_grace_expired"]
        assert len(expired) == 1 and expired[0]["active"] == 1
    finally:
        release.set()
        loop.join(30)


SWEPT_CLIENTS = 30
SWEEP_STEP = 0.5  # seconds per client: 30 x 0.5 s = 15 s, past the 10 s stop bound


@pytest.fixture
def slow_sweep(monkeypatch: pytest.MonkeyPatch) -> tuple[list[UUID], list[UUID]]:
    """The reaper's client sweeps under load: many clients, each capture-off step slow. Returns
    the clients the capture-off and the invocation-hold sweeps reached, in order."""
    captured: list[UUID] = []
    invocations: list[UUID] = []
    clients = [uuid7() for _ in range(SWEPT_CLIENTS)]

    def sweep_client(self: Worker, tenant_id: UUID) -> None:
        captured.append(tenant_id)
        time.sleep(SWEEP_STEP)

    monkeypatch.setattr(Worker, "_clients", lambda self, event: list(clients))
    monkeypatch.setattr(Worker, "_sweep_client", sweep_client)
    monkeypatch.setattr(
        Worker, "_sweep_client_invocations", lambda self, tenant_id: invocations.append(tenant_id)
    )
    return captured, invocations


def test_a_stop_during_a_slow_reaper_sweep_ends_the_sweep_early(
    settings: Settings, slow_sweep: tuple[list[UUID], list[UUID]]
) -> None:
    captured, invocations = slow_sweep
    w = _worker(settings, "w-slow-stop")
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-slow")
    loop.start()
    try:
        _until(lambda: len(captured) >= 1)  # the first reap's sweep is in progress
        stopped = time.monotonic()
        w.request_stop()
        loop.join(30)
        waited = time.monotonic() - stopped
        assert not loop.is_alive(), "the worker did not stop"
        assert exits == [0]
        assert waited < 10, waited
        assert len(captured) <= 3, len(captured)
        assert invocations == []
    finally:
        w.request_stop()
        loop.join(30)


def test_a_worker_in_a_slow_reaper_sweep_stays_ready(
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    slow_sweep: tuple[list[UUID], list[UUID]],
) -> None:
    captured, _ = slow_sweep
    w = _worker(settings, "w-slow-ready")
    w.poll = 2.0
    running(w)
    _until(lambda: len(captured) >= 1)
    began = time.monotonic()
    while time.monotonic() - began <= 3 * w.poll + 1:  # sweep steps past one readiness window
        assert w.ready(), len(captured)
        time.sleep(0.2)
    assert 3 * w.poll / SWEEP_STEP < len(captured) < SWEPT_CLIENTS


@dataclass
class HeldSweep:
    """A client sweep whose first capture-off step is held until `release` is set."""

    clients: list[UUID] = field(default_factory=lambda: [uuid7() for _ in range(3)])
    captured: list[UUID] = field(default_factory=list)  # clients the capture-off sweep reached
    invocations: list[UUID] = field(default_factory=list)  # ... the invocation-hold sweep reached
    ended: list[UUID] = field(default_factory=list)  # capture-off steps that returned
    holding: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    @property
    def in_progress(self) -> bool:
        return self.captured == self.clients[:1] and self.ended == []


@pytest.fixture
def held_sweep(monkeypatch: pytest.MonkeyPatch) -> Iterator[HeldSweep]:
    held = HeldSweep()

    def sweep_client(self: Worker, tenant_id: UUID) -> None:
        held.captured.append(tenant_id)
        held.holding.set()
        held.release.wait(300)
        held.ended.append(tenant_id)

    monkeypatch.setattr(Worker, "_clients", lambda self, event: list(held.clients))
    monkeypatch.setattr(Worker, "_sweep_client", sweep_client)
    monkeypatch.setattr(
        Worker,
        "_sweep_client_invocations",
        lambda self, tenant_id: held.invocations.append(tenant_id),
    )
    yield held
    held.release.set()


def _queued(owner_db: Database, tenant: UUID, **values: Any) -> UUID:
    job = uuid7()
    with owner_db.session() as o:
        insert(o, "jobs", id=job, tenant_id=tenant, kind=connections.TEST_JOB_KIND,
               dedupe_key=f"sweep:{job}", **values)  # fmt: skip
        o.commit()
    return job


def _job_state(owner_db: Database, job: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        return tuple(
            o.execute(
                text("SELECT status, lease_owner FROM shifde.jobs WHERE id = :j"), {"j": job}
            ).one()
        )


def test_a_job_queued_during_a_client_sweep_ends_before_the_sweep(
    world: World,
    owner_db: Database,
    settings: Settings,
    held_sweep: HeldSweep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #361: the reaper's sweeps visit every client; a worker with one slot claims client
    B's job, queued while the sweep is held at its first client, and runs it to its end."""
    ran: list[tuple[UUID, UUID]] = []

    def done(db: Database, runtime: object, tenant_id: UUID, job_id: UUID) -> None:
        ran.append((tenant_id, job_id))

    monkeypatch.setattr(connections, "run_test_job", done)
    first = _queued(owner_db, world.a.tenant)
    w = _worker(settings, "w-sweep-claim")
    loop = threading.Thread(target=w.run, daemon=True, name="test-sweep-claim")
    loop.start()
    try:
        _until(lambda: _job_state(owner_db, first) == ("done", "w-sweep-claim"))
        assert held_sweep.holding.wait(30), "the first reap's sweep never started"
        second = _queued(owner_db, world.b.tenant)
        _until(lambda: _job_state(owner_db, second) == ("done", "w-sweep-claim"))
        assert held_sweep.in_progress and not held_sweep.release.is_set()
        assert ran == [(world.a.tenant, first), (world.b.tenant, second)]
        held_sweep.release.set()  # the sweep goes on: every client, capture-off first
        _until(lambda: held_sweep.invocations == held_sweep.clients)
        assert held_sweep.captured == held_sweep.clients
    finally:
        held_sweep.release.set()
        w.request_stop()
        loop.join(60)
    assert not loop.is_alive(), "the worker did not stop"


def test_a_reap_due_during_a_client_sweep_reaps_leases_and_starts_no_second_sweep(
    world: World,
    owner_db: Database,
    settings: Settings,
    held_sweep: HeldSweep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaps: list[str] = []
    mark_reap = worker_mod.repo.mark_reap

    def counted(session: Any, worker_id: str) -> None:  # Any: a Session
        mark_reap(session, worker_id)
        reaps.append(worker_id)

    monkeypatch.setattr(worker_mod.repo, "mark_reap", counted)
    w = _worker(settings, "w-sweep-reap")
    w.reap_every = 0.2
    loop = threading.Thread(target=w.run, daemon=True, name="test-sweep-reap")
    loop.start()
    try:
        assert held_sweep.holding.wait(30), "the first reap's sweep never started"
        lost = _queued(owner_db, world.a.tenant, status="leased", lease_owner="w-dead",
                       lease_expires_at=utcnow() - timedelta(seconds=1), attempts=1)  # fmt: skip
        _until(lambda: _job_state(owner_db, lost)[0] == "done")  # closed as a stale lease
        seen = len(reaps)
        _until(lambda: len(reaps) >= seen + 3)
        w._reap()  # nor does a whole pass: it reaps and leaves the sweep to the one in progress
        assert held_sweep.in_progress and held_sweep.invocations == []
        held_sweep.release.set()
        # once it ended, a later reap sweeps again
        _until(lambda: len(held_sweep.captured) > len(held_sweep.clients))
    finally:
        held_sweep.release.set()
        w.request_stop()
        loop.join(60)
    assert not loop.is_alive(), "the worker did not stop"


def test_a_stopping_worker_waits_for_the_sweep_step_in_progress_and_ends_the_sweep(
    settings: Settings, held_sweep: HeldSweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _worker(settings, "w-sweep-stop")
    steps_at_dispose: list[int] = []
    dispose = w.db.dispose

    def disposed() -> None:
        steps_at_dispose.append(len(held_sweep.ended))
        dispose()

    monkeypatch.setattr(w.db, "dispose", disposed)
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-sstop")
    loop.start()
    try:
        assert held_sweep.holding.wait(30), "the first reap's sweep never started"
        w.request_stop()
        loop.join(2)
        assert loop.is_alive() and steps_at_dispose == []  # the step still uses the pool
        held_sweep.release.set()
        loop.join(30)
        assert not loop.is_alive(), "the worker did not stop"
        assert exits == [0] and steps_at_dispose == [1]
        assert held_sweep.captured == held_sweep.clients[:1] and held_sweep.invocations == []
    finally:
        held_sweep.release.set()
        w.request_stop()
        loop.join(30)


def test_a_sweep_step_blocked_past_the_grace_does_not_hold_the_worker_up(
    settings: Settings, held_sweep: HeldSweep
) -> None:
    w = _worker(settings, "w-sweep-grace", worker_shutdown_grace_seconds=1)
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-sgrace")
    loop.start()
    try:
        assert held_sweep.holding.wait(30), "the first reap's sweep never started"
        with structlog.testing.capture_logs() as logs:
            stopped = time.monotonic()
            w.request_stop()
            loop.join(30)
            waited = time.monotonic() - stopped
        assert not loop.is_alive(), "the worker waited for the blocked sweep step"
        assert exits == [0] and held_sweep.in_progress
        assert 0.9 <= waited < 10, waited
        assert len([e for e in logs if e["event"] == "worker.sweep_outlived_grace"]) == 1
    finally:
        held_sweep.release.set()
        loop.join(30)


def test_a_stuck_main_loop_turns_unready_during_a_client_sweep(
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    slow_sweep: tuple[list[UUID], list[UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, _ = slow_sweep
    stuck, release = threading.Event(), threading.Event()
    w = _worker(settings, "w-stuck")
    claim = w._claim_and_dispatch

    def blocked() -> None:
        if captured:
            stuck.set()
            release.wait(300)
        claim()

    monkeypatch.setattr(w, "_claim_and_dispatch", blocked)
    running(w)
    try:
        assert stuck.wait(30), "the main loop never ran during the sweep"
        _until(lambda: not w.ready())
        swept = len(captured)
        _until(lambda: len(captured) > swept)  # the sweep's steps do not stand in for the loop
        assert not w.ready()
    finally:
        release.set()


def test_a_reap_on_a_worker_that_is_not_running_completes_both_sweeps_and_claims_nothing(
    world: World, owner_db: Database, settings: Settings, held_sweep: HeldSweep
) -> None:
    held_sweep.release.set()
    job = _queued(owner_db, world.a.tenant)
    w = _worker(settings, "w-direct")
    try:
        for swept in (1, 2):
            w._reap()
            assert held_sweep.captured == held_sweep.ended == held_sweep.clients * swept
            assert held_sweep.invocations == held_sweep.clients * swept
    finally:
        w.db.dispose()
    assert _job_state(owner_db, job) == ("queued", None)


def _sweep_ran(w: Worker, seconds: float) -> None:
    """Move the start of the sweep in progress back, as if it had run `seconds` past the bound
    (negative: still short of it)."""
    w._sweep_started = time.monotonic() - w.wedged_after.total_seconds() - seconds


def _still_running(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == "worker.sweep_still_running"]


def test_a_whole_pass_due_during_an_overdue_sweep_logs_it_and_starts_no_second_one(
    settings: Settings, held_sweep: HeldSweep
) -> None:
    w = _worker(settings, "w-overdue")
    w.poll = 2.0  # a readiness window (3 polls) longer than one pass
    bound = w.wedged_after.total_seconds()
    assert bound == 60
    sweep = threading.Thread(target=w._reap, daemon=True, name="test-overdue")
    sweep.start()
    try:
        assert held_sweep.holding.wait(30), "the sweep never started"
        with structlog.testing.capture_logs() as logs:
            w._reap()  # a fresh sweep
            _sweep_ran(w, -30)  # a long one, still under the bound
            w._reap()
            assert w.ready()
        assert _still_running(logs) == []
        _sweep_ran(w, 5)
        with structlog.testing.capture_logs() as logs:
            for skipped in (1, 2):
                w._reap()
                assert w.ready()
                assert len(_still_running(logs)) == skipped
        for e in _still_running(logs):
            assert e["worker_id"] == "w-overdue" and e["log_level"] == "warning"
            assert bound < e["elapsed_seconds"] < bound + 60
        assert held_sweep.in_progress and held_sweep.invocations == []
        held_sweep.release.set()
        sweep.join(30)
        assert not sweep.is_alive() and held_sweep.invocations == held_sweep.clients
        assert w._sweep_started is None
        # the lock kept by a shutdown is no sweep in progress
        assert w._sweeping.acquire(blocking=False)
        with structlog.testing.capture_logs() as logs:
            w._reap()
        assert _still_running(logs) == []
    finally:
        held_sweep.release.set()
        sweep.join(30)
        w.db.dispose()


def test_a_running_worker_logs_an_overdue_sweep_and_stays_ready(
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    held_sweep: HeldSweep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaps: list[None] = []
    reap = worker_mod.repo.reap

    def counted(session: Session) -> Any:  # Any: reaped rows
        rows = reap(session)
        reaps.append(None)
        return rows

    def three_more_reaps() -> None:
        seen = len(reaps)  # the sweeps of the first two came due and were skipped by the third
        _until(lambda: len(reaps) >= seen + 3)

    monkeypatch.setattr(worker_mod.repo, "reap", counted)
    w = _worker(settings, "w-overdue-loop")
    w.poll, w.reap_every = 2.0, 0.2  # a readiness window (3 polls) of 6 s
    running(w)
    try:
        assert held_sweep.holding.wait(30), "the first reap's sweep never started"
        with structlog.testing.capture_logs() as logs:
            three_more_reaps()
            assert w.ready()
        assert _still_running(logs) == []
        with structlog.testing.capture_logs() as logs:
            _sweep_ran(w, 5)
            three_more_reaps()
            assert w.ready()
        overdue = _still_running(logs)
        assert len(overdue) >= 2  # one for every due sweep it holds up
        assert {e["worker_id"] for e in overdue} == {"w-overdue-loop"}
        assert all(e["elapsed_seconds"] > w.wedged_after.total_seconds() for e in overdue)
        assert held_sweep.in_progress and held_sweep.invocations == []
    finally:
        held_sweep.release.set()


def test_an_overdue_sweep_turns_the_reaper_stale_in_platform_health_until_it_ends(
    world: World,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
    held_sweep: HeldSweep,
) -> None:
    w = _worker(settings, "w-stale")
    w.poll = 2.0  # a readiness window (3 polls) longer than one pass
    with w.db.session() as s:
        worker_mod.repo.upsert_worker(s, w.worker_id, "host", 1, "0", 1)
        s.commit()
    horizon = max(settings.worker_lease_seconds * 3, 15)
    admin = Principal(uuid7(), "platform@example.test", "Platform", is_platform_admin=True)

    def last_reap() -> Any:  # Any: timestamptz
        with owner_db.session() as o:
            return o.execute(text("SELECT last_reap_at FROM shifde.workers")).scalar_one()

    def mark_long_ago() -> Any:  # Any: timestamptz
        with owner_db.session() as o:
            o.execute(
                text("UPDATE shifde.workers SET last_reap_at = now() - make_interval(secs => :s)"),
                {"s": horizon + 60},
            )
            o.commit()
        return last_reap()

    def reaper_status() -> str:
        with app_db.session() as s:
            return str(health_details(s, settings, admin).reaper["status"])

    try:
        w._start_sweep()
        assert held_sweep.holding.wait(30), "the sweep never started"
        old = mark_long_ago()
        assert reaper_status() == "stale"
        _sweep_ran(w, 5)
        lost = _queued(owner_db, world.a.tenant, status="leased", lease_owner="w-dead",
                       lease_expires_at=utcnow() - timedelta(seconds=1), attempts=1)  # fmt: skip
        w._reap()
        assert last_reap() == old and reaper_status() == "stale"
        assert _job_state(owner_db, lost)[0] == "done"  # closed as a stale lease all the same
        assert w.ready()

        _sweep_ran(w, -30)  # under the bound
        w._reap()
        assert last_reap() > old and reaper_status() == "ok"
        assert held_sweep.in_progress

        old = mark_long_ago()
        _sweep_ran(w, 5)
        w._reap()
        assert last_reap() == old and reaper_status() == "stale"
        held_sweep.release.set()
        _until(lambda: not w._sweeping.locked())
        assert held_sweep.invocations == held_sweep.clients
        w._reap()
        assert last_reap() > old and reaper_status() == "ok"
    finally:
        held_sweep.release.set()
        _until(lambda: not w._sweeping.locked())
        w.db.dispose()


def test_a_sweep_thread_that_cannot_start_crashes_the_worker_without_waiting_out_the_grace(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = RuntimeError("no thread for the sweep")
    failed: list[float] = []
    start = threading.Thread.start

    def failing(self: threading.Thread) -> None:
        if self.name == "_sweep":
            failed.append(time.monotonic())
            raise error
        start(self)

    monkeypatch.setattr(threading.Thread, "start", failing)
    w = _worker(settings, "w-no-thread", worker_shutdown_grace_seconds=10)
    try:
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(RuntimeError) as raised,
        ):
            w.run()
        waited = time.monotonic() - failed[0]
        assert raised.value is error and len(failed) == 1
        assert [e for e in logs if e["event"] == "worker.sweep_outlived_grace"] == []
        assert waited < 5, waited
        assert w.closed.is_set() and w._sweep_started is None
    finally:
        w.request_stop()  # the listen loop


def test_a_stop_during_the_capture_off_sweep_lists_no_clients_for_the_second(
    settings: Settings, held_sweep: HeldSweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[str] = []

    def clients(self: Worker, event: str) -> list[UUID]:
        listed.append(event)
        return list(held_sweep.clients)

    monkeypatch.setattr(Worker, "_clients", clients)
    w = _worker(settings, "w-sweep-list")
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-slist")
    loop.start()
    try:
        assert held_sweep.holding.wait(30), "the first reap's sweep never started"
        w.request_stop()
        held_sweep.release.set()
        loop.join(30)
        assert not loop.is_alive(), "the worker did not stop"
        assert exits == [0]
        assert listed == ["worker.capture_sweep_failed"]
        assert held_sweep.captured == held_sweep.clients[:1] and held_sweep.invocations == []
    finally:
        held_sweep.release.set()
        w.request_stop()
        loop.join(30)


def _rows_but_the_jobs(owner_db: Database, job: T) -> dict[str, list[str]]:
    """Every row of `job`'s client, by table, but the ones the connection test itself writes."""
    own = {
        "jobs": "id <> :job",
        "connections": "id <> :connection",
        "connection_tests": "id <> :test",
        "audit_events": "correlation_id IS DISTINCT FROM :request",
    }
    params = {"t": job.tenant, "job": job.job, "connection": job.connection, "test": job.test_id,
              "request": f"connection-test-{job.job}"}  # fmt: skip
    with owner_db.session() as o:
        tables = o.execute(
            text(
                "SELECT c.table_name FROM information_schema.columns c "
                "JOIN information_schema.tables t USING (table_schema, table_name) "
                "WHERE c.table_schema = 'shifde' AND c.column_name = 'tenant_id' "
                "AND t.table_type = 'BASE TABLE' ORDER BY 1"
            )
        ).scalars()
        return {
            name: list(
                o.execute(
                    text(
                        f"SELECT to_jsonb(x)::text FROM shifde.{name} x "
                        f"WHERE tenant_id = :t AND {own.get(name, 'true')} ORDER BY 1"
                    ),
                    params,
                ).scalars()
            )
            for name in tables
        }


def test_a_real_sweep_step_of_one_client_is_isolated_from_a_job_of_another_run_meanwhile(
    owner_db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #363: the real capture-off step of client A, held inside its transaction, and client
    B's real `connection.test` job on the same one-slot worker. Only the client listing is stubbed
    (to A), so nothing may touch B's own unscrubbed run."""
    a_tenant, a_user = _client(owner_db, "metadata")
    b_tenant, b_user = _client(owner_db, "metadata")
    with owner_db.session() as o:
        a = _seed(o, a_tenant, a_user, status="failed")
        b = _seed(o, b_tenant, b_user, status="failed")
        _approval(o, a_tenant, a.run, "approved")
        _approval(o, b_tenant, b.run, "approved")
        o.commit()
    steps: list[tuple[UUID, UUID, str]] = []  # client, run and the session's client
    ended: list[UUID] = []
    holding, release = threading.Event(), threading.Event()
    scrub = worker_mod.repo.scrub_run_content

    def held(session: Session, tenant_id: UUID, run_id: UUID) -> None:
        current = session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        steps.append((tenant_id, run_id, current.scalar_one()))
        holding.set()
        release.wait(300)
        scrub(session, tenant_id, run_id)
        ended.append(run_id)

    monkeypatch.setattr(worker_mod.repo, "scrub_run_content", held)
    monkeypatch.setattr(Worker, "_clients", lambda self, event: [a_tenant])
    run_after = (
        "UPDATE shifde.jobs SET run_after = now() + make_interval(hours => :h) WHERE id = :j"
    )
    with RestTestServer() as server:
        with owner_db.session() as o:
            job = _test_job(o, b_tenant, server)
            o.execute(text(run_after), {"h": 1, "j": job.job})  # not due yet
            o.commit()
        a_content, b_content = _snapshot(owner_db, a_tenant), _snapshot(owner_db, b_tenant)
        b_rows = _rows_but_the_jobs(owner_db, job)
        assert all(a_content) and all(b_content) and len(b_rows["runs"]) == 1
        w = _worker(settings, "w-sweep-isolated")
        loop = threading.Thread(target=w.run, daemon=True, name="test-sweep-isolated")
        loop.start()
        try:
            assert holding.wait(30), "the sweep step of A never started"
            assert steps == [(a_tenant, a.run, str(a_tenant))]
            with owner_db.session() as o:  # due while A's step is held
                o.execute(text(run_after), {"h": 0, "j": job.job})
                o.commit()
            _until(lambda: _job_state(owner_db, job.job) == ("done", "w-sweep-isolated"))
            assert any(r["path"] == "/health" for r in server.requests)
            [(test_id, connection, outcome, _)] = _tests_of(owner_db, b_tenant)
            assert (test_id, connection, outcome) == (job.test_id, job.connection, "pass")
            assert _tests_of(owner_db, a_tenant) == []
            assert _snapshot(owner_db, a_tenant) == a_content  # A's step has not committed
            assert _snapshot(owner_db, b_tenant) == b_content
            assert _rows_but_the_jobs(owner_db, job) == b_rows
            assert ended == [] and not release.is_set() and w._sweeping.locked()
            release.set()
            _until(lambda: not w._sweeping.locked())
        finally:
            release.set()
            w.request_stop()
            loop.join(60)
        assert not loop.is_alive(), "the worker did not stop"
    assert steps == [(a_tenant, a.run, str(a_tenant))] and ended == [a.run]
    artifacts, arguments = _artifacts(owner_db, a.run), _arguments(owner_db, a.run)
    assert artifacts and all(row[3] == SCRUBBED for row in artifacts)
    assert arguments and all(value is None for value in arguments)
    assert _snapshot(owner_db, b_tenant) == b_content
    assert _rows_but_the_jobs(owner_db, job) == b_rows
    assert len(_tests_of(owner_db, b_tenant)) == 1 and _tests_of(owner_db, a_tenant) == []


def test_a_failing_stopped_beat_does_not_fail_the_exit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = worker_mod.repo.beat_worker

    def failing(session: Any, worker_id: str, status: str = "running") -> None:  # Any: a Session
        if status == "stopped":
            raise RuntimeError("the stopped beat failed")
        beat(session, worker_id, status)

    monkeypatch.setattr(worker_mod.repo, "beat_worker", failing)
    closed: list[object] = []
    stop = worker_mod.HealthServer.stop

    def recorded(self: worker_mod.HealthServer) -> None:
        closed.append(self)
        stop(self)

    monkeypatch.setattr(worker_mod.HealthServer, "stop", recorded)
    w = _worker(settings, "w-stop-beat", worker_health_port=_free_port())
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-beat")
    loop.start()
    try:
        _until(lambda: w.claim_tick is not None)
        with structlog.testing.capture_logs() as logs:
            w.request_stop()
            loop.join(30)
        assert not loop.is_alive(), "the worker did not stop"
        assert exits == [0]
        failed = [e for e in logs if e["event"] == "worker.stop_beat_failed"]
        assert len(failed) == 1 and failed[0]["exc_info"] is True
        assert closed == [w.health]
    finally:
        w.request_stop()
        loop.join(30)


def test_the_worker_exits_4_on_a_database_schema_below_head(
    run_id: str,
    admin_url: str,
    app_url: str,
    owner_url: str,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = f"{DB_PREFIX}{run_id}_wschema"
    owner = make_url(owner_url).username or "shifde_owner"
    app_role = make_url(app_url).username or "shifde_app"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    scratch = _with_db(app_url, name)
    try:
        with admin.connect() as c:
            _drop_database(c, name)
            _create_database(c, name, owner, app_role, f"{DB_PREFIX}{run_id}_tmpl")
        command.downgrade(alembic_config(_with_db(owner_url, name)), f"{sorted(code_heads())[0]}-1")
        below = settings.model_copy(update={"database_url": scratch})
        monkeypatch.setattr(worker_mod, "settings_or_exit", lambda: below)
        built: list[object] = []
        monkeypatch.setattr(worker_mod, "Worker", lambda *a, **kw: built.append(a))
        with pytest.raises(SystemExit) as exited:
            worker_mod.run([])
        assert exited.value.code == worker_mod.EXIT_SCHEMA_MISMATCH == 4
        assert built == []
        err = capsys.readouterr().err
        assert "shifde-worker: refusing to start: database schema is pending (revision " in err
        password = make_url(app_url).password
        assert scratch not in err and (password is None or password not in err)
        assert "postgresql" not in err and "@" not in err
    finally:
        with contextlib.suppress(Exception), admin.connect() as c:
            _drop_database(c, name)
        admin.dispose()


@pytest.mark.parametrize(
    "update",
    [
        {"worker_health_port": 65536},
        {"worker_health_port": -1},
        {"worker_shutdown_grace_seconds": 0},
    ],
)
def test_out_of_range_worker_settings_are_refused(
    update: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def load() -> Settings:
        return Settings(_env_file=None, env="test", database_url="postgresql://x@h/d", **update)

    with pytest.raises(ValueError, match=next(iter(update))):
        load()
    monkeypatch.setattr(settings_mod, "get_settings", load)
    with pytest.raises(SystemExit) as exited:
        settings_mod.settings_or_exit()
    assert exited.value.code == 2
    assert f"SHIFDE_{next(iter(update)).upper()}" in capsys.readouterr().err
