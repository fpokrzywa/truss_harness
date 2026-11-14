"""Test fixtures: one migrated template database per run, one database per xdist worker.

Design (DECISIONS B-006, ported from SETH 3ba0e06 `tests/conftest.py`):

* The first worker that needs a database takes a file lock, creates `shifdet_<run>_tmpl` through
  the superuser URL (`SHIFDE_TEST_ADMIN_DATABASE_URL`), owned by `shifde_owner`, runs
  `alembic upgrade heads` on it as `shifde_owner`, and marks it a template that accepts no
  connections. Workers that come later find it ready.
* Each worker then gets `shifdet_<run>_<worker>` with `CREATE DATABASE ... TEMPLATE`, dropped when
  the worker finishes. The controller drops the template and any leftover database of the run.
* Credentials, host and port come from `SHIFDE_TEST_*_DATABASE_URL` (environment first, then
  `.devdata/dev.env`); their database names are never used. Tests never touch the hosted database.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from filelock import FileLock
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from shifde.app import create_app
from shifde.core.db import Database
from shifde.core.migrations import alembic_config
from shifde.core.settings import Settings

DEV_ENV = Path(__file__).resolve().parents[2] / ".devdata" / "dev.env"
ORIGIN = "http://127.0.0.1:3710"
DB_PREFIX = "shifdet_"
_RUN_KEY = "shifde_test_run"

# Tables wiped (as owner, so row-level security does not apply) before every test that uses the
# database. Module ports append their tenant-data tables here; seed catalogs stay.
CLEAN_TABLES: list[str] = [
    # authz (tenant-owned rows; custom roles stay: tests key them to fresh tenant ids)
    "role_bindings",
    "group_members",
    "groups",
    "policy_decisions",
]


# --------------------------------------------------------------------------- run identity
_RUN_STASH = pytest.StashKey[str]()


def pytest_configure(config: pytest.Config) -> None:
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        config.stash[_RUN_STASH] = str(workerinput[_RUN_KEY])
    else:
        config.stash[_RUN_STASH] = uuid.uuid4().hex[:12]


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:  # node: xdist WorkerController
    """xdist controller: hand the run id to every worker."""
    node.workerinput[_RUN_KEY] = node.config.stash[_RUN_STASH]


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Controller (or a run without xdist): drop the template and anything the run left over."""
    if getattr(session.config, "workerinput", None) is not None:
        return
    run = session.config.stash.get(_RUN_STASH, None)
    if run is not None:
        with contextlib.suppress(OSError):
            _lock_path(run).unlink(missing_ok=True)
    admin_url = _env_or_none("SHIFDE_TEST_ADMIN_DATABASE_URL")
    if run is None or admin_url is None:
        return
    with contextlib.suppress(Exception):  # best effort; a leftover database is harmless
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            with admin.connect() as c:
                names = c.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                    {"p": f"{DB_PREFIX}{run}\\_%"},
                ).scalars()
                for name in list(names):
                    _drop_database(c, name)
        finally:
            admin.dispose()


# --------------------------------------------------------------------------- helpers
def _lock_path(run: str) -> Path:
    return Path(tempfile.gettempdir()) / f"shifde-test-{run}.lock"


def _env_or_none(key: str) -> str | None:
    value = os.environ.get(key)
    if not value and DEV_ENV.exists():
        value = dotenv_values(DEV_ENV, encoding="utf-8-sig").get(key)
    return value or None


def _env(key: str) -> str:
    value = _env_or_none(key)
    if not value:
        pytest.exit(
            f"{key} is not set (start the local database: python scripts/dev_db.py start)",
            returncode=2,
        )
    return value


def _with_db(url: str, name: str) -> str:
    return make_url(url).set(database=name).render_as_string(hide_password=False)


def _quote(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError(f"unexpected database name {name!r}")
    return f'"{name}"'


def _drop_database(c: Connection, name: str) -> None:
    """Drop `name` (connection in AUTOCOMMIT); a template must be unmarked first."""
    c.execute(text(f"ALTER DATABASE {_quote(name)} WITH IS_TEMPLATE false"))
    c.execute(text(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)"))


def _create_database(c: Connection, name: str, owner: str, app_role: str, template: str) -> None:
    c.execute(
        text(f"CREATE DATABASE {_quote(name)} OWNER {_quote(owner)} TEMPLATE {_quote(template)}")
    )
    c.execute(text(f"REVOKE ALL ON DATABASE {_quote(name)} FROM PUBLIC"))
    c.execute(text(f"GRANT CONNECT, TEMPORARY ON DATABASE {_quote(name)} TO {_quote(app_role)}"))


def _ensure_template(admin: Engine, run: str, owner_url: str, app_role: str) -> str:
    """Create and migrate the run's template database once (call under the run's file lock)."""
    name = f"{DB_PREFIX}{run}_tmpl"
    owner = make_url(owner_url).username or "shifde_owner"
    with admin.connect() as c:
        row = c.execute(
            text("SELECT datistemplate FROM pg_database WHERE datname = :n"), {"n": name}
        ).first()
        if row is not None and row[0]:
            return name
        if row is not None:  # a previous attempt failed half-way
            _drop_database(c, name)
        _create_database(c, name, owner, app_role, "template0")
    command.upgrade(alembic_config(_with_db(owner_url, name)), "heads")
    with admin.connect() as c:
        c.execute(
            text(f"ALTER DATABASE {_quote(name)} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false")
        )
    return name


# --------------------------------------------------------------------------- database fixtures
@pytest.fixture(scope="session")
def run_id(request: pytest.FixtureRequest) -> str:
    return request.config.stash[_RUN_STASH]


@pytest.fixture(scope="session")
def admin_url() -> str:
    """Superuser URL (database `postgres`) of the local test cluster."""
    return _env("SHIFDE_TEST_ADMIN_DATABASE_URL")


@pytest.fixture(scope="session")
def test_database(run_id: str, admin_url: str) -> Iterator[tuple[str, str]]:
    """(app_url, owner_url) of this worker's migrated database."""
    app_url = _env("SHIFDE_TEST_DATABASE_URL")
    owner_url = _env("SHIFDE_TEST_MIGRATION_DATABASE_URL")
    app_role = make_url(app_url).username or "shifde_app"
    owner_role = make_url(owner_url).username or "shifde_owner"
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "main")  # gw0, gw1, ... under xdist
    worker = re.sub(r"[^a-z0-9]", "", worker_id.lower()) or "main"
    name = f"{DB_PREFIX}{run_id}_{worker}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    lock = FileLock(str(_lock_path(run_id)))
    try:
        with lock:
            template = _ensure_template(admin, run_id, owner_url, app_role)
            with admin.connect() as c:
                _create_database(c, name, owner_role, app_role, template)
        yield _with_db(app_url, name), _with_db(owner_url, name)
    finally:
        with contextlib.suppress(Exception), admin.connect() as c:
            _drop_database(c, name)
        admin.dispose()


@pytest.fixture(scope="session")
def app_url(test_database: tuple[str, str]) -> str:
    """`shifde_app` URL of this worker's database."""
    return test_database[0]


@pytest.fixture(scope="session")
def owner_url(test_database: tuple[str, str]) -> str:
    """`shifde_owner` URL of this worker's database."""
    return test_database[1]


@pytest.fixture(scope="session")
def worker_admin_url(admin_url: str, test_database: tuple[str, str]) -> str:
    """Superuser URL of this worker's database (for tests that create throwaway roles/objects)."""
    return _with_db(admin_url, make_url(test_database[0]).database or "")


@pytest.fixture(scope="session")
def owner_engine(owner_url: str) -> Iterator[Engine]:
    engine = create_engine(owner_url, pool_size=2, max_overflow=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(app_url: str) -> Iterator[Engine]:
    engine = create_engine(app_url, pool_size=2, max_overflow=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_db(owner_url: str) -> Iterator[Database]:
    db = Database(owner_url, pool_size=2, verify_role=False)  # the schema owner, deliberately
    yield db
    db.dispose()


@pytest.fixture(scope="session")
def app_db(app_url: str) -> Iterator[Database]:
    db = Database(app_url, pool_size=1, max_overflow=0)
    yield db
    db.dispose()


@pytest.fixture(autouse=True)
def clean_data(request: pytest.FixtureRequest) -> None:
    """Wipe tenant data before each test. Only touches the database when a module has registered
    tables in `CLEAN_TABLES`, so pure unit tests need no database."""
    if not CLEAN_TABLES:
        return
    engine: Engine = request.getfixturevalue("owner_engine")
    with engine.begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(f"shifde.{t}" for t in CLEAN_TABLES)))


# --------------------------------------------------------------------------- app fixtures
@pytest.fixture
def settings(app_url: str, owner_url: str) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
        login_rate_per_account_per_minute=0,  # throttles are exercised by dedicated tests
        allowed_origins=[ORIGIN],
        log_format="json",
        log_level="INFO",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """TestClient of the app, connected to this worker's database as `shifde_app`."""
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", headers={"Origin": ORIGIN}) as c:
        yield c
