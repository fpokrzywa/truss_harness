"""The worker's client sweeps with several workers and many clients (issue #415).

One sweep at a time across workers: a sweep holds the session-level advisory lock `SWEEP_LOCK` on a
connection of its own; a worker that does not get it skips the sweep, and one that cannot try it
sweeps nothing. The lock is free as soon as the sweep ended, also after an exception or a stop, and
a holder whose step is stuck (or whose session ended) loses it and visits no further client in that
pass. The main loop and lease reaping wait for none of it.

A worker that lost the lock goes on after the client it lost it at (B-4166): its next run of that
sweep starts after that client and its next pass with the other sweep, so a client whose step is
slower than the bound on every pass keeps no other client from being swept, with any number of
workers.

Stuck is judged by progress: the bound (`wedged_after`) counts from the last finished client, in
either sweep, not from the start of the sweep.

The locks are real: the second session is the test's own connection, or another `Worker` of the same
database. Time passes by moving the worker module's clock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
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
from shifde.runtime.worker import CAPTURE_SWEEP_LOCK, REAPER_LOCK, SWEEP_LOCK, Worker
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


_HOLDERS = (
    "SELECT l.pid FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
    "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
    "AND l.objsubid = 1 AND ((l.classid::bigint << 32) | l.objid::bigint) "
    "= hashtextextended(%s, 0)"
)


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

    def holders(self) -> list[int]:
        """The backends that hold `SWEEP_LOCK`."""
        return [int(pid) for (pid,) in self.connection.execute(_HOLDERS, (SWEEP_LOCK,))]


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

    def part(self, worker: str, sweep: str, *clients: int) -> list[Event]:
        """The events of one sweep of `worker` over those clients, in that order."""
        return [(worker, sweep, w) for w in (LISTING, *(self.clients[n] for n in clients))]

    def resumed(self, worker: str, lost: str, *clients: int) -> list[Event]:
        """The events of the pass after one that was cut short in `lost`: the other sweep whole,
        then `lost` over those clients."""
        first = INVOCATIONS if lost == CAPTURE else CAPTURE
        return [
            *self.part(worker, first, *range(len(self.clients))),
            *self.part(worker, lost, *clients),
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
    assert lost["tenant_id"] == str(sweeps.clients[0])
    assert w._sweep_started is None and w._sweep_progressed is None
    other.unlock()
    sweeps.gates.clear()
    sweeps.events.clear()
    w.wedged_after = timedelta(seconds=60)
    w._reap()
    assert sweeps.events == sweeps.resumed("w-lost", sweep, 1, 2, 0)
    sweeps.events.clear()
    w._reap()
    assert sweeps.events == whole


# --------------------------------------------------------------- a client that is slow every time
SLOW_BOUND = 2.0  # the workers' `wedged_after`
SLOW_STEP = 3.0  # longer by several reap intervals
SLOW_REAP = 0.3
BOTH = (CAPTURE, INVOCATIONS)


@dataclass
class Visit:
    worker: str
    sweep: str
    client: UUID
    began: float
    has_lock: bool  # the sweep's own lock session held the lock when the step began
    ended: float | None = None


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    ("slow_in", "slow", "outcome"),
    [
        ((CAPTURE,), 0, "failed"),
        ((CAPTURE,), 2, "done"),
        ((INVOCATIONS,), 0, "done"),
        ((INVOCATIONS,), 1, "failed"),
        (BOTH, 0, "failed"),
        (BOTH, 2, "done"),
    ],
)
def test_a_client_that_is_slow_on_every_pass_stops_no_other_client(
    slow_in: tuple[str, ...],
    slow: int,
    outcome: str,
    workers: int,
    settings: Settings,
    running: Callable[[Worker], threading.Thread],
    other: OtherSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real steps and the server's own timers: the slow client's query outlasts the idle bound
    each time, and is cut by the statement timeout ("failed") or returns ("done"). In the capture
    sweep it comes before the per-client lock, which else lets another worker step past the client
    while the query runs: here it is slow for both."""
    clients = [uuid7() for _ in range(3)]
    visits: list[Visit] = []
    lock_connections: dict[int, psycopg.Connection] = {}  # by sweep thread
    querying = threading.Lock()  # `other` is shared by the sweep threads
    connect = psycopg.connect
    capture_step, invocation_step = Worker._sweep_client, Worker._sweep_client_invocations
    set_tenant = worker_mod.db.set_tenant
    in_capture_step = threading.local()

    def recorded_connect(*args: Any, **kwargs: Any) -> Any:
        c = connect(*args, **kwargs)
        if _is_lock_connect(kwargs):
            lock_connections[threading.get_ident()] = c
        return c

    def visited(w: Worker, sweep: str, tenant_id: UUID, step: Callable[[], None]) -> None:
        own = lock_connections[threading.get_ident()].info.backend_pid
        with querying:
            holders = other.holders()
        visit = Visit(w.worker_id, sweep, tenant_id, time.monotonic(), holders == [own])
        visits.append(visit)
        step()
        visit.ended = time.monotonic()

    def query(s: Session, sweep: str, tenant_id: UUID) -> None:
        if sweep in slow_in and tenant_id == clients[slow]:
            s.execute(text("SELECT pg_sleep(:s)"), {"s": 60 if outcome == "failed" else SLOW_STEP})

    def capture(w: Worker, tenant_id: UUID) -> None:
        in_capture_step.now = True
        try:
            capture_step(w, tenant_id)
        finally:
            in_capture_step.now = False

    def set_tenant_then_query(s: Session, tenant_id: UUID) -> None:
        set_tenant(s, tenant_id)
        if getattr(in_capture_step, "now", False):
            query(s, CAPTURE, tenant_id)

    def settle(s: Session, tenant_id: UUID) -> int:
        query(s, INVOCATIONS, tenant_id)
        return 0

    monkeypatch.setattr(psycopg, "connect", recorded_connect)
    monkeypatch.setattr(Worker, "_clients", lambda self, event: list(clients))
    monkeypatch.setattr(
        Worker,
        "_sweep_client",
        lambda self, t: visited(self, CAPTURE, t, lambda: capture(self, t)),
    )
    monkeypatch.setattr(
        Worker,
        "_sweep_client_invocations",
        lambda self, t: visited(self, INVOCATIONS, t, lambda: invocation_step(self, t)),
    )
    monkeypatch.setattr(worker_mod.db, "set_tenant", set_tenant_then_query)
    monkeypatch.setattr(ledger.reservations, "settle_orphaned_invocations", settle)
    cut = {"db_statement_timeout_ms": int(SLOW_STEP * 1000)} if outcome == "failed" else {}
    names = [f"w-slow-{n}" for n in range(workers)]
    targets = {(sweep, c) for sweep in BOTH for c in clients}

    def is_slow(v: Visit) -> bool:
        return v.sweep in slow_in and v.client == clients[slow]

    def reached_twice() -> bool:
        seen = [(v.sweep, v.client) for v in list(visits)]
        losers = {e["worker_id"] for e in _events(list(logs), "worker.sweep_lock_lost")}
        return all(seen.count(t) >= 2 for t in targets) and losers == set(names)

    with structlog.testing.capture_logs() as logs:
        for name in names:
            w = _worker(settings, name, **cut)
            w.reap_every = SLOW_REAP
            w.wedged_after = timedelta(seconds=SLOW_BOUND)
            running(w)
        with suppress(AssertionError):  # what was not reached is named below
            _until(reached_twice, 60)
        done = list(visits)
    seen = [(v.sweep, v.client) for v in done]
    assert {t for t in targets if seen.count(t) < 2} == set()
    # in bounded time: one worker has lost three passes before it has visited every client
    first_slow = max(seen.index(t) for t in targets)
    assert sum(is_slow(v) for v in done[:first_slow]) <= 4 * workers
    # slow every time, and the lock was lost each time, at that client only
    ended = [v.ended - v.began for v in done if is_slow(v) and v.ended is not None]
    assert len(ended) >= 2 and min(ended) > SLOW_BOUND
    lost = _events(logs, "worker.sweep_lock_lost")
    assert {e["worker_id"] for e in lost} == set(names)
    assert {e["tenant_id"] for e in lost} == {str(clients[slow])}
    failed = {e["tenant_id"] for sweep in BOTH for e in _events(logs, sweep)}
    assert failed == ({str(clients[slow])} if outcome == "failed" else set())
    assert _events(logs, "worker.sweep_failed") == []
    # no step begins without the lock, so no two sweeps go on at a time
    assert [v for v in done if not v.has_lock] == []
    if workers == 2:  # the other worker swept while a step that had lost the lock went on
        assert any(
            a.ended is not None and a.worker != b.worker and a.began < b.began < a.ended
            for a in done
            for b in done
        )


@pytest.mark.parametrize("sweep", BOTH)
def test_two_workers_that_lose_the_lock_to_each_other_both_go_on_after_the_slow_client(
    sweep: str, worker: Callable[..., Worker], sweeps: Sweeps, other: OtherSession
) -> None:
    """The alternation: A loses the lock at the first client, B takes it and starts there too, A's
    next try fails, and B loses it in its turn."""
    a, b = worker("w-a"), worker("w-b")
    a.wedged_after = b.wedged_after = timedelta(seconds=3)
    gate_a, gate_b = sweeps.hold("w-a", sweep), sweeps.hold("w-b", sweep)
    with structlog.testing.capture_logs() as logs, sweeping(a, gate_a):
        assert gate_a.reached.wait(30), "A's step never started"
        _until(other.is_free)
        with sweeping(b, gate_b):
            assert gate_b.reached.wait(30), "B's step never started"
            gate_a.release.set()
            assert a._sweeping.acquire(timeout=60), "A's sweep did not end"
            a._sweeping.release()
            sweeps.events.clear()
            a._reap()
            assert sweeps.events == []  # B has the lock
            _until(other.is_free)
            a._reap()
            assert sweeps.events == sweeps.resumed("w-a", sweep, 1, 2, 0)
            sweeps.events.clear()
        b._reap()
    assert sweeps.events == sweeps.resumed("w-b", sweep, 1, 2, 0)
    lost = [(e["worker_id"], e["tenant_id"]) for e in _loud(logs)]
    assert lost == [("w-a", str(sweeps.clients[0])), ("w-b", str(sweeps.clients[0]))]


# --------------------------------------------------------------- where a lost sweep goes on
LoseAt = Callable[[Worker, str, int], None]


@pytest.fixture
def lose_at(
    sweeps: Sweeps,
    other: OtherSession,
    worker_admin_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LoseAt]:
    """A pass whose lock session the server ends during the step of one client."""
    admin = create_engine(worker_admin_url, poolclass=NullPool)
    step = sweeps.step
    losing: list[Event] = []

    def losing_step(event: Event) -> None:
        step(event)
        if losing == [event]:
            with admin.connect() as c:
                [pid] = other.holders()
                assert c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid}).scalar()
            _until(other.is_free)

    monkeypatch.setattr(sweeps, "step", losing_step)

    def lose(w: Worker, sweep: str, client: int) -> None:
        losing[:] = [(w.worker_id, sweep, sweeps.clients[client])]
        sweeps.events.clear()
        with structlog.testing.capture_logs() as logs:
            w._reap()
        assert [(e["event"], e.get("tenant_id")) for e in _loud(logs)] == [
            ("worker.sweep_lock_lost", str(sweeps.clients[client]))
        ]
        assert sweeps.events[-1] == losing[0]
        losing.clear()

    yield lose
    admin.dispose()


def _pass(w: Worker, sweeps: Sweeps) -> list[Event]:
    """The events of one more pass, which logs nothing loud."""
    sweeps.events.clear()
    with structlog.testing.capture_logs() as logs:
        w._reap()
    assert _loud(logs) == []
    return list(sweeps.events)


@pytest.mark.parametrize("sweep", BOTH)
@pytest.mark.parametrize(("at", "order"), [(0, (1, 2, 0)), (1, (2, 0, 1)), (2, (0, 1, 2))])
def test_the_next_pass_goes_on_after_the_client_and_a_whole_pass_starts_over(
    at: int,
    order: tuple[int, ...],
    sweep: str,
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    lose_at: LoseAt,
) -> None:
    w = worker("w-resume")
    lose_at(w, sweep, at)
    assert _pass(w, sweeps) == sweeps.resumed("w-resume", sweep, *order)
    assert _pass(w, sweeps) == sweeps.whole("w-resume")


def test_each_sweep_has_its_own_place_and_neither_keeps_the_other_from_its_turn(
    worker: Callable[..., Worker], sweeps: Sweeps, lose_at: LoseAt
) -> None:
    w = worker("w-places")
    lose_at(w, CAPTURE, 0)
    lose_at(w, INVOCATIONS, 1)  # it went first
    assert sweeps.events == sweeps.part("w-places", INVOCATIONS, 0, 1)
    lose_at(w, CAPTURE, 1)  # first again, after the first client
    assert sweeps.events == sweeps.part("w-places", CAPTURE, 1)
    assert _pass(w, sweeps) == [
        *sweeps.part("w-places", INVOCATIONS, 2, 0, 1),
        *sweeps.part("w-places", CAPTURE, 2, 0, 1),
    ]
    assert _pass(w, sweeps) == sweeps.whole("w-places")


def test_a_sweep_that_visited_every_client_starts_over_although_the_other_was_lost(
    worker: Callable[..., Worker], sweeps: Sweeps, lose_at: LoseAt
) -> None:
    w = worker("w-over")
    lose_at(w, CAPTURE, 0)
    lose_at(w, INVOCATIONS, 1)
    lose_at(w, INVOCATIONS, 2)  # after the capture sweep, which went on to its end
    assert sweeps.events == [
        *sweeps.part("w-over", CAPTURE, 1, 2, 0),
        *sweeps.part("w-over", INVOCATIONS, 2),
    ]
    assert _pass(w, sweeps) == sweeps.whole("w-over")


@pytest.mark.parametrize("sweep", BOTH)
@pytest.mark.parametrize(("at", "order"), [(0, (0, 1)), (1, (1, 0)), (2, (0, 1))])
def test_the_place_of_a_client_that_is_gone_stays(
    at: int,
    order: tuple[int, ...],
    sweep: str,
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    lose_at: LoseAt,
) -> None:
    w = worker("w-gone")
    lose_at(w, sweep, at)
    del sweeps.clients[at]
    assert _pass(w, sweeps) == sweeps.resumed("w-gone", sweep, *order)
    assert _pass(w, sweeps) == sweeps.whole("w-gone")


def test_without_any_client_left_the_place_is_given_up(
    worker: Callable[..., Worker], sweeps: Sweeps, lose_at: LoseAt
) -> None:
    w = worker("w-none-left")
    clients = list(sweeps.clients)
    lose_at(w, CAPTURE, 1)
    sweeps.clients.clear()
    assert _pass(w, sweeps) == sweeps.resumed("w-none-left", CAPTURE)
    sweeps.clients[:] = clients
    assert _pass(w, sweeps) == sweeps.whole("w-none-left")


@pytest.mark.parametrize("sweep", BOTH)
def test_clients_added_and_removed_between_the_passes(
    sweep: str, worker: Callable[..., Worker], sweeps: Sweeps, lose_at: LoseAt
) -> None:
    w = worker("w-changed")
    first, second, third = sweeps.clients
    lose_at(w, sweep, 1)
    before, between, behind = (UUID(int=n) for n in (first.int - 1, second.int + 1, third.int + 1))
    sweeps.clients[:] = [before, second, between, third, behind]  # the first one is gone
    assert _pass(w, sweeps) == sweeps.resumed("w-changed", sweep, 2, 3, 4, 0, 1)
    assert _pass(w, sweeps) == sweeps.whole("w-changed")


def test_a_restarted_worker_and_another_worker_start_at_the_first_client(
    worker: Callable[..., Worker], sweeps: Sweeps, lose_at: LoseAt
) -> None:
    w = worker("w-again")
    lose_at(w, CAPTURE, 1)
    assert _pass(worker("w-other"), sweeps) == sweeps.whole("w-other")
    assert _pass(worker("w-again"), sweeps) == sweeps.whole("w-again")
    assert _pass(w, sweeps) == sweeps.resumed("w-again", CAPTURE, 2, 0, 1)


def test_a_pass_that_lists_no_client_keeps_the_place(
    worker: Callable[..., Worker],
    sweeps: Sweeps,
    lose_at: LoseAt,
    other: OtherSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = worker("w-keeps")
    lose_at(w, CAPTURE, 0)
    assert other.try_lock()
    assert _pass(w, sweeps) == []
    other.unlock()
    with monkeypatch.context() as m:  # both listings fail, so the pass is not cut short
        m.setattr(Worker, "_clients", lambda self, event: None)
        assert _pass(w, sweeps) == []
    assert _pass(w, sweeps) == [
        *sweeps.part("w-keeps", CAPTURE, 1, 2, 0),
        *sweeps.part("w-keeps", INVOCATIONS, 0, 1, 2),
    ]
    assert _pass(w, sweeps) == sweeps.whole("w-keeps")


@pytest.mark.parametrize("sweep", BOTH)
def test_an_exception_that_escapes_a_step_on_every_pass_stops_no_other_client(
    sweep: str, worker: Callable[..., Worker], sweeps: Sweeps
) -> None:
    w = worker("w-escaped")
    sweeps.raising = ("w-escaped", sweep, sweeps.clients[0])
    seen: set[Event] = set()
    for _ in range(2):
        sweeps.events.clear()
        with structlog.testing.capture_logs() as logs:
            w._reap()
        assert [e["event"] for e in _loud(logs)] == ["worker.sweep_failed"]
        seen |= set(sweeps.events)
    assert seen == set(sweeps.whole("w-escaped"))
    sweeps.raising = None
    assert _pass(w, sweeps) == sweeps.resumed("w-escaped", sweep, 1, 2, 0)


def test_the_clients_are_listed_in_the_order_the_place_is_kept_in(
    world: World, worker: Callable[..., Worker]
) -> None:
    listed = worker("w-order")._clients(CAPTURE)
    assert listed is not None and {world.a.tenant, world.b.tenant} <= set(listed)
    assert listed == sorted(listed)


def test_a_client_whose_capture_lock_is_taken_is_skipped_alone(
    worker: Callable[..., Worker], other: OtherSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    swept: list[UUID] = []

    def capture_full(s: Session, tenant_id: UUID, settings: Settings) -> bool:
        swept.append(tenant_id)
        return True

    monkeypatch.setattr(worker_mod, "capture_full", capture_full)
    w = worker("w-per-client")
    held, free = uuid7(), uuid7()
    assert other.try_lock(CAPTURE_SWEEP_LOCK.format(held))
    w._sweep_client(held)
    w._sweep_client(free)
    assert swept == [free]


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
