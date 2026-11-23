"""SP5-1: publish-time validation of `connection.sql` tools on a Databricks `data_platform`
connection (design 6.1): such a tool publishes, its derived maturity is the Databricks adapter's,
the SELECT-only statement rules of `build/sqlrules.py` run on its template, a Snowflake connection
is `executor_adapter_unsupported`, postgres tools validate as before, and client B's Databricks
connection is the unknown-id outcome. With `RepoEnv` and the production `Env`, as `shifde_app` and
as the schema owner (no row-level security)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build.env import Env
from shifde.build.issues import Report
from shifde.build.tools import named_sql, validate_tool
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.build.conftest import EnvFactory, World
from tests.build.env_support import Overriding, RepoEnv
from tests.connections.conftest import connection_values
from tests.tenancy.conftest import app_session

DATABRICKS = {"host": "https://ws1.cloud.databricks.com", "warehouse_id": "wh-0001"}
SNOWFLAKE = {"account": "acct1", "user": "reader"}
INPUT = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "since": {"type": "string"}},
}


@dataclass(frozen=True)
class Conns:
    databricks: UUID
    snowflake: UUID
    postgres: UUID
    theirs: UUID  # client B's Databricks connection


def _insert(o: Session, tenant: UUID, **over: Any) -> UUID:
    values = connection_values(tenant, **over)
    conn_repo.insert_connection(o, values)
    o.execute(
        text("UPDATE shifde.connections SET status = 'active' WHERE id = :i"), {"i": values["id"]}
    )
    return UUID(str(values["id"]))


def _databricks(o: Session, tenant: UUID) -> UUID:
    return _insert(
        o, tenant, connection_type="data_platform", adapter_key="databricks", config=DATABRICKS
    )


@pytest.fixture
def conns(owner_db: Database, world: World) -> Conns:
    with owner_db.session() as o:
        out = Conns(
            databricks=_databricks(o, world.a.tenant),
            snowflake=_insert(
                o,
                world.a.tenant,
                connection_type="data_platform",
                adapter_key="snowflake",
                config=SNOWFLAKE,
            ),
            postgres=_insert(
                o,
                world.a.tenant,
                connection_type="database",
                adapter_key="postgres",
                config={"host": "db.example.com", "database": "app"},
            ),
            theirs=_databricks(o, world.b.tenant),
        )
        o.commit()
    return out


Opener = Callable[[Database, Database, UUID], AbstractContextManager[Session]]


def _app(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    return app_session(app_db, tenant)


def _owner(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    return owner_db.session()


@pytest.fixture(params=[_app, _owner], ids=["as-app-role", "as-owner-no-rls"])
def open_session(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], AbstractContextManager[Session]]:
    opener: Opener = request.param
    return lambda tenant: opener(app_db, owner_db, tenant)


def _sql_tool(connection: UUID, sql: str, side: str = "read", **binding: Any) -> dict[str, Any]:
    return {
        "executor": "connection.sql",
        "binding": {"connection_id": str(connection), "sql_template": sql, **binding},
        "input_schema": INPUT,
        "output_schema": {"type": "object"},
        "side_effect_class": side,
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 10,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }


def _errors(env: Env, spec: dict[str, Any]) -> list[tuple[str, str]]:
    report = Report()
    validate_tool(deepcopy(spec), env, report)
    return [(e.path, e.code) for e in report.errors]


READ_SQL = (
    "select o.id, o.amount from main.default.orders o "
    "where o.customer_id = %(customer_id)s and o.ordered_on >= %(since)s and o.note like '50%%'"
)


def test_named_sql_converts_parameters_and_percent_escapes() -> None:
    assert named_sql(READ_SQL).endswith(
        "o.customer_id = :customer_id and o.ordered_on >= :since and o.note like '50%'"
    )


def test_a_read_sql_tool_on_an_active_databricks_read_connection_publishes(
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        info = env.connection(conns.databricks)
        assert info is not None
        assert (info.connection_type, info.access_mode) == ("data_platform", "read")
        assert _errors(env, _sql_tool(conns.databricks, READ_SQL, max_rows=200)) == []
        s.rollback()


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("delete from main.default.orders", "sql_write_statement"),
        ("select 1 from main.default.orders -- trailing", "sql_comment"),
        ("select 1 /* note */ from main.default.orders", "sql_comment"),
        ("select 1; select 2", "sql_multiple_statements"),
        ("show tables", "sql_not_select"),
    ],
)
def test_the_statement_rules_run_at_publish_on_a_databricks_sql_tool(
    sql: str,
    code: str,
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        errors = _errors(env, _sql_tool(conns.databricks, sql))
        assert ("binding.sql_template", code) in errors, errors
        s.rollback()


def test_a_write_keyword_is_refused_whatever_the_side_effect_class(
    conns: Conns, world: World, owner_db: Database, make_env: EnvFactory
) -> None:
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.connections SET access_mode = 'read_write' WHERE id = :i"),
            {"i": conns.databricks},
        )
        o.commit()
    with owner_db.session() as s:
        env = make_env(s, world.a.tenant)
        for side in ("read", "write_idempotent"):
            spec = _sql_tool(conns.databricks, "delete from main.default.orders", side)
            if side == "write_idempotent":
                spec["idempotency"] = "key_supported"
            assert ("binding.sql_template", "sql_write_statement") in _errors(env, spec), side
        s.rollback()


def test_a_snowflake_sql_tool_is_executor_adapter_unsupported(
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        report = Report()
        validate_tool(_sql_tool(conns.snowflake, "select 1"), env, report)
        assert [(e.path, e.code, e.message) for e in report.errors] == [
            (
                "binding.connection_id",
                "executor_adapter_unsupported",
                "connection.sql supports Databricks data platform connections only",
            )
        ]
        s.rollback()


class _Maturity:
    """An `Env` whose platform adapter maturity is fixed (the executor's stays `inner`'s)."""

    def __init__(self, inner: Env, level: str) -> None:
        self.inner = Overriding(inner)
        self.level = level
        self.asked: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def adapter_maturity(self, adapter_key: str, capability: str) -> str:
        self.asked.append((adapter_key, capability))
        return self.level


@pytest.mark.parametrize(("level", "planned"), [("planned", True), ("experimental", False)])
def test_the_databricks_tool_inherits_the_databricks_adapters_maturity(
    level: str, planned: bool, conns: Conns, world: World, owner_db: Database
) -> None:
    with owner_db.session() as s:
        env = _Maturity(RepoEnv(s, world.a.tenant), level)
        errors = _errors(env, _sql_tool(conns.databricks, "select 1"))  # type: ignore[arg-type]
        assert (("executor", "executor_planned") in errors) is planned
        assert env.asked == [("databricks", "query.sql")]
        env.asked.clear()
        _errors(env, _sql_tool(conns.postgres, "select 1"))  # type: ignore[arg-type]
        assert env.asked == []  # a postgres tool keeps the executor's own maturity
        s.rollback()


def test_the_production_env_derives_the_databricks_maturity_as_experimental(
    conns: Conns, world: World, owner_db: Database, make_env: EnvFactory
) -> None:
    with owner_db.session() as s:
        env = make_env(s, world.a.tenant)
        expected = "ready" if isinstance(env, RepoEnv) else "experimental"  # RepoEnv: fixed
        assert env.adapter_maturity("databricks", "query.sql") == expected
        s.rollback()


@pytest.mark.parametrize(
    ("sql", "side", "expected"),
    [
        ("select 1 -- a comment", "read", []),
        ("delete from t", "read", [("side_effect_class", "write_statement_requires_write_class")]),
        ("select 1; select 2", "read", [("binding.sql_template", "invalid_template")]),
        ("select %(nope)s", "read", [("binding", "unknown_argument")]),
        ("select * from orders where id = %(customer_id)s", "read", []),
    ],
)
def test_a_postgres_sql_tools_validation_is_unchanged(
    sql: str,
    side: str,
    expected: list[tuple[str, str]],
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        assert _errors(env, _sql_tool(conns.postgres, sql, side)) == expected
        s.rollback()


def test_client_bs_databricks_connection_gets_the_unknown_id_outcome(
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        assert env.connection(conns.theirs) is None
        theirs = _errors(env, _sql_tool(conns.theirs, "delete from main.default.orders"))
        unknown = _errors(env, _sql_tool(uuid7(), "delete from main.default.orders"))
        assert theirs == unknown
        assert ("binding.connection_id", "connection_not_found") in theirs
        # the statement rules never ran on another client's connection: no data_platform info
        assert not any(path == "binding.sql_template" for path, _ in theirs)
        assert _errors(env, _sql_tool(conns.databricks, "select 1")) == []  # control
        s.rollback()
