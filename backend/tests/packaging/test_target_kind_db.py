"""`packaging_0004` in the database (issue #271, SP6-4; SP6 design section 5).

As `shifde_app` with a client context, a `solutions` row takes `target_kind`
`databricks.app/v1` or `snowflake.native-app/v1` (`ck_solutions__target_kind`) and any other kind
is 23514. The downgrade to `packaging_0003` is refused while any client has a Snowflake solution,
naming only the count, and otherwise restores the original single-value CHECK under its generated
name, on a scratch database built from this run's template.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config
from tests._support.fullapp import World
from tests._support.tenants import add_project, add_tenant, add_workspace
from tests.conftest import DB_PREFIX, _create_database, _drop_database, _with_db
from tests.tenancy.conftest import app_session

SNOWFLAKE = "snowflake.native-app/v1"
_INSERT = text(
    "INSERT INTO shifde.solutions (tenant_id, id, workspace_id, project_id, name, target_kind, "
    "created_by) VALUES (:t, :i, :w, :p, :n, :k, :t)"
)
# the CHECK constraints on `solutions.target_kind`: name and definition
_CHECKS = (
    "SELECT k.conname, pg_get_constraintdef(k.oid) FROM pg_catalog.pg_constraint k "
    "JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.conkey[1] "
    "WHERE n.nspname = 'shifde' AND k.contype = 'c' AND c.relname = 'solutions' "
    "AND a.attname = 'target_kind'"
)


def _row(tenant: UUID, ws: UUID, project: UUID, kind: str) -> dict[str, object]:
    return {"t": tenant, "i": uuid7(), "w": ws, "p": project, "n": uuid7().hex, "k": kind}


def test_the_check_takes_the_two_kinds_and_refuses_another(
    world: World, owner_session: Session, app_db: Database
) -> None:
    tenant, ws, project = owner_session.execute(
        text(
            "SELECT t.id, w.id, p.id FROM shifde.tenants t "
            "JOIN shifde.workspaces w ON w.tenant_id = t.id AND w.slug = 'main' "
            "JOIN shifde.projects p ON p.workspace_id = w.id AND p.slug = 'sample' "
            "WHERE t.slug = 'demo-a'"
        )
    ).one()
    owner_session.rollback()
    for kind in ("databricks.app/v1", SNOWFLAKE):
        with app_session(app_db, tenant) as app:
            app.execute(_INSERT, _row(tenant, ws, project, kind))
            app.commit()
    for kind in ("snowflake.app/v1", "databricks.native-app/v1", ""):
        with app_session(app_db, tenant) as app, pytest.raises(DBAPIError) as err:
            app.execute(_INSERT, _row(tenant, ws, project, kind))
        assert getattr(err.value.orig, "sqlstate", None) == "23514", err.value
    checks = owner_session.execute(text(_CHECKS)).all()
    owner_session.rollback()
    assert [tuple(c) for c in checks] == [
        (
            "ck_solutions__target_kind",
            "CHECK ((target_kind = ANY (ARRAY['databricks.app/v1'::text, "
            "'snowflake.native-app/v1'::text])))",
        )
    ]


@contextlib.contextmanager
def _scratch(run_id: str, admin_url: str, owner_url: str, app_url: str, tag: str) -> Iterator[str]:
    """A database built from this run's (fully migrated) template, dropped afterwards."""
    name = f"{DB_PREFIX}{run_id}_{tag}"
    owner = make_url(owner_url).username or "shifde_owner"
    app_role = make_url(app_url).username or "shifde_app"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as c:
            if c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first():
                _drop_database(c, name)
            _create_database(c, name, owner, app_role, f"{DB_PREFIX}{run_id}_tmpl")
        yield _with_db(owner_url, name)
    finally:
        with contextlib.suppress(Exception), admin.connect() as c:
            _drop_database(c, name)
        admin.dispose()


def _solution(c: Connection, kind: str) -> UUID:
    """A solution of `kind` in a new client (owner rights, in that client's context)."""
    tenant = add_tenant(c)
    ws = add_workspace(c, tenant)
    project = add_project(c, tenant, ws)
    c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
    row = _row(tenant, ws, project, kind)
    c.execute(_INSERT, row)
    return UUID(str(row["i"]))


def test_the_downgrade_is_refused_while_a_snowflake_solution_exists(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "pkgkind1") as url:
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.begin() as c:
                _solution(c, SNOWFLAKE)
            with engine.begin() as c:
                _solution(c, SNOWFLAKE)
            with engine.begin() as c:
                databricks = _solution(c, "databricks.app/v1")
            with pytest.raises(DBAPIError) as err:
                command.downgrade(cfg, "packaging_0003")
            message = str(err.value.orig).splitlines()[0]
            assert message == "packaging_0004: 2 Snowflake solution row(s) exist"
            assert str(databricks) not in str(err.value)
            with engine.connect() as c:  # the downgrade was refused as a whole
                assert [r[0] for r in c.execute(text(_CHECKS))] == ["ck_solutions__target_kind"]
        finally:
            engine.dispose()


def test_the_downgrade_restores_the_original_check_without_snowflake_solutions(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "pkgkind2") as url:
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.begin() as c:
                _solution(c, "databricks.app/v1")
            command.downgrade(cfg, "packaging_0003")
            with engine.connect() as c:
                assert [tuple(r) for r in c.execute(text(_CHECKS))] == [
                    (
                        "solutions_target_kind_check",
                        "CHECK ((target_kind = 'databricks.app/v1'::text))",
                    )
                ]
            with pytest.raises(DBAPIError) as err, engine.begin() as c:
                _solution(c, SNOWFLAKE)
            assert getattr(err.value.orig, "sqlstate", None) == "23514", err.value
            command.upgrade(cfg, "heads")
            with engine.begin() as c:
                _solution(c, SNOWFLAKE)
        finally:
            engine.dispose()
