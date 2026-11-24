"""SP6-1: `connection.sql` tools on a Snowflake connection through `tool_runtime` (SP6 design 6.1):
`positional_sql`, the request shape per adapter key (Snowflake: positional `?`, values in
occurrence order, types keyed by position; Databricks and postgres unchanged), and a tool run end
to end against `FakeSnowflakeAccount(sample_data=True)`: the connection is made and activated
through the routes, and the `ConnectionService` reaches the fake through the runtime's
`resolver`/`inner` hooks."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.adapters.dataplatforms.fakes import FakeSnowflakeAccount
from shifde.adapters.runtime import IntegrationRuntime
from shifde.authz.service import new_pdp
from shifde.build import tool_runtime
from shifde.build.tools import named_sql, positional_sql
from shifde.connections.service import ConnectionService
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.settings import Settings
from shifde.tenancy.scope import TenantScope
from tests.build.conftest import World
from tests.connections.conftest import people
from tests.connections.routes import (
    REGISTRY,
    activate,
    as_user,
    connection,
    connections_client,
    org,
    secret,
)
from tests.tenancy.conftest import app_session
from tests.tenancy.routes import People

__all__ = ["people"]

TOKEN = "sf-runtime-test-pat-0001"  # scan-secrets: allow, the fake's accepted token
SQL = (
    "select o.id, o.amount, o.ordered_on from DEMO_DB.PUBLIC.ORDERS o "
    "where o.customer_id = %(customer_id)s and o.ordered_on >= %(since)s order by o.id"
)
INPUT = {
    "type": "object",
    "required": ["customer_id", "since"],
    "properties": {
        "customer_id": {"type": "integer"},
        "since": {"type": "string", "format": "date"},
    },
}
ARGS = {"customer_id": 1, "since": "2026-01-01"}
GENEROUS = 60
COLUMNS = [
    {"name": "ID", "type": "fixed"},
    {"name": "AMOUNT", "type": "real"},
    {"name": "ORDERED_ON", "type": "date"},
]
ROWS = [["1", "25.5", "2026-01-11"], ["7", "175.5", "2026-02-17"]]


@pytest.fixture
def fake() -> FakeSnowflakeAccount:
    return FakeSnowflakeAccount(account="acct1", user="reader", pat=TOKEN, sample_data=True)


@dataclass(frozen=True)
class Setup:
    tenant_a: UUID
    tenant_b: UUID
    email_a: str
    email_b: str
    connection: UUID


@pytest.fixture
def setup(
    settings: Settings,
    fake: FakeSnowflakeAccount,
    world: World,
    people: People,
    app_db: Database,
) -> Setup:
    """Client A's active Snowflake read connection on the fake, made through the routes."""
    with connections_client(settings) as c:
        app: Any = c.app  # Any: the FastAPI app behind the test client
        app.state.integration = dataclasses.replace(
            app.state.integration, resolver=lambda host: ["127.0.0.1"], inner=fake.transport()
        )
        A, h = org(world.a.slug), as_user(c, people.owner_a)
        sid = secret(app_db, settings, world.a.tenant, TOKEN)
        made = connection(
            c,
            h,
            {
                "name": "warehouse",
                "connection_type": "data_platform",
                "adapter_key": "snowflake",
                "config": {"account": "acct1", "user": "reader", "warehouse": "COMPUTE_WH"},
                "auth_method": "bearer",
                "secret_id": sid,
            },
            A,
        )
        activate(c, h, made["id"], A)  # the worker's test job, against the fake
    fake.requests.clear()
    return Setup(world.a.tenant, world.b.tenant, people.owner_a, people.owner_b, UUID(made["id"]))


@contextmanager
def service(
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    fake: FakeSnowflakeAccount,
    tenant: UUID,
    email: str,
    *,
    as_owner: bool = False,
) -> Iterator[ConnectionService]:
    """A `ConnectionService` of `email` in `tenant` (as `shifde_app`, or as the owner with no
    row-level security) whose runtime reaches the fake."""
    with owner_db.session() as o:
        user = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    principal = Principal(user_id=user, email=email, display_name=email)
    opened = owner_db.session() if as_owner else app_session(app_db, tenant, user)
    with opened as s:
        if as_owner:
            set_tenant(s, tenant)
            set_principal(s, user)
        scope = TenantScope(
            session=s,
            ctx=RequestContext(request_id="r-sf-tool", principal=principal, tenant_id=tenant),
            principal=principal,
            tenant_id=tenant,
            tenant_slug="x",
            tenant_name="x",
            tenant_status="active",
            pdp=new_pdp(s, principal, tenant),
        )
        runtime = IntegrationRuntime(
            settings=settings,
            registry=REGISTRY,
            resolver=lambda host: ["127.0.0.1"],
            inner=fake.transport(),
        )
        yield ConnectionService(scope, runtime)


def spec(connection_id: UUID, **binding: Any) -> dict[str, Any]:
    return {
        "executor": "connection.sql",
        "binding": {"connection_id": str(connection_id), "sql_template": SQL, **binding},
        "input_schema": INPUT,
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "timeout_s": GENEROUS,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "idempotency": "none",
    }


def run(conns: ConnectionService, call: str, tool: dict[str, Any]) -> Any:
    if call == "execute":
        return tool_runtime.execute(conns, tool, ARGS, purpose="t", idempotency_key="k").output
    return tool_runtime.invoke_once(
        conns, tool, ARGS, purpose="t", idempotency_key="k", deadline=GENEROUS
    )


def test_positional_sql_binds_outside_literals_only() -> None:
    template = (
        "select * from DEMO_DB.PUBLIC.ORDERS where customer_id = %(c)s and note <> '%(c)s' "
        "and id > %(c)s and x like 'a%%'"
    )
    sql, names = positional_sql(template)
    assert sql == (
        "select * from DEMO_DB.PUBLIC.ORDERS where customer_id = ? and note <> ':c' "
        "and id > ? and x like 'a%'"
    )
    assert names == ["c", "c"]


@pytest.mark.parametrize(
    ("template", "sql", "names"),
    [
        ("select %(a)s, %(b)s, %(a)s", "select ?, ?, ?", ["a", "b", "a"]),
        ("select $$%(a)s 50%%$$, %(b)s", "select $$:a 50%$$, ?", ["b"]),
        ("select 'it''s %(a)s', %(b)s", "select 'it''s :a', ?", ["b"]),
        ("select 'a\\' %(a)s', %(b)s", "select 'a\\' :a', ?", ["b"]),
        ("select 100%% as p, %(b)s", "select 100% as p, ?", ["b"]),
        ("select '%(a)s", "select ':a", []),  # an unclosed literal runs to the end
        ("select %(a)", "select %(a)", []),
    ],
)
def test_positional_sql_cases(template: str, sql: str, names: list[str]) -> None:
    assert positional_sql(template) == (sql, names)


def test_positional_sql_keeps_the_literal_text_named_sql_gives() -> None:
    template = "select 'x %(a)s %%' as l, $$%(b)s$$ as m from t where c = %(c)s"
    sql, _ = positional_sql(template)
    assert sql.replace("?", ":c") == named_sql(template)


def test_snowflake_sql_request_is_positional_with_types_by_position() -> None:
    binding = {
        "sql_template": SQL + " and o.customer_id <> %(customer_id)s and o.note like '5%%'",
        "max_rows": 25,
    }
    request = tool_runtime.sql_request(binding, ARGS, INPUT, "data_platform", "snowflake")
    assert request == {
        "sql": (
            "select o.id, o.amount, o.ordered_on from DEMO_DB.PUBLIC.ORDERS o "
            "where o.customer_id = ? and o.ordered_on >= ? order by o.id "
            "and o.customer_id <> ? and o.note like '5%'"
        ),
        "params": [1, "2026-01-01", 1],
        "param_types": {"1": "BIGINT", "2": "DATE", "3": "BIGINT"},
        "max_rows": 25,
    }
    assert tool_runtime.sql_request(
        {"sql_template": "select 1"}, {}, {}, "data_platform", "snowflake"
    ) == {
        "sql": "select 1",
        "params": [],
        "param_types": {},
        "max_rows": 1000,
    }


def test_the_databricks_and_postgres_sql_requests_are_unchanged_by_the_adapter_key() -> None:
    binding = {"sql_template": SQL + " and o.note like '5%%'", "max_rows": 25}
    databricks = tool_runtime.sql_request(binding, ARGS, INPUT, "data_platform", "databricks")
    assert databricks == tool_runtime.sql_request(binding, ARGS, INPUT, "data_platform")
    assert databricks == {
        "sql": named_sql(binding["sql_template"]),
        "params": ARGS,
        "param_types": {"customer_id": "BIGINT", "since": "DATE"},
        "max_rows": 25,
    }
    for key in ("postgres", "snowflake"):  # the connection type decides first
        assert tool_runtime.sql_request(binding, ARGS, INPUT, "database", key) == {
            "sql": binding["sql_template"],
            "params": ARGS,
        }


@pytest.mark.parametrize("call", ["execute", "invoke_once"])
def test_a_snowflake_sql_tool_sends_positional_bindings_and_returns_typed_sample_rows(
    call: str,
    setup: Setup,
    fake: FakeSnowflakeAccount,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    with service(app_db, owner_db, settings, fake, setup.tenant_a, setup.email_a) as conns:
        out = run(conns, call, spec(setup.connection, max_rows=50))
    [body] = fake.bodies()
    assert "%(" not in body["statement"] and body["statement"].count("?") == 2
    assert body["bindings"] == {
        "1": {"type": "FIXED", "value": "1"},
        "2": {"type": "TEXT", "value": "2026-01-01"},
    }
    assert out == {"columns": COLUMNS, "rows": ROWS, "row_count": 2}


def test_the_snowflake_binding_max_rows_caps_and_marks_truncation(
    setup: Setup,
    fake: FakeSnowflakeAccount,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    with service(app_db, owner_db, settings, fake, setup.tenant_a, setup.email_a) as conns:
        out = run(conns, "execute", spec(setup.connection, max_rows=1))
    assert out == {"columns": COLUMNS, "rows": ROWS[:1], "row_count": 1, "truncated": True}
