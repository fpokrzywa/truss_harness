"""SP6-1 cross-client isolation of the Snowflake SQL surfaces (Charter isolation rule; SP6 design
6.1): client B naming client A's Snowflake connection gets the unknown-id outcome on the invoke
route (positional `params` and `param_types`), on `tool_runtime.execute` / `invoke_once`
(`NotFound`, as `shifde_app` and as the owner with no row-level security) and at tool publish
(`connection_not_found`, equal to a random id's errors, no statement rule run). The fake records
no statement for B's attempt; each test has an A-side control that succeeds."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from shifde.adapters.dataplatforms.fakes import FakeSnowflakeAccount
from shifde.build.env import Env
from shifde.build.issues import Report
from shifde.build.tools import validate_tool
from shifde.core.db import Database
from shifde.core.errors import NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.conntests import invoke_outcome
from tests.build.conftest import EnvFactory, admins, make_env
from tests.build.test_tool_runtime_snowflake import (
    COLUMNS,
    INPUT,
    ROWS,
    SQL,
    Setup,
    fake,
    people,
    run,
    service,
    setup,
    spec,
)
from tests.connections.routes import as_user, connections_client, org
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People

__all__ = ["admins", "fake", "make_env", "people", "setup"]

POSITIONAL = {
    "sql": "select id from DEMO_DB.PUBLIC.ORDERS where customer_id = ? and ordered_on >= ?",
    "params": [1, "2026-01-01"],
    "param_types": {"1": "BIGINT", "2": "DATE"},
}


def test_cross_client_snowflake_invoke_route_with_positional_params_is_404(
    settings: Settings,
    fake: FakeSnowflakeAccount,
    setup: Setup,
    world: World,
    people: People,
) -> None:
    cid = str(setup.connection)
    with connections_client(settings) as c:
        app: Any = c.app  # Any: the FastAPI app behind the test client
        app.state.integration = dataclasses.replace(
            app.state.integration, resolver=lambda host: ["127.0.0.1"], inner=fake.transport()
        )
        hb = as_user(c, people.owner_b)
        theirs = c.post(
            f"{org(world.b.slug)}/connections/{cid}/invoke", headers=hb, json=POSITIONAL
        )
        unknown = c.post(
            f"{org(world.b.slug)}/connections/{uuid7()}/invoke", headers=hb, json=POSITIONAL
        )
        assert theirs.status_code == 404, theirs.text
        assert theirs.json()["code"] == unknown.json()["code"]
        assert fake.bodies() == [] and fake.requests == []  # nothing reached the account
        # control: A's own invoke with the same body runs on the fake
        ha = as_user(c, people.owner_a)
        status, result = invoke_outcome(
            c, ha, org(world.a.slug), cid, POSITIONAL, tenant_id=world.a.tenant
        )
    assert status == 200, result
    assert result["result"] == {
        "columns": [{"name": "ID", "type": "fixed"}],
        "rows": [["1"], ["7"]],
        "row_count": 2,
    }
    [body] = fake.bodies()
    assert body["bindings"] == {
        "1": {"type": "FIXED", "value": "1"},
        "2": {"type": "TEXT", "value": "2026-01-01"},
    }


@pytest.mark.parametrize("as_owner", [False, True], ids=["as-app-role", "as-owner-no-rls"])
@pytest.mark.parametrize("call", ["execute", "invoke_once"])
def test_cross_client_snowflake_sql_tool_run_is_refused_before_any_statement(
    call: str,
    as_owner: bool,
    setup: Setup,
    fake: FakeSnowflakeAccount,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
) -> None:
    with (
        service(
            app_db, owner_db, settings, fake, setup.tenant_b, setup.email_b, as_owner=as_owner
        ) as conns,
        pytest.raises(NotFound),
    ):
        run(conns, call, spec(setup.connection))
    assert fake.bodies() == [] and fake.requests == []
    # control: A's own principal runs the same spec
    with service(app_db, owner_db, settings, fake, setup.tenant_a, setup.email_a) as conns:
        assert run(conns, call, spec(setup.connection)) == {
            "columns": COLUMNS,
            "rows": ROWS,
            "row_count": 2,
        }
    assert len(fake.bodies()) == 1


def _tool(connection: UUID, sql: str) -> dict[str, Any]:
    return {
        "executor": "connection.sql",
        "binding": {"connection_id": str(connection), "sql_template": sql},
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


def _errors(env: Env, tool: dict[str, Any]) -> list[tuple[str, str]]:
    report = Report()
    validate_tool(deepcopy(tool), env, report)
    return [(e.path, e.code) for e in report.errors]


def _app(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    return app_session(app_db, tenant)


def _owner(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    return owner_db.session()


@pytest.fixture(params=[_app, _owner], ids=["as-app-role", "as-owner-no-rls"])
def open_session(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], AbstractContextManager[Session]]:
    opener = request.param
    return lambda tenant: opener(app_db, owner_db, tenant)


def test_cross_client_snowflake_tool_publish_is_connection_not_found(
    setup: Setup,
    fake: FakeSnowflakeAccount,
    open_session: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    refused = "select * from t where a = ?"  # would be sql_positional_parameter on a known one
    with open_session(setup.tenant_b) as s:
        env = make_env(s, setup.tenant_b)
        assert env.connection(setup.connection) is None
        theirs = _errors(env, _tool(setup.connection, refused))
        unknown = _errors(env, _tool(uuid7(), refused))
        assert theirs == unknown
        assert ("binding.connection_id", "connection_not_found") in theirs
        assert not any(path == "binding.sql_template" for path, _ in theirs)
        s.rollback()
    assert fake.bodies() == []
    with open_session(setup.tenant_a) as s:  # control: A publishes on its own connection
        env = make_env(s, setup.tenant_a)
        assert _errors(env, _tool(setup.connection, SQL)) == []
        assert ("binding.sql_template", "sql_positional_parameter") in _errors(
            env, _tool(setup.connection, refused)
        )
        s.rollback()
