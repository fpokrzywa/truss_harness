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

from shifde.core.dbrole import UnsafeDatabaseRole, check_app_role, check_database_url

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


def test_schema_owner_role_shifde_owner_is_refused(owner_url: str) -> None:
    with pytest.raises(UnsafeDatabaseRole) as exc:
        _check(owner_url)
    reason = exc.value.reason
    assert "owns the database" in reason
    assert "owns schema shifde" in reason
    assert "owns relation shifde.alembic_version" in reason


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
    assert "member of role 'shifde_owner' owns relation shifde.alembic_version" in (
        exc.value.reason
    )


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
