"""SP5-1: `connection.sql` tools on a Databricks connection through `tool_runtime.execute` /
`invoke_once` (design 6.1). The connection is made and activated through the routes, and the
`ConnectionService` reaches `FakeDatabricksWorkspace(sample_data=True)` through the runtime's
`resolver`/`inner` hooks. The request sent is `:name` with typed parameters and the binding's
`max_rows`; the request form follows the type of the row `open_for_use` re-read; a postgres-bound
request is unchanged; and client B's principal naming A's Databricks connection id is refused
before any statement reaches the workspace."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace
from shifde.adapters.runtime import IntegrationRuntime
from shifde.authz.service import new_pdp
from shifde.build import tool_runtime
from shifde.connections.service import ConnectionService
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.errors import NotFound
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

HOST = "ws1.cloud.databricks.com"
STATEMENTS = ("POST", "/api/2.0/sql/statements")
TOKEN = "dbx-runtime-test-pat-0001"  # scan-secrets: allow, any non-empty token passes the fake
SQL = (
    "select o.id, o.amount, o.ordered_on from main.default.orders o "
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


@pytest.fixture
def fake() -> FakeDatabricksWorkspace:
    return FakeDatabricksWorkspace(host=HOST, warehouse_id="wh-0001", sample_data=True)


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
    fake: FakeDatabricksWorkspace,
    world: World,
    people: People,
    app_db: Database,
) -> Setup:
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
                "name": "lakehouse",
                "connection_type": "data_platform",
                "adapter_key": "databricks",
                "config": {"host": f"https://{HOST}", "warehouse_id": "wh-0001"},
                "auth_method": "bearer",
                "secret_id": sid,
            },
            A,
        )
        activate(c, h, made["id"], A)  # the worker's test job, against the fake
    fake.requests.clear()
    return Setup(world.a.tenant, world.b.tenant, people.owner_a, people.owner_b, UUID(made["id"]))


@contextmanager
def _service(
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    fake: FakeDatabricksWorkspace,
    tenant: UUID,
    email: str,
    *,
    as_owner: bool = False,
) -> Iterator[ConnectionService]:
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
            ctx=RequestContext(request_id="r-dbx-tool", principal=principal, tenant_id=tenant),
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


def _spec(connection_id: UUID, **binding: Any) -> dict[str, Any]:
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


def _run(conns: ConnectionService, call: str, spec: dict[str, Any]) -> Any:
    if call == "execute":
        return tool_runtime.execute(conns, spec, ARGS, purpose="t", idempotency_key="k").output
    return tool_runtime.invoke_once(
        conns, spec, ARGS, purpose="t", idempotency_key="k", deadline=GENEROUS
    )


def _statements(fake: FakeDatabricksWorkspace) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in fake.requests if (r.method, r.url.path) == STATEMENTS]


def test_sql_request_shapes_per_connection_type() -> None:
    binding = {"sql_template": SQL + " and o.note like '5%%'", "max_rows": 25}
    platform = tool_runtime.sql_request(binding, ARGS, INPUT, "data_platform")
    assert "%(" not in platform["sql"] and platform["sql"].endswith("like '5%'")
    assert ":customer_id" in platform["sql"] and ":since" in platform["sql"]
    assert platform["params"] == ARGS
    assert platform["param_types"] == {"customer_id": "BIGINT", "since": "DATE"}
    assert platform["max_rows"] == 25
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "number"},
            "b": {"type": "boolean"},
            "s": {"type": "string"},
            "o": {"type": "object"},
        },
    }
    typed = tool_runtime.sql_request(
        {"sql_template": "select %(n)s, %(b)s, %(s)s, %(o)s, %(missing)s"},
        {"n": 1.5, "b": True, "s": "x", "o": {}},
        schema,
        "data_platform",
    )
    assert typed["param_types"] == {
        "n": "DOUBLE",
        "b": "BOOLEAN",
        "s": "STRING",
        "o": "STRING",
        "missing": "STRING",
    }
    assert typed["max_rows"] == 1000  # the binding's default
    # a postgres-bound request is unchanged: the %(name)s template and the params only
    assert tool_runtime.sql_request(binding, ARGS, INPUT, "database") == {
        "sql": binding["sql_template"],
        "params": ARGS,
    }


@pytest.mark.parametrize("call", ["execute", "invoke_once"])
def test_a_databricks_sql_tool_sends_typed_named_parameters_and_returns_sample_rows(
    call: str,
    setup: Setup,
    fake: FakeDatabricksWorkspace,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    with _service(app_db, owner_db, settings, fake, setup.tenant_a, setup.email_a) as conns:
        out = _run(conns, call, _spec(setup.connection, max_rows=50))
    [body] = _statements(fake)
    assert "%(" not in body["statement"]
    assert ":customer_id" in body["statement"] and ":since" in body["statement"]
    assert sorted(body["parameters"], key=lambda p: p["name"]) == [
        {"name": "customer_id", "value": "1", "type": "BIGINT"},
        {"name": "since", "value": "2026-01-01", "type": "DATE"},
    ]
    assert body["row_limit"] == 51  # the binding's max_rows, plus the truncation probe
    assert out == {
        "columns": ["id", "amount", "ordered_on"],
        "rows": [["1", "25.5", "2026-01-11"], ["7", "175.5", "2026-02-17"]],
        "row_count": 2,
    }


def test_the_binding_max_rows_caps_and_marks_truncation(
    setup: Setup,
    fake: FakeDatabricksWorkspace,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    with _service(app_db, owner_db, settings, fake, setup.tenant_a, setup.email_a) as conns:
        out = _run(conns, "execute", _spec(setup.connection, max_rows=1))
    assert _statements(fake)[0]["row_limit"] == 2
    assert out["rows"] == [["1", "25.5", "2026-01-11"]] and out["truncated"] is True


@pytest.mark.parametrize("as_owner", [False, True], ids=["as-app-role", "as-owner-no-rls"])
@pytest.mark.parametrize("call", ["execute", "invoke_once"])
def test_client_b_naming_as_databricks_connection_is_refused_before_any_statement(
    call: str,
    as_owner: bool,
    setup: Setup,
    fake: FakeDatabricksWorkspace,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    with (
        _service(
            app_db, owner_db, settings, fake, setup.tenant_b, setup.email_b, as_owner=as_owner
        ) as conns,
        pytest.raises(NotFound),
    ):
        _run(conns, call, _spec(setup.connection))
    assert _statements(fake) == [] and fake.requests == []  # nothing reached the workspace
    # control: A's own principal runs the same spec
    with _service(app_db, owner_db, settings, fake, setup.tenant_a, setup.email_a) as conns:
        assert _run(conns, call, _spec(setup.connection))["row_count"] == 2
    assert len(_statements(fake)) == 1
