"""Fixtures of the full-app tests (`tests/_support/fullapp.py`).

SETH's tests assume a database holding only the seeded demo world (an exact organization list, an
exact member count). Clients cannot be truncated away between tests, so every test here gets its
own database cloned from the run's migrated template (DECISIONS B-2234); the root fixtures
`test_database`, `app_url`, `owner_url`, `owner_engine` and `app_engine` are overridden per test.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.fullapp import World, fresh_world, full_client
from tests._support.servers import mcp_server, oai_server, rest_server
from tests.conftest import (
    DB_PREFIX,
    _create_database,
    _drop_database,
    _ensure_template,
    _env,
    _lock_path,
    _with_db,
)


class _Dropper:
    """Drops each finished test's database in the background, so the next test need not wait.

    `DROP DATABASE` waits for a checkpoint. Dropped in the test's own teardown, eight workers'
    drops queued behind one another for seconds to minutes (B-2330). Here the test returns at
    once; the drop still happens straight away (which also spares later checkpoints from syncing
    that database's files), and the worker waits for every drop before it ends.
    """

    def __init__(self, admin_url: str) -> None:
        self._admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_size=2)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="drop-test-db")
        self._pending: list[Future[None]] = []

    def drop(self, name: str) -> None:
        self._pending.append(self._pool.submit(self._drop, name))

    def _drop(self, name: str) -> None:
        with contextlib.suppress(Exception), self._admin.connect() as c:
            _drop_database(c, name)

    def close(self) -> None:
        try:
            for done in self._pending:
                done.result()
            self._pool.shutdown()
        finally:
            self._admin.dispose()


@pytest.fixture(scope="session")
def _dropper(admin_url: str) -> Iterator[_Dropper]:
    dropper = _Dropper(admin_url)
    yield dropper
    dropper.close()


@pytest.fixture(scope="session")
def _admin_engine(admin_url: str) -> Iterator[Engine]:
    """One pooled superuser engine per worker: opening a connection costs about 0.3 s on this
    machine, and every test used to open (and discard) two."""
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_size=2, max_overflow=2)
    yield engine
    engine.dispose()


_READY_TEMPLATES: set[str] = set()  # templates this worker has seen finished (they never change)


@pytest.fixture
def test_database(
    run_id: str, _admin_engine: Engine, _dropper: _Dropper
) -> Iterator[tuple[str, str]]:
    """(app_url, owner_url) of a database cloned from the run's template for this test alone;
    dropped in the background once the test ends (`_Dropper`)."""
    app_url = _env("SHIFDE_TEST_DATABASE_URL")
    owner_url = _env("SHIFDE_TEST_MIGRATION_DATABASE_URL")
    app_role = make_url(app_url).username or "shifde_app"
    owner_role = make_url(owner_url).username or "shifde_owner"
    name = f"{DB_PREFIX}{run_id}_a{uuid.uuid4().hex[:10]}"
    admin = _admin_engine
    try:
        if run_id not in _READY_TEMPLATES:
            with FileLock(str(_lock_path(run_id))):  # only the one-time template build is serial
                template = _ensure_template(admin, run_id, owner_url, app_role)
            _READY_TEMPLATES.add(run_id)
        else:
            template = f"{DB_PREFIX}{run_id}_tmpl"
        with admin.connect() as c:  # clones of a finished template can run side by side
            _create_database(c, name, owner_role, app_role, template)
        yield _with_db(app_url, name), _with_db(owner_url, name)
    finally:
        _dropper.drop(name)


@pytest.fixture
def app_url(test_database: tuple[str, str]) -> str:
    return test_database[0]


@pytest.fixture
def owner_url(test_database: tuple[str, str]) -> str:
    return test_database[1]


@pytest.fixture
def owner_engine(owner_url: str) -> Iterator[Engine]:
    engine = create_engine(owner_url, pool_size=2, max_overflow=2)
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(app_url: str) -> Iterator[Engine]:
    engine = create_engine(app_url, pool_size=2, max_overflow=2)
    yield engine
    engine.dispose()


@pytest.fixture
def owner_db(owner_url: str) -> Iterator[Database]:
    db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, deliberately
    yield db
    db.dispose()


@pytest.fixture
def app_db(app_url: str) -> Iterator[Database]:
    db = Database(app_url, pool_size=1, max_overflow=0)
    yield db
    db.dispose()


@pytest.fixture
def owner_session(owner_engine: Engine) -> Iterator[Session]:
    with Session(owner_engine) as s:
        yield s


@pytest.fixture
def world(owner_session: Session) -> World:
    return fresh_world(owner_session)


@pytest.fixture
def client(settings: Settings, world: World) -> Iterator[TestClient]:
    with full_client(settings) as c:
        yield c


__all__ = [
    "app_db",
    "app_engine",
    "app_url",
    "client",
    "mcp_server",
    "oai_server",
    "owner_db",
    "owner_engine",
    "owner_session",
    "owner_url",
    "rest_server",
    "test_database",
    "world",
]
