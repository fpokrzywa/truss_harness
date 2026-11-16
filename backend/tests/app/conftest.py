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
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

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

DROP_THREADS = 8  # per worker; the drops of all workers overlap at the end of the run


class _Dropper:
    """Collects the per-test databases a worker has finished with and drops them all at the end.

    `DROP DATABASE` forces an immediate checkpoint. Run one per test, with eight busy workers,
    those checkpoints queued behind one another and starved every other test of disk time (single
    teardowns waited minutes; B-2330). Dropped together after the last test, concurrently so that
    waiting drops share checkpoints, they cost a fraction. Every test still gets a database of its
    own for its whole run, and none outlives it by more than the worker's session.
    """

    def __init__(self, admin_url: str) -> None:
        self._names: list[str] = []
        self._admin_url = admin_url

    def drop(self, name: str) -> None:
        self._names.append(name)

    def close(self) -> None:
        admin = create_engine(
            self._admin_url,
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )

        def one(name: str) -> None:
            with contextlib.suppress(Exception), admin.connect() as c:
                _drop_database(c, name)

        try:
            with ThreadPoolExecutor(max_workers=DROP_THREADS) as pool:
                list(pool.map(one, self._names))
        finally:
            admin.dispose()


@pytest.fixture(scope="session")
def _dropper(admin_url: str) -> Iterator[_Dropper]:
    dropper = _Dropper(admin_url)
    yield dropper
    dropper.close()


@pytest.fixture
def test_database(run_id: str, admin_url: str, _dropper: _Dropper) -> Iterator[tuple[str, str]]:
    """(app_url, owner_url) of a database cloned from the run's template for this test alone."""
    app_url = _env("SHIFDE_TEST_DATABASE_URL")
    owner_url = _env("SHIFDE_TEST_MIGRATION_DATABASE_URL")
    app_role = make_url(app_url).username or "shifde_app"
    owner_role = make_url(owner_url).username or "shifde_owner"
    name = f"{DB_PREFIX}{run_id}_a{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with FileLock(str(_lock_path(run_id))):  # only the one-time template build is serial
            template = _ensure_template(admin, run_id, owner_url, app_role)
        with admin.connect() as c:  # clones of a finished template can run side by side
            _create_database(c, name, owner_role, app_role, template)
        yield _with_db(app_url, name), _with_db(owner_url, name)
    finally:
        _dropper.drop(name)
        admin.dispose()


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
