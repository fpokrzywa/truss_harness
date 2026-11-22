"""`shifde-worker` process control (SP4 design 6.2): the optional `/healthz` + `/readyz` port, the
bounded shutdown grace and exit 4 when the database schema is not at the code's head.

No socket is opened with the default settings (port 0). With a port, `/healthz` follows the
heartbeat loop's ticks (two intervals) and `/readyz` the main loop's claim ticks (three polls)
and the worker's own database; a stop request makes readiness 503 and any other path is 404. A
job blocked past `worker_shutdown_grace_seconds` does not hold `Worker.run()` up. The predicates
are driven through recorded tick times rather than real waits.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
import structlog
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from shifde.adapters.runtime import IntegrationRuntime
from shifde.connections import service as connections
from shifde.core import settings as settings_mod
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config, code_heads
from shifde.core.settings import Settings
from shifde.runtime import worker as worker_mod
from shifde.runtime.governance import PolicyBudgetHooks
from shifde.runtime.worker import Worker
from tests.build.service_support import REGISTRY
from tests.conftest import DB_PREFIX, _create_database, _drop_database, _with_db
from tests.runtime.conftest import insert
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
