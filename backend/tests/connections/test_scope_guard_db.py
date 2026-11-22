"""`connections_0002` and `models_admin_0004` in the database (#156, design 2026-11-22 section 5,
DECISIONS B-2743, B-2744, B-2754).

As `shifde_app` with a client context:

* a connection's secret must have the connection's scope (`connections_guard`, 23514), on insert
  and on a change of `secret_ref_id`;
* names are unique within a scope: the same name in two projects, or in a project and at client
  level, is allowed; twice in one scope is 23505;
* a client's model provider needs a client-wide connection (23514).

The two prechecks refuse an upgrade over violating rows, naming only the count (never an id or a
name), on a scratch database built from this run's template.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any  # Any: SQL parameter values
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
from tests._support.tenants import add_project, add_tenant, add_workspace
from tests.conftest import DB_PREFIX, _create_database, _drop_database, _with_db
from tests.connections.conftest import Pair, connection_values
from tests.connections.routes import SECRET_PREFIX
from tests.tenancy.conftest import app_session

_INSERT_CONNECTION = text(
    "INSERT INTO shifde.connections (id, tenant_id, workspace_id, project_id, name, "
    "connection_type, adapter_key, vendor_id, config, auth_method, secret_ref_id, "
    "egress_policy_id, access_mode, created_by) VALUES (:id, :tenant_id, :workspace_id, "
    ":project_id, :name, :connection_type, :adapter_key, :vendor_id, CAST(:config AS jsonb), "
    ":auth_method, :secret_ref_id, :egress_policy_id, :access_mode, :created_by)"
)
_INSERT_PROVIDER = text(
    "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, connection_id) "
    "VALUES (:i, :t, :n, 'openai_compatible', :c)"
)


def _sqlstate(err: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    return getattr(err.value.orig, "sqlstate", None)


def _secret(owner: Session | Connection, tenant: UUID, scope: str, scope_id: UUID | None) -> UUID:
    sid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref, "
            "scope_type, scope_id) VALUES (:i, :t, :n, 'env', 'SHIFDE_TEST_UNUSED', :s, :sid)"
        ),
        {"i": sid, "t": tenant, "n": f"{SECRET_PREFIX}{sid.hex}", "s": scope, "sid": scope_id},
    )
    return sid


def _connection(s: Session | Connection, tenant: UUID, **overrides: Any) -> UUID:
    values = connection_values(tenant, **overrides)
    values["config"] = '{"base_url": "https://api.example.com"}'
    s.execute(_INSERT_CONNECTION, values)
    cid: UUID = values["id"]
    return cid


class _Scopes:
    """Client A's scopes: client, workspace `ws`, `project` in `ws`, `project2` in `ws`, and a
    secret of each scope (plus one of `project2`)."""

    def __init__(self, owner: Session, pair: Pair) -> None:
        c = pair.a.c
        self.tenant, self.ws, self.ws2, self.project = c.tenant, c.ws, c.ws2, c.project
        self.project2 = add_project(owner, c.tenant, c.ws)
        self.tenant_secret = _secret(owner, c.tenant, "tenant", None)
        self.ws_secret = _secret(owner, c.tenant, "workspace", c.ws)
        self.ws2_secret = _secret(owner, c.tenant, "workspace", c.ws2)
        self.project_secret = _secret(owner, c.tenant, "project", c.project)
        self.project2_secret = _secret(owner, c.tenant, "project", self.project2)

    def where(self, scope: str) -> dict[str, UUID | None]:
        return {
            "client": {"workspace_id": None, "project_id": None},
            "workspace": {"workspace_id": self.ws, "project_id": None},
            "project": {"workspace_id": self.ws, "project_id": self.project},
        }[scope]


@pytest.fixture
def scopes(owner_db: Database, pair: Pair) -> _Scopes:
    with owner_db.session() as o:
        got = _Scopes(o, pair)
        o.commit()
    return got


# (connection scope, secret attribute of _Scopes): every pairing whose scopes differ
MISMATCHES = [
    ("client", "ws_secret"),
    ("client", "project_secret"),
    ("workspace", "tenant_secret"),
    ("workspace", "ws2_secret"),
    ("workspace", "project_secret"),
    ("project", "tenant_secret"),
    ("project", "ws_secret"),
    ("project", "project2_secret"),
]
MATCHES = [("client", "tenant_secret"), ("workspace", "ws_secret"), ("project", "project_secret")]


@pytest.mark.parametrize(("scope", "secret"), MISMATCHES)
def test_a_secret_of_another_scope_is_refused_with_23514(
    app_db: Database, scopes: _Scopes, scope: str, secret: str
) -> None:
    with app_session(app_db, scopes.tenant) as s, pytest.raises(DBAPIError) as err:
        _connection(s, scopes.tenant, secret_ref_id=getattr(scopes, secret), **scopes.where(scope))
    assert _sqlstate(err) == "23514"
    assert "secret scope must match the connection scope" in str(err.value.orig)


@pytest.mark.parametrize(("scope", "secret"), MATCHES)
def test_a_secret_of_the_connections_scope_is_accepted(
    app_db: Database, scopes: _Scopes, scope: str, secret: str
) -> None:
    with app_session(app_db, scopes.tenant) as s:
        cid = _connection(
            s, scopes.tenant, secret_ref_id=getattr(scopes, secret), **scopes.where(scope)
        )
        s.commit()
        assert s.execute(
            text("SELECT secret_ref_id FROM shifde.connections WHERE id = :i"), {"i": cid}
        ).scalar_one() == getattr(scopes, secret)


def test_rebinding_a_secret_of_another_scope_is_refused_with_23514(
    app_db: Database, scopes: _Scopes
) -> None:
    with app_session(app_db, scopes.tenant) as s:
        cid = _connection(
            s, scopes.tenant, secret_ref_id=scopes.project_secret, **scopes.where("project")
        )
        s.commit()
    with app_session(app_db, scopes.tenant) as s, pytest.raises(DBAPIError) as err:
        s.execute(
            text("UPDATE shifde.connections SET secret_ref_id = :x WHERE id = :i"),
            {"x": scopes.project2_secret, "i": cid},
        )
    assert _sqlstate(err) == "23514"


def test_another_clients_secret_keeps_its_message(app_db: Database, pair: Pair) -> None:
    with app_session(app_db, pair.a.tenant) as s, pytest.raises(DBAPIError) as err:
        _connection(s, pair.a.tenant, secret_ref_id=pair.b.secret)
    # the composite key or the guard refuses it; the guard's first check keeps its message
    assert _sqlstate(err) in {"23514", "23503"}
    assert "secret scope" not in str(err.value.orig)


def test_the_same_name_in_two_projects_and_at_client_level_is_allowed(
    app_db: Database, scopes: _Scopes
) -> None:
    with app_session(app_db, scopes.tenant) as s:
        _connection(s, scopes.tenant, name="shared-name", **scopes.where("project"))
        _connection(
            s, scopes.tenant, name="shared-name", workspace_id=scopes.ws, project_id=scopes.project2
        )
        _connection(s, scopes.tenant, name="shared-name", **scopes.where("workspace"))
        _connection(s, scopes.tenant, name="shared-name", workspace_id=scopes.ws2, project_id=None)
        _connection(s, scopes.tenant, name="shared-name", **scopes.where("client"))
        s.commit()
        assert (
            s.execute(
                text("SELECT count(*) FROM shifde.connections WHERE name = 'shared-name'")
            ).scalar_one()
            == 5
        )


@pytest.mark.parametrize("scope", ["client", "workspace", "project"])
def test_the_same_name_twice_in_one_scope_is_23505(
    app_db: Database, scopes: _Scopes, scope: str
) -> None:
    with app_session(app_db, scopes.tenant) as s:
        _connection(s, scopes.tenant, name="dup", **scopes.where(scope))
        s.commit()
    with app_session(app_db, scopes.tenant) as s, pytest.raises(DBAPIError) as err:
        _connection(s, scopes.tenant, name="dup", **scopes.where(scope))
    assert _sqlstate(err) == "23505"


@pytest.mark.parametrize("scope", ["workspace", "project"])
def test_a_model_provider_over_a_workspace_or_project_connection_is_refused_with_23514(
    app_db: Database, scopes: _Scopes, scope: str
) -> None:
    with app_session(app_db, scopes.tenant) as s:
        cid = _connection(s, scopes.tenant, connection_type="model_api", **scopes.where(scope))
        s.commit()
    with app_session(app_db, scopes.tenant) as s, pytest.raises(DBAPIError) as err:
        s.execute(_INSERT_PROVIDER, {"i": uuid7(), "t": scopes.tenant, "n": "p", "c": cid})
    assert _sqlstate(err) == "23514"
    assert "a model provider needs a client-wide connection" in str(err.value.orig)


def test_a_model_provider_over_a_client_wide_connection_is_accepted(
    app_db: Database, owner_db: Database, scopes: _Scopes
) -> None:
    with app_session(app_db, scopes.tenant) as s:
        cid = _connection(s, scopes.tenant, connection_type="model_api")
        pid = uuid7()
        s.execute(_INSERT_PROVIDER, {"i": pid, "t": scopes.tenant, "n": "p", "c": cid})
        s.commit()
        assert (
            s.execute(
                text("SELECT connection_id FROM shifde.model_providers WHERE id = :i"), {"i": pid}
            ).scalar_one()
            == cid
        )
    with owner_db.session() as o:  # shifde_app cannot delete; the connection cleanup needs it gone
        o.execute(text("DELETE FROM shifde.model_providers WHERE id = :i"), {"i": pid})
        o.commit()


def test_the_guards_are_invoker_functions_with_a_pinned_path_and_no_public_execute(
    owner_db: Database,
) -> None:
    with owner_db.session() as s:
        rows = s.execute(
            text(
                "SELECT p.proname, p.prosecdef, "
                "has_function_privilege('public', p.oid, 'EXECUTE'), p.proconfig "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'shifde' AND p.proname IN "
                "('connections_guard', 'model_providers_connection_guard') ORDER BY 1"
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("connections_guard", False, False, ["search_path=pg_catalog, shifde, pg_temp"]),
        (
            "model_providers_connection_guard",
            False,
            False,
            ["search_path=pg_catalog, shifde, pg_temp"],
        ),
    ]


# --------------------------------------------------------------------------- prechecks
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


# trigger, function settings, SECURITY DEFINER, PUBLIC execute, scope-check text in the body,
# the per-client name constraint, the per-scope name index
_GUARD_STATE = (
    "SELECT pg_get_triggerdef(t.oid), p.proconfig, p.prosecdef, "
    "has_function_privilege('public', p.oid, 'EXECUTE'), "
    "p.prosrc LIKE '%secret scope must match%', "
    "EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_connections__tenant_name'), "
    "EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'uq_connections__scope_name') "
    "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
    "WHERE t.tgname = 'trg_connections_guard'"
)


def _client_with_project(c: Connection) -> tuple[UUID, UUID, UUID]:
    tenant = add_tenant(c)
    ws = add_workspace(c, tenant)
    return tenant, ws, add_project(c, tenant, ws)


def test_the_connections_precheck_refuses_an_upgrade_over_mismatched_secrets(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "cmscope1") as url:
        cfg = alembic_config(url)
        command.downgrade(cfg, "connections_0001")
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.connect() as c:  # the downgrade restores connections_0001 exactly
                assert c.execute(text(_GUARD_STATE)).one() == (
                    "CREATE TRIGGER trg_connections_guard BEFORE INSERT OR UPDATE OF "
                    "secret_ref_id, egress_policy_id, vendor_id ON shifde.connections "
                    "FOR EACH ROW EXECUTE FUNCTION shifde.connections_guard()",
                    ["search_path=pg_catalog, shifde, pg_temp"],
                    False,
                    False,
                    False,
                    True,
                    False,
                )
            names: list[str] = []
            with engine.begin() as c:
                for _ in range(2):  # one violation in each of two clients: the total is 2
                    tenant, ws, project = _client_with_project(c)
                    wrong = _secret(c, tenant, "tenant", None)
                    right = _secret(c, tenant, "project", project)
                    bad = f"bad-{uuid7().hex}"
                    names.append(bad)
                    _connection(c, tenant, name=bad, workspace_id=ws, secret_ref_id=wrong)
                    _connection(c, tenant, workspace_id=ws, project_id=project, secret_ref_id=right)
            with pytest.raises(DBAPIError) as err:
                command.upgrade(cfg, "connections_0002")
            message = str(err.value.orig).splitlines()[0]
            assert message == "connections_0002: 2 connection(s) bind a secret of another scope"
            assert not any(n in str(err.value) for n in names)
            with engine.connect() as c:  # the upgrade was refused as a whole
                assert (
                    c.execute(
                        text(
                            "SELECT count(*) FROM pg_constraint "
                            "WHERE conname = 'uq_connections__tenant_name'"
                        )
                    ).scalar_one()
                    == 1
                )
        finally:
            engine.dispose()


def test_the_model_providers_precheck_refuses_an_upgrade_over_scoped_connections(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "cmscope2") as url:
        cfg = alembic_config(url)
        command.downgrade(cfg, "models_admin_0003")
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.begin() as c:
                tenant, ws, project = _client_with_project(c)
                for where in ({"workspace_id": ws}, {"workspace_id": ws, "project_id": project}):
                    cid = _connection(c, tenant, connection_type="model_api", **where)
                    c.execute(
                        _INSERT_PROVIDER, {"i": uuid7(), "t": tenant, "n": uuid7().hex, "c": cid}
                    )
                ok = _connection(c, tenant, connection_type="model_api")
                c.execute(_INSERT_PROVIDER, {"i": uuid7(), "t": tenant, "n": "ok", "c": ok})
            with pytest.raises(DBAPIError) as err:
                command.upgrade(cfg, "models_admin_0004")
            message = str(err.value.orig).splitlines()[0]
            assert message == (
                "models_admin_0004: 2 model provider(s) use a workspace or project connection"
            )
            with engine.connect() as c:
                assert (
                    c.execute(
                        text(
                            "SELECT count(*) FROM pg_trigger "
                            "WHERE tgname = 'trg_model_providers_connection_scope'"
                        )
                    ).scalar_one()
                    == 0
                )
        finally:
            engine.dispose()


def test_both_upgrades_pass_over_rows_that_keep_the_rules(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "cmscope3") as url:
        cfg = alembic_config(url)
        command.downgrade(cfg, "connections_0001")
        command.downgrade(cfg, "models_admin_0003")
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.begin() as c:
                tenant, ws, project = _client_with_project(c)
                secret = _secret(c, tenant, "project", project)
                _connection(c, tenant, workspace_id=ws, project_id=project, secret_ref_id=secret)
                cid = _connection(c, tenant, connection_type="model_api")
                c.execute(_INSERT_PROVIDER, {"i": uuid7(), "t": tenant, "n": "ok", "c": cid})
            command.upgrade(cfg, "heads")
            with engine.connect() as c:
                assert c.execute(text(_GUARD_STATE)).one() == (
                    "CREATE TRIGGER trg_connections_guard BEFORE INSERT OR UPDATE OF "
                    "secret_ref_id, egress_policy_id, vendor_id, workspace_id, project_id "
                    "ON shifde.connections FOR EACH ROW EXECUTE FUNCTION "
                    "shifde.connections_guard()",
                    ["search_path=pg_catalog, shifde, pg_temp"],
                    False,
                    False,
                    True,
                    False,
                    True,
                )
                assert (
                    c.execute(
                        text(
                            "SELECT count(*) FROM pg_indexes "
                            "WHERE indexname = 'uq_connections__scope_name'"
                        )
                    ).scalar_one()
                    == 1
                )
        finally:
            engine.dispose()
