"""The worker's client sweeps with several workers and many clients (issue #415).

One sweep at a time across workers: a sweep holds the session-level advisory lock `SWEEP_LOCK` on a
connection of its own; a worker that does not get it skips the sweep, and one that cannot try it
sweeps nothing. The lock is free as soon as the sweep ended, also after an exception or a stop, and
a holder whose step is stuck (or whose session ended) loses it and visits no further client. The
main loop and lease reaping wait for none of it.

Stuck is judged by progress: the bound (`wedged_after`) counts from the last finished client, in
either sweep, not from the start of the sweep.

The locks are real: the second session is the test's own connection, or another `Worker` of the same
database. Time passes by moving the worker module's clock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde import ledger
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import worker as worker_mod
from shifde.runtime.worker import REAPER_LOCK, SWEEP_LOCK, Worker
from tests._support.dberrors import rendered_logs
from tests.runtime.test_worker_health import (
    _job_state,
    _queued,
    _still_running,
    _until,
    _worker,
    running,
)
from tests.tenancy.conftest import World, world

__all__ = ["running", "world"]

CAPTURE = "worker.capture_sweep_failed"  # what `_clients` is listed for, by sweep
INVOCATIONS = "worker.invocation_sweep_failed"
LISTING = "listing"


class OtherSession:
    """A second database session of the application role."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def _bool(self, function: str, key: str) -> bool:
        row = self.connection.execute(
            f"SELECT {function}(hashtextextended(%s, 0))".encode(), (key,)
        ).fetchone()
        assert row is not None
        return bool(row[0])

    def try_lock(self, key: str = SWEEP_LOCK) -> bool:
        return self._bool("pg_try_advisory_lock", key)

    def unlock(self, key: str = SWEEP_LOCK) -> None:
        assert self._bool("pg_advisory_unlock", key)

    def is_free(self, key: str = SWEEP_LOCK) -> bool:
        if not self.try_lock(key):
            return False
        self.unlock(key)
        return True


@pytest.fixture
def other(settings: Settings) -> Iterator[OtherSession]:
    url = make_url(settings.database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as c:
        yield OtherSession(c)


@dataclass
class Gate:
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def wait(self) -> None:
        self.reached.set()
        self.release.wait(300)


Event = tuple[str, str, UUID | str]  # worker, sweep, client (or LISTING)


@dataclass
class Sweeps:
    """What every worker's sweeps did, in order; the step of an event in `gates` is held there."""

    clients: list[UUID] = field(default_factory=lambda: [uuid7() for _ in range(3)])
    events: list[Event] = field(default_factory=list)
    gates: dict[Event, Gate] = field(default_factory=dict)
    raising: Event | None = None

    def hold(self, worker: str, sweep: str = CAPTURE, client: int = 0) -> Gate:
        return self.gates.setdefault((worker, sweep, self.clients[client]), Gate())

    def step(self, event: Event) -> None:
        self.events.append(event)
        if event == self.raising:
            raise RuntimeError("the sweep broke")
        gate = self.gates.get(event)
        if gate is not None:
            gate.wait()

    def whole(self, worker: str) -> list[Event]:
        """The events of one complete pass of `worker`."""
        return [
            (worker, sweep, what)
            for sweep in (CAPTURE, INVOCATIONS)
            for what in (LISTING, *self.clients)
        ]


@pytest.fixture
def sweeps(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sweeps]:
    record = Sweeps()

    def clients(self: Worker, event: str) -> list[UUID]:
        record.step((self.worker_id, event, LISTING))
        return list(record.clients)

    monkeypatch.setattr(Worker, "_clients", clients)
    monkeypatch.setattr(
        Worker, "_sweep_client", lambda self, t: record.step((self.worker_id, CAPTURE, t))
    )
    monkeypatch.setattr(
        Worker,
        "_sweep_client_invocations",
        lambda self, t: record.step((self.worker_id, INVOCATIONS, t)),
    )
    yield record
    for gate in record.gates.values():
        gate.release.set()


@pytest.fixture
def worker(settings: Settings) -> Iterator[Callable[..., Worker]]:
    """Workers that are not running: the tests call their passes."""
    made: list[Worker] = []

    def make(worker_id: str) -> Worker:
        w = _worker(settings, worker_id)
        made.append(w)
        return w

    yield make
    for w in made:
        w.db.dispose()


@contextmanager
def sweeping(w: Worker, *gates: Gate) -> Iterator[threading.Thread]:
    """`w._reap()` on a thread, which is joined on leaving (with the gates open)."""
    t = threading.Thread(target=w._reap, daemon=True, name=f"test-{w.worker_id}")
    t.start()
    try:
        yield t
    finally:
        for gate in gates:
            gate.release.set()
        t.join(60)
    assert not t.is_alive(), "the pass did not end"


def _free_after(w: Worker, other: OtherSession) -> None:
    """Another session gets the sweep lock at the moment `_sweeping` is free."""
    assert w._sweeping.acquire(timeout=60), "the sweep did not end"
    try:
        assert other.is_free()
    finally:
        w._sweeping.release()
    assert w._sweep_started is None and w._sweep_progressed is None


def _loud(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["log_level"] not in ("debug", "info")]


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


def _is_lock_connect(kwargs: Mapping[str, Any]) -> bool:
    return "idle_session_timeout" in str(kwargs.get("options"))


def _is_try_lock(query: object) -> bool:
    return "pg_try_advisory_lock(" in str(query)


# --------------------------------------------------------------- one sweep at a time
def test_a_pass_while_another_session_holds_the_lock_visits_no_client(
    worker: Callable[..., Worker], sweeps: Sweeps, other: OtherSession
) -> None:
    w = worker("w-skip")
    assert other.try_lock()
    with structlog.testing.capture_logs() as logs:
        w._reap()
        w._reap()
    assert sweeps.events == [] and _loud(logs) == []
    assert w._sweep_started is None and not w._sweeping.locked()
    other.unlock()
    w._reap()
    assert sweeps.events == sweeps.whole("w-skip")


def test_two_running_workers_never_sweep_at_the_same_time(
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    sweeps: Sweeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = sweeps.hold("w-one")
    one, two = _worker(settings, "w-one"), _worker(settings, "w-two")
    one.reap_every = two.reap_every = 0.2
    passes: list[None] = []  # sweeps of the second worker that ended (skipped ones too)
    sweep = two._sweep

    def counted() -> None:
        sweep()
        passes.append(None)

    monkeypatch.setattr(two, "_sweep", counted)
    running(one)
    assert gate.reached.wait(30), "the first worker's sweep never started"
    running(two)
    _until(lambda: len(passes) >= 3)
    held = [("w-one", CAPTURE, LISTING), ("w-one", CAPTURE, sweeps.clients[0])]
    assert sweeps.events == held
    gate.release.set()
    whole = len(sweeps.whole("w-two"))
    _until(lambda: len(sweeps.events) >= 6 * whole)
    events = list(sweeps.events)
    # each pass is complete before the next one starts, whichever worker's it is
    for at in range(0, len(events), whole):
        part = events[at : at + whole]
        assert part == sweeps.whole(part[0][0])[: len(part)], at
    assert events[:whole] == sweeps.whole("w-one")
    assert "w-two" in {e[0] for e in events}


@pytest.mark.parametrize("held", ["connect", "lock statement"])
def test_a_main_loop_does_not_wait_for_the_sweep_lock(
    held: str,
    world: World,
    owner_db: Database,
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    sweeps: Sweeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = Gate()
    connect, execute = psycopg.connect, psycopg.Connection.execute

    def held_connect(*args: Any, **kwargs: Any) -> Any:
        if _is_lock_connect(kwargs):
            gate.wait()
        return connect(*args, **kwargs)

    def held_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_try_lock(query):
            gate.wait()
        return execute(self, query, *args, **kwargs)

    if held == "connect":
        monkeypatch.setattr(psycopg, "connect", held_connect)
    else:
        monkeypatch.setattr(psycopg.Connection, "execute", held_execute)
    w = _worker(settings, "w-lock-held")
    w.poll, w.reap_every = 2.0, 0.2  # a readiness window (3 polls) of 6 s
    running(w)
    try:
        assert gate.reached.wait(30), "the sweep never came to its lock"
        for _ in range(2):  # a pass on either side of a claim pass
            lost = _queued(owner_db, world.a.tenant, status="leased", lease_owner="w-dead",
                           lease_expires_at=utcnow() - timedelta(seconds=1),
                           attempts=1)  # fmt: skip
            _until(lambda job=lost: _job_state(owner_db, job)[0] == "done")
            assert w.ready()
        assert w._sweeping.locked() and sweeps.events == []
        gate.release.set()
        _until(lambda: len(sweeps.events) >= len(sweeps.whole("w-lock-held")))
    finally:
        gate.release.set()


def test_the_lock_connection_runs_its_own_statements_only_and_is_bounded_and_closed(
    worker: Callable[..., Worker], sweeps: Sweeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[psycopg.Connection, Mapping[str, Any]]] = []
    ran: list[Any] = []  # the lock connection's statements, between the steps
    connect, execute = psycopg.connect, psycopg.Connection.execute

    def recorded_connect(*args: Any, **kwargs: Any) -> Any:
        c = connect(*args, **kwargs)
        if _is_lock_connect(kwargs):
            opened.append((c, kwargs))
        return c

    def recorded_execute(self: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if any(self is c for c, _ in opened):
            ran.append((query, params))
        return execute(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg, "connect", recorded_connect)
    monkeypatch.setattr(psycopg.Connection, "execute", recorded_execute)
    w = worker("w-lock-conn")
    sweep_step = sweeps.step

    def step(event: Event) -> None:
        sweep_step(event)
        ran.append(event[2])

    monkeypatch.setattr(sweeps, "step", step)
    w._reap()
    each = [x for c in sweeps.clients for x in (c, ("SELECT 1", None))]
    assert ran == [
        ("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (SWEEP_LOCK,)),
        LISTING,
        *each,
        LISTING,
        *each,
        ("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (SWEEP_LOCK,)),
    ]
    [(c, kwargs)] = opened
    assert c.closed
    assert kwargs["autocommit"] is True and kwargs["connect_timeout"] == 10
    timeout = w.settings.db_statement_timeout_ms
    assert w.wedged_after == timedelta(seconds=60) and timeout > 0
    assert kwargs["options"].split("-c ")[1:] == [
        "idle_session_timeout=60000 ",
        f"statement_timeout={timeout}",
    ]
    # no statement timeout and a bound below a millisecond: the idle bound stays one
    w.settings = w.settings.model_copy(update={"db_statement_timeout_ms": 0})
    w.wedged_after = timedelta(microseconds=1)
    w._reap()
    assert opened[1][1]["options"] == "-c idle_session_timeout=1"


# --------------------------------------------------------------- released
def test_the_lock_is_free_once_a_sweep_ended(
    worker: Callable[..., Worker], sweeps: Sweeps, other: OtherSession
) -> None:
    w = worker("w-normal")
    gate = sweeps.hold("w-normal", INVOCATIONS, 2)
    with sweeping(w, gate):
        assert gate.reached.wait(30), "the last step never started"
        assert not other.is_free()  # held over both sweeps
        gate.release.set()
        _free_after(w, other)
    assert sweeps.events == sweeps.whole("w-normal")


@pytest.mark.parametrize("sweep", [CAPTURE, INVOCATIONS])
def test_the_lock_is_free_after_a_sweep_that_raised(
    sweep: str, worker: Callable[..., Worker], sweeps: Sweeps, other: OtherSession
) -> None:
    w = worker("w-raised")
    sweeps.raising = ("w-raised", sweep, LISTING)
    with structlog.testing.capture_logs() as logs, sweeping(w):
        _until(lambda: sweeps.raising in sweeps.events)
        _free_after(w, other)
    assert len(_events(logs, "worker.sweep_failed")) == 1 and len(_loud(logs)) == 1
    sweeps.raising = None
    sweeps.events.clear()
    w._reap()
    assert sweeps.events == sweeps.whole("w-raised")


def test_the_lock_is_free_after_a_stop_in_the_middle_of_a_sweep(
    settings: Settings, sweeps: Sweeps, other: OtherSession
) -> None:
    w = _worker(settings, "w-stopped")
    gate = sweeps.hold("w-stopped", client=1)
    exits: list[int] = []
    loop = threading.Thread(target=lambda: exits.append(w.run()), daemon=True, name="test-stopped")
    loop.start()
    try:
        assert gate.reached.wait(30), "the second step never started"
        assert not other.is_free()
        w.request_stop()
        loop.join(1)
        assert loop.is_alive() and not other.is_free()  # the step in progress keeps the lock
        gate.release.set()
        loop.join(60)  # the shutdown keeps `_sweeping`, so the lock was free before `run` returned
        assert not loop.is_alive(), "the worker did not stop"
        assert exits == [0] and w._sweeping.locked() and other.is_free()
    finally:
        gate.release.set()
        w.request_stop()
        loop.join(60)
    assert sweeps.events == sweeps.whole("w-stopped")[:3]


def test_passes_in_a_row_sweep_every_time(
    worker: Callable[..., Worker], sweeps: Sweeps, other: OtherSession
) -> None:
    one, two = worker("w-row-1"), worker("w-row-2")
    expected: list[Event] = []
    for _ in range(10):
        for w in (one, one, two):
            w._reap()
            expected += sweeps.whole(w.worker_id)
            assert sweeps.events == expected
            assert other.is_free()


@pytest.mark.parametrize("failing", ["execute", "close"])
def test_a_lock_that_cannot_be_released_explicitly_does_not_keep_the_sweeps(
    failing: str,
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    other: OtherSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = worker("w-unlock")
    failures: list[None] = []
    execute, close = psycopg.Connection.execute, psycopg.Connection.close
    lock_connections: list[Any] = []

    def failing_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_try_lock(query):
            lock_connections.append(self)
        if failing == "execute" and "pg_advisory_unlock" in str(query):
            failures.append(None)
            raise psycopg.OperationalError("the unlock failed")
        return execute(self, query, *args, **kwargs)

    def failing_close(self: Any) -> None:
        close(self)
        if failing == "close" and self in lock_connections:
            failures.append(None)
            raise psycopg.OperationalError("the close failed")

    with monkeypatch.context() as m:
        m.setattr(psycopg.Connection, "execute", failing_execute)
        m.setattr(psycopg.Connection, "close", failing_close)
        with structlog.testing.capture_logs() as logs:
            w._reap()
    assert len(failures) == 1 and _loud(logs) == []
    assert not w._sweeping.locked() and w._sweep_started is None
    assert sweeps.events == sweeps.whole("w-unlock")
    _until(other.is_free)  # closed: the server frees it


# --------------------------------------------------------------- cannot take it
def test_without_a_lock_connection_nothing_is_swept(
    worker: Callable[..., Worker],
    settings: Settings,
    sweeps: Sweeps,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    w = worker("w-unreachable")
    password = f"ocelot-{uuid7().hex}"
    # the server refuses it; the worker's pool is connected to the real database
    refused = make_url(settings.database_url).set(database="shifdet_absent", password=password)
    w.settings = settings.model_copy(
        update={"database_url": refused.render_as_string(hide_password=False)}
    )
    assert password in w.settings.database_url
    with structlog.testing.capture_logs(processors=[structlog.processors.format_exc_info]) as logs:
        w._reap()
    [failed] = _loud(logs)
    assert failed["event"] == "worker.sweep_failed"
    assert "psycopg.OperationalError" in failed["exception"] and "connect" in failed["exception"]
    assert password not in str(failed)
    assert sweeps.events == [] and not w._sweeping.locked() and w._sweep_started is None
    with rendered_logs(capsys, caplog, settings) as shown:  # as the product renders it
        w._reap()
        rendered = shown()
    assert "worker.sweep_failed" in rendered and "psycopg.OperationalError" in rendered
    assert password not in rendered
    assert sweeps.events == []
    w.settings = settings
    w._reap()
    assert sweeps.events == sweeps.whole("w-unreachable")


def test_a_failing_lock_statement_sweeps_nothing_and_leaves_no_connection(
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    other: OtherSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = worker("w-no-lock")
    execute = psycopg.Connection.execute
    failed_on: list[psycopg.Connection] = []

    def failing(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_try_lock(query):
            failed_on.append(self)
            raise psycopg.OperationalError("the lock statement failed")
        return execute(self, query, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(psycopg.Connection, "execute", failing)
        with structlog.testing.capture_logs() as logs:
            w._reap()
    assert [e["event"] for e in _loud(logs)] == ["worker.sweep_failed"]
    assert sweeps.events == [] and not w._sweeping.locked() and w._sweep_started is None
    assert len(failed_on) == 1 and failed_on[0].closed
    w._reap()
    assert sweeps.events == sweeps.whole("w-no-lock")


def test_a_stop_while_the_lock_is_taken_lists_no_client(
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    other: OtherSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = worker("w-stop-lock")
    execute = psycopg.Connection.execute

    def stopping(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_try_lock(query):
            w.request_stop()
        return execute(self, query, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(psycopg.Connection, "execute", stopping)
        w._reap()
    assert sweeps.events == [] and not w._sweeping.locked()
    assert other.is_free()


# --------------------------------------------------------------- lost
_HOLDER = text(
    "SELECT l.pid FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
    "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
    "AND l.objsubid = 1 AND ((l.classid::bigint << 32) | l.objid::bigint) "
    "= hashtextextended(:k, 0)"
)


@pytest.mark.parametrize("sweep", [CAPTURE, INVOCATIONS])
@pytest.mark.parametrize("how", ["idle", "terminated"])
def test_a_holder_that_lost_the_lock_visits_no_further_client(
    how: str,
    sweep: str,
    worker: Callable[..., Worker],
    worker_admin_url: str,
    sweeps: Sweeps,
    other: OtherSession,
) -> None:
    w = worker("w-lost")
    gate = sweeps.hold("w-lost", sweep)
    if how == "idle":
        w.wedged_after = timedelta(seconds=3)
    with structlog.testing.capture_logs() as logs, sweeping(w, gate):
        assert gate.reached.wait(30), "the step never started"
        if how == "terminated":
            admin = create_engine(worker_admin_url, poolclass=NullPool)
            try:
                with admin.connect() as c:
                    [pid] = c.execute(_HOLDER, {"k": SWEEP_LOCK}).scalars()
                    assert c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid}).scalar()
            finally:
                admin.dispose()
        _until(other.try_lock)
        assert not gate.release.is_set() and w._sweeping.locked()
        gate.release.set()
        assert w._sweeping.acquire(timeout=60), "the sweep did not end"
        w._sweeping.release()
    held = ("w-lost", sweep, sweeps.clients[0])
    whole = sweeps.whole("w-lost")
    assert sweeps.events == whole[: whole.index(held) + 1]
    [lost] = _loud(logs)
    assert lost["event"] == "worker.sweep_lock_lost" and lost["log_level"] == "warning"
    assert lost["worker_id"] == "w-lost" and "exc_info" not in lost
    assert w._sweep_started is None and w._sweep_progressed is None
    other.unlock()
    sweeps.gates.clear()
    sweeps.events.clear()
    w.wedged_after = timedelta(seconds=60)
    w._reap()
    assert sweeps.events == whole


# --------------------------------------------------------------- progress
class Clock:
    """The worker module's monotonic clock, which the tests move."""

    def __init__(self) -> None:
        self.ahead = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.ahead


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    moved = Clock()
    monkeypatch.setattr(worker_mod, "time", SimpleNamespace(monotonic=moved.monotonic))
    return moved


def _register(w: Worker) -> None:
    with w.db.session() as s:
        worker_mod.repo.upsert_worker(s, w.worker_id, "host", 1, "0", 1)
        s.commit()


def _last_reap(owner_db: Database, w: Worker) -> Any:  # Any: timestamptz
    with owner_db.session() as o:
        return o.execute(
            text("SELECT last_reap_at FROM shifde.workers WHERE id = :w"), {"w": w.worker_id}
        ).scalar_one()


def _due_pass(owner_db: Database, w: Worker) -> tuple[bool, list[Mapping[str, Any]]]:
    """A due pass while the sweep is in progress: was it recorded as `last_reap_at`, and the
    `worker.sweep_still_running` entries."""
    before = _last_reap(owner_db, w)
    with structlog.testing.capture_logs() as logs:
        w._reap()
    return _last_reap(owner_db, w) != before, _still_running(logs)


@pytest.mark.parametrize("first", ["done", "failed"])
@pytest.mark.parametrize("sweep", [CAPTURE, INVOCATIONS])
def test_a_long_sweep_that_progresses_is_not_overdue(
    sweep: str,
    first: str,
    owner_db: Database,
    worker: Callable[..., Worker],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real steps, held (and failing) inside their transactions."""
    clients = [uuid7() for _ in range(3)]
    gates = {c: Gate() for c in clients}
    visited: list[UUID] = []

    def held(tenant_id: UUID) -> None:
        visited.append(tenant_id)
        gates[tenant_id].wait()
        if first == "failed" and tenant_id == clients[0]:
            raise RuntimeError("the step failed")

    def capture_full(s: Session, tenant_id: UUID, settings: Settings) -> bool:
        if sweep == CAPTURE:
            held(tenant_id)
        return True  # nothing to scrub

    def settle(s: Session, tenant_id: UUID) -> int:
        if sweep == INVOCATIONS:
            held(tenant_id)
        return 0

    monkeypatch.setattr(Worker, "_clients", lambda self, event: list(clients))
    monkeypatch.setattr(worker_mod, "capture_full", capture_full)
    monkeypatch.setattr(ledger.reservations, "settle_orphaned_invocations", settle)
    w = worker("w-progress")
    _register(w)
    assert w.wedged_after == timedelta(seconds=60)
    with structlog.testing.capture_logs() as logs, sweeping(w, *gates.values()):
        assert gates[clients[0]].reached.wait(30), "the sweep never started"
        for done, waiting in zip(clients, clients[1:], strict=False):
            clock.ahead += 50
            gates[done].release.set()
            assert gates[waiting].reached.wait(30), "the next step never started"
        # 100 s into the sweep, 50 s of them before the last client finished
        assert w._sweep_started is not None and clock.monotonic() - w._sweep_started > 100
        assert w._sweep_overdue() is None
        assert _due_pass(owner_db, w) == (True, [])
        clock.ahead += 50  # 50 s since the last client finished, 150 s in all
        assert w._sweep_overdue() is None
        assert _due_pass(owner_db, w) == (True, [])
        assert visited == clients and not gates[clients[2]].release.is_set()
    # `sweep` is also the event of a failed step of that sweep; the passes captured their own logs
    assert [e["event"] for e in _loud(logs)] == ([sweep] if first == "failed" else [])
    assert w._sweep_started is None and w._sweep_progressed is None


@pytest.mark.parametrize("sweep", [CAPTURE, INVOCATIONS])
def test_a_sweep_is_overdue_once_no_client_finished_for_the_bound(
    sweep: str,
    owner_db: Database,
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    clock: Clock,
) -> None:
    w = worker("w-stalled")
    _register(w)
    bound = w.wedged_after.total_seconds()
    gates = [sweeps.hold("w-stalled", sweep, n) for n in range(3)]
    with sweeping(w, *gates):
        assert gates[0].reached.wait(30), "the sweep never started"
        clock.ahead += bound - 10
        assert _due_pass(owner_db, w) == (True, [])
        gates[0].release.set()
        assert gates[1].reached.wait(30), "the second step never started"
        assert _due_pass(owner_db, w) == (True, [])
        stalled_at = clock.ahead
        clock.ahead += bound + 10
        for _ in range(2):  # each due pass
            recorded, overdue = _due_pass(owner_db, w)
            assert not recorded and len(overdue) == 1
            assert overdue[0]["worker_id"] == "w-stalled"
            # from the end of the first client, not from the start of the sweep (bound - 10 more)
            since = clock.ahead - stalled_at
            assert bound < since <= overdue[0]["elapsed_seconds"] < since + bound - 10
        gates[1].release.set()
        assert gates[2].reached.wait(30), "the third step never started"
        assert _due_pass(owner_db, w) == (True, [])
        assert w._sweeping.locked() and not gates[2].release.is_set()
    assert sweeps.events == sweeps.whole("w-stalled")


# --------------------------------------------------------------- lease reaping
@pytest.mark.parametrize("by", ["w-holder", "w-other"])
def test_leases_are_reaped_while_a_sweep_holds_its_lock(
    by: str,
    world: World,
    owner_db: Database,
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    other: OtherSession,
) -> None:
    gate = sweeps.hold("w-holder")
    holder = _worker(settings, "w-holder")  # reaps once an hour
    if by == "w-holder":
        holder.reap_every = 0.2
    running(holder)
    try:
        assert gate.reached.wait(30), "the sweep never started"
        lost = _queued(owner_db, world.a.tenant, status="leased", lease_owner="w-dead",
                       lease_expires_at=utcnow() - timedelta(seconds=1), attempts=1)  # fmt: skip
        if by == "w-other":
            worker("w-other")._reap()
            assert _job_state(owner_db, lost)[0] == "done"
        else:
            _until(lambda: _job_state(owner_db, lost)[0] == "done")
        _until(lambda: other.is_free(REAPER_LOCK))
        assert not other.is_free()
        assert sweeps.events == sweeps.whole("w-holder")[:2] and not gate.release.is_set()
    finally:
        gate.release.set()
