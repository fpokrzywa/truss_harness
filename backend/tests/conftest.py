"""Test fixtures: one migrated template database per run, one database per xdist worker.

Design (DECISIONS B-006, ported from SETH 3ba0e06 `tests/conftest.py`):

* The first worker that needs a database takes a file lock, creates `shifdet_<run>_tmpl` through
  the superuser URL (`SHIFDE_TEST_ADMIN_DATABASE_URL`), owned by `shifde_owner`, runs
  `alembic upgrade heads` on it as `shifde_owner`, and marks it a template that accepts no
  connections. Workers that come later find it ready.
* Each worker then gets `shifdet_<run>_<worker>` with `CREATE DATABASE ... TEMPLATE`, dropped when
  the worker finishes. The controller drops the template and any leftover database of the run.
* At session start the controller drops every `shifdet_*` database older than
  `STALE_DATABASE_AGE_SECONDS`, never one of its own run, once, under a cross-run file lock taken
  without waiting (so killed runs do not leave databases behind for ever). The age is the
  modification time of the database's `PG_VERSION` file (`pg_stat_file`) against the server's
  clock; a database whose age cannot be read is kept. Local (loopback) admin URLs only.
* Credentials, host and port come from `SHIFDE_TEST_*_DATABASE_URL` (environment first, then
  `.devdata/dev.env`); their database names are never used. Tests never touch the hosted database.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
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
from tests._support import connect_retry

DEV_ENV = Path(__file__).resolve().parents[2] / ".devdata" / "dev.env"
ORIGIN = "http://127.0.0.1:3710"
DB_PREFIX = "shifdet_"
_RUN_KEY = "shifde_test_run"
DROP_THREADS = 8  # the run's databases are dropped side by side at the end (pytest_sessionfinish)
# Another run's database older than this is a leftover of a killed run (pytest_sessionstart): well
# over the longest gate run, about 30 minutes, so a live run's databases are never swept.
STALE_DATABASE_AGE_SECONDS = 12 * 60 * 60
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

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
    connect_retry.install()  # WinError 10055/10048 at connect (issue #238)
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        config.stash[_RUN_STASH] = str(workerinput[_RUN_KEY])
    else:
        config.stash[_RUN_STASH] = uuid.uuid4().hex[:12]


# The longest tests of a full run (measured, idle machine). Collected first so that, under xdist,
# they start at the head of the run instead of leaving one worker as the tail. Nothing is
# excluded or changed; only the collection order moves.
_SLOWEST_FIRST = (
    "tests/app/test_p8_perf.py::test_dashboards_respond_within_three_seconds_over_a_million_events",
    "tests/build/test_model_refs.py::test_the_upgrade_fills_own_model_id_of_stored_rows",
    "tests/app/test_r1_round6.py::test_an_mcp_error_result_on_a_resend_is_a_refusal",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    rank = {nodeid: n for n, nodeid in enumerate(_SLOWEST_FIRST)}
    items.sort(key=lambda item: rank.get(item.nodeid, len(rank)))


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:  # node: xdist WorkerController
    """xdist controller: hand the run id to every worker."""
    node.workerinput[_RUN_KEY] = node.config.stash[_RUN_STASH]


def pytest_sessionstart(session: pytest.Session) -> None:
    """Controller (or a run without xdist): drop other runs' stale databases (issue #115)."""
    if getattr(session.config, "workerinput", None) is not None:
        return
    run = session.config.stash.get(_RUN_STASH, None)
    admin_url = _env_or_none("SHIFDE_TEST_ADMIN_DATABASE_URL")
    if run is None or admin_url is None:
        return
    with contextlib.suppress(Exception):  # best effort; a sweep never fails the session
        if make_url(admin_url).host not in _LOOPBACK_HOSTS:
            return
        # One sweep at a time across concurrent runs; a run that finds the lock taken skips it.
        with FileLock(str(Path(tempfile.gettempdir()) / "shifde-test-sweep.lock"), timeout=0):
            # A stopped cluster's port can hang a connect here instead of refusing it.
            admin = create_engine(
                admin_url,
                isolation_level="AUTOCOMMIT",
                poolclass=NullPool,
                connect_args={"connect_timeout": 5},
            )
            try:
                with admin.connect() as c:
                    names = _stale_databases(_database_ages(c), run, STALE_DATABASE_AGE_SECONDS)

                def drop(name: str) -> None:
                    with contextlib.suppress(Exception), admin.connect() as c:
                        _drop_database(c, name)

                with ThreadPoolExecutor(max_workers=DROP_THREADS) as pool:
                    list(pool.map(drop, names))
            finally:
                admin.dispose()


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Controller (or a run without xdist): drop the template and anything the run left over."""
    if getattr(session.config, "workerinput", None) is not None:
        return
    run = session.config.stash.get(_RUN_STASH, None)
    if run is not None:
        with contextlib.suppress(OSError):
            _lock_path(run).unlink(missing_ok=True)
            _alone_lock_path(run).unlink(missing_ok=True)
        shutil.rmtree(_busy_dir(run), ignore_errors=True)
    admin_url = _env_or_none("SHIFDE_TEST_ADMIN_DATABASE_URL")
    if run is None or admin_url is None:
        return
    with contextlib.suppress(Exception):  # best effort; a leftover database is harmless
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            with admin.connect() as c:
                names = list(
                    c.execute(
                        text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                        {"p": f"{DB_PREFIX}{run}\\_%"},
                    ).scalars()
                )

            def drop(name: str) -> None:
                with contextlib.suppress(Exception), admin.connect() as c:
                    _drop_database(c, name)

            # Side by side: each DROP DATABASE waits for a checkpoint, and waiting drops share one.
            # tests/app leaves its per-test databases to this (B-2330).
            with ThreadPoolExecutor(max_workers=DROP_THREADS) as pool:
                list(pool.map(drop, names))
        finally:
            admin.dispose()


# --------------------------------------------------------------------------- helpers
def _stale_databases(
    rows: Iterable[tuple[str, float | None]], run: str, max_age_seconds: float
) -> list[str]:
    """Names among `rows` (name, age in seconds or None) to drop: test databases of other runs
    older than `max_age_seconds`. An unknown age keeps the database."""
    own = f"{DB_PREFIX}{run}_"
    return [
        name
        for name, age in rows
        if name.startswith(DB_PREFIX)
        and not name.startswith(own)
        and age is not None
        and age > max_age_seconds
    ]


def _database_ages(c: Connection) -> list[tuple[str, float | None]]:
    """(name, age in seconds) of every `shifdet_*` database (superuser connection). The age is
    the server's `now()` minus the modification time of `PG_VERSION`, which `CREATE DATABASE`
    writes once; None when the file cannot be read (e.g. outside the default tablespace)."""
    rows = c.execute(
        text(
            """SELECT datname,
                      EXTRACT(EPOCH FROM now() - (pg_stat_file(
                          'base/' || oid || '/PG_VERSION', true)).modification)
                 FROM pg_database
                WHERE datname LIKE :p"""
        ),
        {"p": "shifdet\\_%"},  # DB_PREFIX with `_` escaped
    )
    return [(name, None if age is None else float(age)) for name, age in rows]


def _lock_path(run: str) -> Path:
    return Path(tempfile.gettempdir()) / f"shifde-test-{run}.lock"


def _alone_lock_path(run: str) -> Path:
    return Path(tempfile.gettempdir()) / f"shifde-test-{run}-alone.lock"


def _busy_dir(run: str) -> Path:
    return Path(tempfile.gettempdir()) / f"shifde-test-{run}-busy"


# --------------------------------------------------------------------------- timing alone
# Under xdist, a wall-clock budget is only meaningful while no other worker loads the shared
# cluster (#109, DECISIONS B-2770). Every test marks its worker busy for its duration; a test that
# times something enters `running_alone`, which stops other workers from starting a test and waits
# until the tests they are running have finished. Without xdist there is nothing to wait for.
@pytest.fixture(autouse=True)
def _busy_marker(request: pytest.FixtureRequest) -> Iterator[None]:
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        yield
        return
    run = request.config.stash[_RUN_STASH]
    busy = _busy_dir(run) / f"{worker}.busy"
    with FileLock(str(_alone_lock_path(run))):  # held by `running_alone` while it times
        busy.parent.mkdir(exist_ok=True)
        busy.touch()
    try:
        yield
    finally:
        busy.unlink(missing_ok=True)


@contextlib.contextmanager
def running_alone(config: pytest.Config) -> Iterator[None]:
    """Run the block while no other xdist worker of this run is inside a test."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        yield
        return
    run = config.stash[_RUN_STASH]
    with FileLock(str(_alone_lock_path(run))):
        while any(p.stem != worker for p in _busy_dir(run).glob("*.busy")):
            time.sleep(0.1)
        yield


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
    """Drop `name` (connection in AUTOCOMMIT); a template must be unmarked first. A database left
    invalid by an interrupted drop (`datconnlimit = -2`) refuses ALTER and is dropped as it is."""
    valid = c.execute(
        text("SELECT datconnlimit <> -2 FROM pg_database WHERE datname = :n"), {"n": name}
    ).scalar()
    if valid:
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
def sample_db_url(run_id: str, admin_url: str) -> Iterator[str]:
    """A throwaway `shifde_sample`-shaped database + read-only reader role (P3 PostgreSQL target).

    SETH 3ba0e06 `tests/conftest.py::sample_db_url` (B-2420): built through the local cluster's
    superuser URL exactly like `shifde-admin dev-sample-data`; unique names so concurrent workers
    and runs never collide; dropped at session end (best effort). The database name carries this
    run's prefix, so `pytest_sessionfinish` drops it too if the worker dies."""
    from shifde.cli.sampledata import build_sample_database, drop_sample_database

    suffix = f"{os.getpid()}_{secrets.token_hex(3)}"
    db_name, role, password = (
        f"{DB_PREFIX}{run_id}_sample{suffix}",
        f"shifde_sample_r{run_id}_{suffix}",
        secrets.token_hex(12),
    )
    build_sample_database(admin_url, db_name, role, password)
    admin = make_url(admin_url)
    try:
        yield f"postgresql://{role}:{password}@{admin.host}:{admin.port}/{db_name}"
    finally:
        with contextlib.suppress(Exception):  # best effort; a leftover database is harmless
            drop_sample_database(admin_url, db_name, role)


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
    tables in `CLEAN_TABLES`, so pure unit tests need no database.

    DELETE, not TRUNCATE: TRUNCATE gives every table (and its indexes and TOAST) new files, and
    the next checkpoint has to fsync them all; at a hundred tables per test that made checkpoints
    take seconds and every `DROP DATABASE` wait for them (B-2330). The superuser on the test's own
    database, with `session_replication_role = replica`, is not subject to row-level security,
    append-only triggers or foreign-key checks, so the tables end up as empty as TRUNCATE left
    them."""
    if not CLEAN_TABLES:
        return
    engine: Engine = request.getfixturevalue("owner_engine")
    database = engine.url.database or ""
    admin = create_engine(
        _with_db(_env("SHIFDE_TEST_ADMIN_DATABASE_URL"), database), poolclass=NullPool
    )
    try:
        with admin.begin() as c:
            c.execute(text("SET LOCAL session_replication_role = replica"))
            for table in _with_referencing_tables(c, CLEAN_TABLES):
                c.execute(text(f"DELETE FROM shifde.{_quote(table)}"))
    finally:
        admin.dispose()


_REFERENCING = text(
    """SELECT DISTINCT src.relname
         FROM pg_catalog.pg_constraint k
         JOIN pg_catalog.pg_class src ON src.oid = k.conrelid
         JOIN pg_catalog.pg_class dst ON dst.oid = k.confrelid
         JOIN pg_catalog.pg_namespace n ON n.oid = src.relnamespace
        WHERE k.contype = 'f' AND n.nspname = 'shifde'
          AND dst.relname = ANY(:tables) AND src.relname <> ALL(:tables)"""
)


def _with_referencing_tables(c: Connection, tables: list[str]) -> list[str]:
    """`tables` plus every `shifde` table whose foreign keys reach them (TRUNCATE refuses a table
    another table references unless both are truncated). A module's tests then clean up whatever
    later modules' tables point at its rows (e.g. identity's `idp_configs` -> `secrets` when only
    the secrets tests run), without each module listing the others."""
    result = list(tables)
    while extra := sorted(c.execute(_REFERENCING, {"tables": result}).scalars()):
        result += extra
    return result


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


@pytest.fixture
def frozen_cookie_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the date a clearing cookie's `Expires=` carries (B-2320).

    Starlette's `delete_cookie` sets `expires=0`, which `http.cookies` renders as "now" through
    `http.cookies._getdate`. Two responses that straddle a second boundary then differ in
    `Expires`. Tests that compare `Set-Cookie` headers byte for byte use this fixture instead of
    stripping or tolerating the date.
    """
    import http.cookies as http_cookies
    import time

    fixed = time.time()
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = (
        "",
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )

    def getdate(future: float = 0) -> str:
        year, month, day, hh, mm, ss, wd, _y, _z = time.gmtime(fixed + future)
        return f"{weekdays[wd]}, {day:02d} {months[month]} {year:4d} {hh:02d}:{mm:02d}:{ss:02d} GMT"

    monkeypatch.setattr(http_cookies, "_getdate", getdate)  # private stdlib helper, no stub
