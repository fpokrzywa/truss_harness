"""The application refuses to run under a database role that can bypass row-level security
(Review Focus item 2).

Throwaway roles are created through the superuser URL with random names and passwords and are
dropped afterwards (`DROP OWNED` + `DROP ROLE`).
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from shifde.app import create_app
from shifde.core.db import Database
from shifde.core.dbrole import UnsafeDatabaseRole, check_app_role, check_database_url
from shifde.core.settings import Settings

MakeRole = Callable[..., str]


def _engine(url: str) -> Engine:
    return create_engine(url, poolclass=NullPool)


def _check(url: str) -> None:
    engine = _engine(url)
    try:
        check_app_role(engine)
    finally:
        engine.dispose()


@pytest.fixture
def admin(worker_admin_url: str) -> Iterator[Engine]:
    """Superuser engine on this worker's database (AUTOCOMMIT)."""
    engine = create_engine(worker_admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def make_role(admin: Engine, app_url: str) -> Iterator[MakeRole]:
    """`make_role(attrs, login=True) -> url`: a throwaway role on this worker's database; for a
    NOLOGIN role the return value is just its name. All are dropped at teardown."""
    created: list[str] = []
    database = make_url(app_url).database

    def make(attrs: str = "", *, login: bool = True) -> str:
        name = f"shifdet_role_{secrets.token_hex(5)}"
        password = secrets.token_hex(16)
        with admin.connect() as c:
            if login:
                c.execute(text(f"CREATE ROLE {name} LOGIN {attrs} PASSWORD '{password}'"))
                c.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {name}'))
            else:
                c.execute(text(f"CREATE ROLE {name} NOLOGIN {attrs}"))
        created.append(name)
        if not login:
            return name
        return (
            make_url(app_url)
            .set(username=name, password=password)
            .render_as_string(hide_password=False)
        )

    yield make
    with admin.connect() as c:
        for name in reversed(created):
            c.execute(text(f"DROP OWNED BY {name} CASCADE"))
            c.execute(text(f"DROP ROLE {name}"))


def _role_of(url: str) -> str:
    return make_url(url).username or ""


# --------------------------------------------------------------------------- refused
def test_superuser_is_refused(worker_admin_url: str) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="is a superuser"):
        _check(worker_admin_url)


def test_superuser_role_attribute_is_refused(make_role: MakeRole) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="is a superuser"):
        _check(make_role("SUPERUSER"))


def test_bypassrls_role_is_refused(make_role: MakeRole) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="has BYPASSRLS"):
        _check(make_role("NOSUPERUSER BYPASSRLS"))


def test_replication_role_is_refused(make_role: MakeRole) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="has REPLICATION"):
        _check(make_role("NOSUPERUSER NOBYPASSRLS REPLICATION"))


def test_createrole_role_is_refused(make_role: MakeRole) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="has CREATEROLE"):
        _check(make_role("NOSUPERUSER NOBYPASSRLS CREATEROLE"))


def test_member_of_replication_or_createrole_role_is_refused(
    make_role: MakeRole, admin: Engine
) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS NOINHERIT")
    repl = make_role("NOSUPERUSER REPLICATION", login=False)
    creator = make_role("NOSUPERUSER CREATEROLE", login=False)
    with admin.connect() as c:
        c.execute(text(f"GRANT {repl} TO {_role_of(url)}"))
        c.execute(text(f"GRANT {creator} TO {_role_of(url)}"))
    with pytest.raises(UnsafeDatabaseRole) as exc:
        _check(url)
    assert f"member of role '{repl}' has REPLICATION" in exc.value.reason
    assert f"member of role '{creator}' has CREATEROLE" in exc.value.reason


def test_statistics_reader_is_refused(make_role: MakeRole, admin: Engine) -> None:
    """pg_monitor (a member of pg_read_all_stats) sees other sessions' query texts."""
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    with admin.connect() as c:
        c.execute(text(f"GRANT pg_monitor TO {_role_of(url)}"))
    with pytest.raises(UnsafeDatabaseRole) as exc:
        _check(url)
    assert "member of role 'pg_read_all_stats' reads other sessions' statistics" in (
        exc.value.reason
    )
    assert "member of role 'pg_monitor' reads other sessions' statistics" in exc.value.reason


def test_create_on_a_schema_is_refused(make_role: MakeRole, admin: Engine) -> None:
    """CREATE on a schema lets the role plant objects that shadow names other code resolves."""
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    with admin.connect() as c:
        c.execute(text(f"GRANT CREATE ON SCHEMA public TO {_role_of(url)}"))
    try:
        with pytest.raises(UnsafeDatabaseRole, match="may create objects in schema public"):
            _check(url)
    finally:
        with admin.connect() as c:
            c.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {_role_of(url)}"))


def test_create_granted_to_public_is_refused(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    with admin.connect() as c:
        c.execute(text("GRANT CREATE ON SCHEMA public TO PUBLIC"))
    try:
        with pytest.raises(UnsafeDatabaseRole, match="may create objects in schema public"):
            _check(url)
    finally:
        with admin.connect() as c:
            c.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))


# --------------------------------------------------------------------------- preset context
TENANT_X = "01900000-0000-7000-8000-0000000000aa"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("app.platform_admin", "on"),
        ("app.tenant_id", TENANT_X),
        ("app.principal_id", TENANT_X),
    ],
)
def test_role_with_preset_context_is_refused(
    make_role: MakeRole, admin: Engine, setting: str, value: str
) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    with admin.connect() as c:
        c.execute(text(f"ALTER ROLE {_role_of(url)} SET {setting} = '{value}'"))
    with pytest.raises(UnsafeDatabaseRole) as exc:
        _check(url)
    assert f"{setting} is preset on a new connection" in exc.value.reason
    assert f"ALTER ROLE ... SET {setting}" in exc.value.reason


def test_database_with_preset_context_is_refused(admin: Engine, app_url: str) -> None:
    database = make_url(app_url).database
    with admin.connect() as c:
        c.execute(text(f"ALTER DATABASE \"{database}\" SET app.tenant_id = '{TENANT_X}'"))
    try:
        with pytest.raises(UnsafeDatabaseRole) as exc:
            _check(app_url)
        assert "app.tenant_id is preset on a new connection" in exc.value.reason
        assert "ALTER DATABASE ... SET app.tenant_id" in exc.value.reason
    finally:
        with admin.connect() as c:
            c.execute(text(f'ALTER DATABASE "{database}" RESET app.tenant_id'))


def test_url_options_with_preset_context_are_refused(app_url: str) -> None:
    url = make_url(app_url).update_query_dict({"options": f"-c app.tenant_id={TENANT_X}"})
    with pytest.raises(UnsafeDatabaseRole, match="app.tenant_id is preset on a new connection"):
        _check(url.render_as_string(hide_password=False))


def test_table_owner_is_refused(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    role = _role_of(url)
    with admin.connect() as c:
        c.execute(text("CREATE TABLE shifde.zz_owned_probe (id int PRIMARY KEY)"))
        c.execute(text(f"ALTER TABLE shifde.zz_owned_probe OWNER TO {role}"))
    try:
        with pytest.raises(UnsafeDatabaseRole) as exc:
            _check(url)
        assert f"role '{role}' owns relation shifde.zz_owned_probe" in exc.value.reason
    finally:
        with admin.connect() as c:
            c.execute(text("DROP TABLE IF EXISTS shifde.zz_owned_probe"))


def _first_owned_relation(admin: Engine, role: str) -> str:
    """The relation `check_app_role` names for `role`: the smallest `schema.name` among its
    relations (tables, indexes, sequences) in non-system schemas. It is `shifde.alembic_version`
    only while no module has a table sorting before it (DECISIONS B-303)."""
    with admin.connect() as c:
        return str(
            c.execute(
                text(
                    "SELECT min(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_roles r ON r.oid = c.relowner "
                    "WHERE r.rolname = :role AND n.nspname NOT IN ('pg_catalog', "
                    "'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"
                ),
                {"role": role},
            ).scalar_one()
        )


def test_schema_owner_role_shifde_owner_is_refused(owner_url: str, admin: Engine) -> None:
    with pytest.raises(UnsafeDatabaseRole) as exc:
        _check(owner_url)
    reason = exc.value.reason
    assert "owns the database" in reason
    assert "owns schema shifde" in reason
    assert f"owns relation {_first_owned_relation(admin, 'shifde_owner')}" in reason


def test_function_owner_is_refused(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    role = _role_of(url)
    with admin.connect() as c:
        c.execute(text(f"ALTER FUNCTION shifde.current_tenant_id() OWNER TO {role}"))
    try:
        with pytest.raises(UnsafeDatabaseRole, match="owns function shifde.current_tenant_id"):
            _check(url)
    finally:
        with admin.connect() as c:
            c.execute(text("ALTER FUNCTION shifde.current_tenant_id() OWNER TO shifde_owner"))


def test_database_owner_is_refused(make_role: MakeRole, admin: Engine, app_url: str) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    database = make_url(app_url).database
    with admin.connect() as c:
        c.execute(text(f'ALTER DATABASE "{database}" OWNER TO {_role_of(url)}'))
    try:
        with pytest.raises(UnsafeDatabaseRole) as exc:
            _check(url)
        assert "owns the database" in exc.value.reason
        # pg_database_owner owns schema public, so the database owner owns it too.
        assert "member of role 'pg_database_owner' owns schema public" in exc.value.reason
    finally:
        with admin.connect() as c:
            c.execute(text(f'ALTER DATABASE "{database}" OWNER TO shifde_owner'))


def test_member_of_owner_role_is_refused(make_role: MakeRole, admin: Engine) -> None:
    # NOINHERIT: the member holds none of the owner's rights until it runs SET ROLE, which it may.
    url = make_role("NOSUPERUSER NOBYPASSRLS NOINHERIT")
    with admin.connect() as c:
        c.execute(text(f"GRANT shifde_owner TO {_role_of(url)}"))
    with pytest.raises(UnsafeDatabaseRole) as exc:
        _check(url)
    expected = _first_owned_relation(admin, "shifde_owner")
    assert f"member of role 'shifde_owner' owns relation {expected}" in exc.value.reason


def test_indirect_member_of_table_owner_is_refused(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    owner_group = make_role("NOSUPERUSER NOBYPASSRLS", login=False)
    middle = make_role("NOSUPERUSER NOBYPASSRLS", login=False)
    with admin.connect() as c:
        c.execute(text("CREATE TABLE shifde.zz_owned_probe2 (id int PRIMARY KEY)"))
        c.execute(text(f"ALTER TABLE shifde.zz_owned_probe2 OWNER TO {owner_group}"))
        c.execute(text(f"GRANT {owner_group} TO {middle}"))
        c.execute(text(f"GRANT {middle} TO {_role_of(url)}"))
    try:
        with pytest.raises(UnsafeDatabaseRole) as exc:
            _check(url)
        assert f"member of role '{owner_group}' owns relation shifde.zz_owned_probe2" in (
            exc.value.reason
        )
    finally:
        with admin.connect() as c:
            c.execute(text("DROP TABLE IF EXISTS shifde.zz_owned_probe2"))


def test_member_of_bypassrls_role_is_refused(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    group = make_role("NOSUPERUSER BYPASSRLS", login=False)
    with admin.connect() as c:
        c.execute(text(f"GRANT {group} TO {_role_of(url)}"))
    with pytest.raises(UnsafeDatabaseRole, match=f"member of role '{group}' has BYPASSRLS"):
        _check(url)


def test_server_file_access_is_refused(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    with admin.connect() as c:
        c.execute(text(f"GRANT pg_read_server_files TO {_role_of(url)}"))
    with pytest.raises(UnsafeDatabaseRole, match="pg_read_server_files"):
        _check(url)


# --------------------------------------------------------------------------- accepted
def test_application_role_is_accepted(app_engine: Engine) -> None:
    check_app_role(app_engine)  # does not raise


def test_plain_login_role_is_accepted(make_role: MakeRole) -> None:
    check_database_url(make_role("NOSUPERUSER NOBYPASSRLS"))  # does not raise


# --------------------------------------------------------------------------- the real entrypoint
def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _listening(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start_api(database_url: str, port: int) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHIFDE_")}
    env.update(
        SHIFDE_ENV="test",
        SHIFDE_DATABASE_URL=database_url,
        SHIFDE_API_HOST="127.0.0.1",
        SHIFDE_API_PORT=str(port),
    )
    return subprocess.Popen(
        [sys.executable, "-m", "shifde.main"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_api_exits_3_under_owner_role_without_listening(owner_url: str) -> None:
    port = _free_port()
    proc = _start_api(owner_url, port)
    try:
        deadline = time.monotonic() + 60
        while proc.poll() is None and time.monotonic() < deadline:
            assert not _listening(port), "the API bound its port under an unsafe role"
            time.sleep(0.05)
        assert proc.poll() is not None, "the API did not exit under an unsafe role"
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 3, (stdout, stderr)
    lines = [line for line in stderr.splitlines() if line.strip()]
    assert len(lines) == 1, stderr
    assert lines[0].startswith("shifde: refusing to start: database role 'shifde_owner'")
    password = make_url(owner_url).password
    assert password and password not in stderr


def test_api_serves_under_application_role(app_url: str) -> None:
    """Positive control: the same probe sees the port open when the role is safe."""
    port = _free_port()
    proc = _start_api(app_url, port)
    try:
        deadline = time.monotonic() + 60
        while not _listening(port):
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "the API did not start"
            time.sleep(0.1)
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=10)
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
    finally:
        proc.terminate()
        proc.communicate(timeout=30)


# --------------------------------------------------------------------------- every entrypoint
def test_database_refuses_unsafe_role_when_built(owner_url: str) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="owns the database"):
        Database(owner_url)


def test_create_app_refuses_unsafe_role(settings: Settings, owner_url: str) -> None:
    with pytest.raises(UnsafeDatabaseRole, match="owns the database"):
        create_app(settings.model_copy(update={"database_url": owner_url}))


def test_privileged_database_must_opt_out_explicitly(owner_url: str) -> None:
    db = Database(owner_url, verify_role=False)  # migrations / test fixtures only
    try:
        with db.session() as s:
            assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_owner"
    finally:
        db.dispose()


def test_pool_checkout_refuses_preset_context(make_role: MakeRole, admin: Engine) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    db = Database(url, pool_size=1, max_overflow=0)  # safe when built
    try:
        with admin.connect() as c:
            c.execute(text(f"ALTER ROLE {_role_of(url)} SET app.platform_admin = 'on'"))
        db.engine.dispose()  # the next checkout opens a new connection, which gets the preset
        for _ in range(2):  # refused every time: the connection is discarded, not reused
            with (
                pytest.raises(UnsafeDatabaseRole, match="app.platform_admin is preset"),
                db.session() as s,
            ):
                s.execute(text("SELECT 1"))
    finally:
        db.dispose()


def test_session_level_context_does_not_survive_checkin(app_url: str) -> None:
    db = Database(app_url, pool_size=1, max_overflow=0)
    try:
        with db.engine.connect() as c:
            c.exec_driver_sql(f"SET app.tenant_id = '{TENANT_X}'")  # session-level: a bug
            c.exec_driver_sql("SET search_path = public")
            c.commit()
        with db.session() as s:
            row = s.execute(
                text(
                    "SELECT pg_backend_pid(), current_setting('app.tenant_id', true),"
                    " current_setting('search_path'), current_setting('TimeZone')"
                )
            ).one()
            assert row[1] in (None, "")
            assert row[2] == '"$user", public'
            assert row[3] == "UTC"  # connection options survive DISCARD ALL
    finally:
        db.dispose()


# --------------------------------------------------------------------------- isolation review 2
def test_temporary_objects_do_not_survive_checkin(app_url: str) -> None:
    """DISCARD ALL on checkout: a temp table (which could shadow a name) never reaches the next
    user of the pooled connection."""
    db = Database(app_url, pool_size=1, max_overflow=0)
    try:
        with db.engine.connect() as c:
            pid = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            c.exec_driver_sql("CREATE TEMP TABLE zz_left_behind (x int)")
            c.commit()
        with db.session() as s:
            row = s.execute(
                text("SELECT pg_backend_pid(), to_regclass('pg_temp.zz_left_behind')")
            ).one()
            assert row[0] == pid  # same pooled connection
            assert row[1] is None
    finally:
        db.dispose()


def test_auto_prepared_statements_survive_discard(app_url: str) -> None:
    """psycopg prepares a query after 5 runs; DISCARD ALL drops it server-side and psycopg must
    notice (it does: it clears its cache on DISCARD ALL)."""
    db = Database(app_url, pool_size=1, max_overflow=0)
    try:
        for _ in range(3):
            with db.session() as s:
                for i in range(8):
                    assert (
                        s.execute(text("SELECT CAST(:i AS int) + 1"), {"i": i}).scalar_one()
                        == i + 1
                    )
    finally:
        db.dispose()


def test_many_checkouts_of_one_pooled_connection_keep_working(app_url: str) -> None:
    """Regression (DECISIONS B-404): psycopg auto-prepares a statement on its sixth run, the
    checkout's own `DISCARD ALL` included; that prepared statement then deallocated itself, and
    the next checkout of the connection failed with 'prepared statement "_pg3_0" does not exist'.
    psycopg notices DISCARD ALL only while it holds statements it prepared, so its cache could go
    stale either way. Each commit releases the connection; the next statement checks it out."""
    db = Database(app_url, pool_size=1, max_overflow=0)
    try:
        with db.session() as s:
            for n in range(12):
                assert s.execute(text("SELECT CAST(:n AS int)"), {"n": n}).scalar_one() == n
                s.commit()
        for n in range(12):
            with db.session() as s:
                for i in range(7):
                    got = s.execute(text("SELECT CAST(:i AS int) + :n"), {"i": i, "n": n})
                    assert got.scalar_one() == i + n
    finally:
        db.dispose()


def test_create_on_the_database_is_refused(
    make_role: MakeRole, admin: Engine, app_url: str
) -> None:
    """CREATE on the database lets the role create schemas (e.g. one named after itself, first on
    the default `"$user"` search path) and trusted extensions."""
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    database = make_url(app_url).database
    with admin.connect() as c:
        c.execute(text(f'GRANT CREATE ON DATABASE "{database}" TO {_role_of(url)}'))
    with pytest.raises(UnsafeDatabaseRole, match="may create schemas in the database"):
        _check(url)


@pytest.mark.parametrize("predefined", ["pg_read_all_data", "pg_write_all_data", "pg_maintain"])
def test_data_wide_predefined_roles_are_refused(
    make_role: MakeRole, admin: Engine, predefined: str
) -> None:
    url = make_role("NOSUPERUSER NOBYPASSRLS")
    with admin.connect() as c:
        c.execute(text(f"GRANT {predefined} TO {_role_of(url)}"))
    with pytest.raises(UnsafeDatabaseRole, match=f"member of role '{predefined}'"):
        _check(url)
