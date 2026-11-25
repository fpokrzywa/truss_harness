"""SP6-1: publish-time validation of `connection.sql` tools on a Snowflake `data_platform` connection
(SP6 design 6.1): such a tool publishes, its derived maturity is the Snowflake adapter's
`query.sql`, the statement rules run in the Snowflake dialect (the dialect follows the
connection's adapter key), and an adapter key outside `DATA_PLATFORM_ADAPTER_OF` is
`executor_adapter_unsupported`. With `RepoEnv` and the production `Env`, as `shifde_app` and as the
schema owner (no row-level security)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.base import AdapterFailure
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.adapters.dataplatforms.sqlapi import check_read_only
from shifde.build.env import Env
from shifde.build.executors import DATA_PLATFORM_ADAPTER_OF
from shifde.build.issues import Report
from shifde.build.tools import positional_sql, validate_tool
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from tests.build.conftest import EnvFactory, World
from tests.build.env_support import Overriding, RepoEnv
from tests.connections.conftest import connection_values
from tests.tenancy.conftest import app_session

SNOWFLAKE = {"account": "acct1", "user": "reader"}
DATABRICKS = {"host": "https://ws1.cloud.databricks.com", "warehouse_id": "wh-0001"}
INPUT = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "since": {"type": "string"}},
}
READ_SQL = (
    "select o.id, o.amount from DEMO_DB.PUBLIC.ORDERS o where o.customer_id = %(customer_id)s "
    "and o.ordered_on >= %(since)s and o.note like '50%%' and o.memo <> $$%(since)s$$"
)


@dataclass(frozen=True)
class Conns:
    snowflake: UUID
    databricks: UUID


def _insert(o: Session, tenant: UUID, adapter_key: str, config: dict[str, Any]) -> UUID:
    values = connection_values(
        tenant, connection_type="data_platform", adapter_key=adapter_key, config=config
    )
    conn_repo.insert_connection(o, values)
    o.execute(
        text("UPDATE shifde.connections SET status = 'active' WHERE id = :i"), {"i": values["id"]}
    )
    return UUID(str(values["id"]))


@pytest.fixture
def conns(owner_db: Database, world: World) -> Conns:
    with owner_db.session() as o:
        out = Conns(
            snowflake=_insert(o, world.a.tenant, "snowflake", SNOWFLAKE),
            databricks=_insert(o, world.a.tenant, "databricks", DATABRICKS),
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


def _sql_tool(connection: UUID, sql: str, **binding: Any) -> dict[str, Any]:
    return {
        "executor": "connection.sql",
        "binding": {"connection_id": str(connection), "sql_template": sql, **binding},
        "input_schema": INPUT,
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
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


def test_the_snowflake_adapter_registers_query_sql() -> None:
    assert "query.sql" in SnowflakeAdapter.descriptor.capabilities
    assert DATA_PLATFORM_ADAPTER_OF["snowflake"] == ("snowflake", "query.sql")


def test_a_read_sql_tool_on_an_active_snowflake_read_connection_publishes(
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        info = env.connection(conns.snowflake)
        assert info is not None
        assert (info.connection_type, info.adapter_key, info.access_mode) == (
            "data_platform",
            "snowflake",
            "read",
        )
        assert _errors(env, _sql_tool(conns.snowflake, READ_SQL, max_rows=200)) == []
        s.rollback()


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("select * from t where a = ?", "sql_positional_parameter"),
        ("select :1", "sql_positional_bind"),
        ('select "Demo" from DEMO_DB.PUBLIC.X', "sql_quoted_identifier_unsupported"),
        ("select `a` from DEMO_DB.PUBLIC.X", "sql_syntax"),
        ("select 1 from DEMO_DB.PUBLIC.X // note", "sql_comment"),
        ("commit", "sql_write_statement"),
        ("select 1 from DEMO_DB.PUBLIC.X where x = 1 or delete", "sql_write_statement"),
        ("select 1; select 2", "sql_multiple_statements"),
        ("select a$$b$$ from DEMO_DB.PUBLIC.X", "sql_syntax"),
        # the rules shared with the runtime check (#358)
        ("select system$wait(5)", "sql_system_function"),
        ('select "SYSTEM$WAIT"(5)', "sql_system_function"),
        ("select * from identifier(%(customer_id)s||'x')", "sql_dynamic_name"),
        ("select * from identifier(:region||'x')", "sql_dynamic_name"),
        ("select * from table(:region)", "sql_dynamic_name"),
        (
            "with p as procedure () returns varchar language sql as $$ select 1 $$ select 1",
            "sql_write_statement",
        ),
        ("select 1 ->> execute immediate 'drop table x'", "sql_flow_operator"),
        # (#375)
        ("select $1 from @s/it's ->> remove @s/o'x", "sql_stage_path"),
        ("select get_ddl('table', 't')", "sql_refused_function"),
        ("select seq.nextval", "sql_refused_function"),
        (
            "select o.id from DEMO_DB.PUBLIC.ORDERS o where o.id = %(customer_id)s_x",
            "invalid_template",
        ),
    ],
)
def test_the_snowflake_statement_rules_run_at_publish(
    sql: str,
    code: str,
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = make_env(s, world.a.tenant)
        errors = _errors(env, _sql_tool(conns.snowflake, sql))
        assert ("binding.sql_template", code) in errors, errors
        s.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "select comment from DEMO_DB.PUBLIC.T where id = %(customer_id)s",
        'select "ID" from "DEMO_DB"."PUBLIC"."ORDERS" where note <> $$a delete$$',
        "select ':1'",
        "select x::int from DEMO_DB.PUBLIC.T",
        "select :id from DEMO_DB.PUBLIC.T",
    ],
)
def test_snowflake_only_text_publishes_on_snowflake(
    sql: str,
    conns: Conns,
    world: World,
    owner_db: Database,
    make_env: EnvFactory,
) -> None:
    with owner_db.session() as s:
        env = make_env(s, world.a.tenant)
        errors = _errors(env, _sql_tool(conns.snowflake, sql))
        assert not any(path == "binding.sql_template" for path, _ in errors), errors
        s.rollback()


# (template, code, message) of a raw `:word` the run-time check reads as code (#375)
RAW_WORDS = [
    ("select :call", "sql_write_statement", "CALL is not allowed in a read statement"),
    ("select src:drop from t", "sql_write_statement", "DROP is not allowed in a read statement"),
    ("select :Delete", "sql_write_statement", "DELETE is not allowed in a read statement"),
    ("select :system$x", "sql_system_function", "system functions are not allowed"),
    (
        "select :nextval",
        "sql_refused_function",
        "GET_DDL, GET_PRESIGNED_URL and NEXTVAL are not allowed",
    ),
    (
        "select :identifier(%(customer_id)s||'x')",
        "sql_dynamic_name",
        "IDENTIFIER(...) and TABLE(...) must hold one plain string literal or name",
    ),
    # reported once: the statement rules already give the same issue
    ("select call, :call", "sql_write_statement", "CALL is not allowed in a read statement"),
    ("select :nextval, nextval", "sql_refused_function", None),
]


@pytest.mark.parametrize(("sql", "code", "message"), RAW_WORDS)
def test_a_raw_word_the_run_time_check_refuses_is_refused_at_publish(
    sql: str,
    code: str,
    message: str | None,
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with pytest.raises(AdapterFailure):
        check_read_only(positional_sql(sql)[0], dialect="snowflake")
    with open_session(world.a.tenant) as s:
        report = Report()
        validate_tool(_sql_tool(conns.snowflake, sql), make_env(s, world.a.tenant), report)
        found = [e for e in report.errors if e.path == "binding.sql_template"]
        assert [e.code for e in found] == [code]
        assert message is None or found[0].message == message
        s.rollback()


def test_a_parameter_named_as_a_refused_word_publishes(
    conns: Conns, world: World, owner_db: Database, make_env: EnvFactory
) -> None:
    sql = "select %(call)s, %(drop)s, %(nextval)s"
    spec = _sql_tool(conns.snowflake, sql)
    spec["input_schema"] = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in ("call", "drop", "nextval")},
    }
    check_read_only(positional_sql(sql)[0], dialect="snowflake")
    with owner_db.session() as s:
        env = make_env(s, world.a.tenant)
        assert _errors(env, spec) == []
        for other in ("select :id", "select src:customer from t"):
            assert _errors(env, _sql_tool(conns.snowflake, other)) == []
        # the Databricks dialect has no raw word rule
        assert _errors(env, _sql_tool(conns.databricks, "select :call")) == []
        s.rollback()


def test_the_dialect_follows_the_connections_adapter_key(
    conns: Conns, world: World, owner_db: Database, make_env: EnvFactory
) -> None:
    with owner_db.session() as s:
        env = make_env(s, world.a.tenant)
        positional = "select * from main.default.orders where a = ?"
        assert ("binding.sql_template", "sql_positional_parameter") in _errors(
            env, _sql_tool(conns.snowflake, positional)
        )
        assert _errors(env, _sql_tool(conns.databricks, positional)) == []  # Databricks dialect
        dollars = "select $$ delete $$ as s"
        assert _errors(env, _sql_tool(conns.snowflake, dollars)) == []
        assert ("binding.sql_template", "sql_write_statement") in _errors(
            env, _sql_tool(conns.databricks, dollars)
        )
        bind = "select :1 from main.default.orders"
        assert ("binding.sql_template", "sql_positional_bind") not in _errors(
            env, _sql_tool(conns.databricks, bind)
        )
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
def test_the_snowflake_tool_inherits_the_snowflake_adapters_maturity(
    level: str, planned: bool, conns: Conns, world: World, owner_db: Database
) -> None:
    with owner_db.session() as s:
        env = _Maturity(RepoEnv(s, world.a.tenant), level)
        errors = _errors(env, _sql_tool(conns.snowflake, "select 1"))  # type: ignore[arg-type]
        assert (("executor", "executor_planned") in errors) is planned
        assert env.asked == [("snowflake", "query.sql")]
        s.rollback()


def test_the_production_env_derives_the_snowflake_maturity_as_experimental(
    conns: Conns, world: World, owner_db: Database, make_env: EnvFactory
) -> None:
    with owner_db.session() as s:
        env = make_env(s, world.a.tenant)
        expected = "ready" if isinstance(env, RepoEnv) else "experimental"  # RepoEnv: fixed
        assert env.adapter_maturity("snowflake", "query.sql") == expected
        s.rollback()


class _AdapterKey:
    """An `Env` whose connections report another `adapter_key`."""

    def __init__(self, inner: Env, adapter_key: str) -> None:
        self.inner = Overriding(inner)
        self.adapter_key = adapter_key

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def connection(self, connection_id: UUID) -> Any:  # Any: ConnectionInfo | None
        info = self.inner.connection(connection_id)
        return None if info is None else replace(info, adapter_key=self.adapter_key)


@pytest.mark.parametrize("adapter_key", ["bigquery", "redshift", "Snowflake"])
def test_another_data_platform_adapter_key_is_executor_adapter_unsupported(
    adapter_key: str,
    conns: Conns,
    world: World,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    with open_session(world.a.tenant) as s:
        env = _AdapterKey(make_env(s, world.a.tenant), adapter_key)
        report = Report()
        validate_tool(_sql_tool(conns.snowflake, "select 1"), env, report)  # type: ignore[arg-type]
        assert [(e.path, e.code, e.message) for e in report.errors] == [
            (
                "binding.connection_id",
                "executor_adapter_unsupported",
                "connection.sql supports Databricks and Snowflake data platform connections only",
            )
        ]
        s.rollback()
