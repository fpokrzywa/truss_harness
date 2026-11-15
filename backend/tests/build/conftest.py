"""build tests (part 1: data layer): two clients from the tenancy world; the assertions run as
`shifde_app` under row-level security (`tests.tenancy.conftest.app_session` asserts the role).

Rows the tests add are removed before each test, as the schema owner.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import repository as build_repo
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from tests.build.env_support import add_published
from tests.conftest import CLEAN_TABLES
from tests.connections.conftest import connection_values, discovery_values, owner_vendor
from tests.tenancy.conftest import World, app_session, world

__all__ = ["World", "owner_vendor", "world"]

for _table in (
    "pipeline_schedules",
    "pipeline_validation_reports",
    "pipeline_version_refs",
    "prompt_test_runs",
    "publish_requests",
    "artifact_version_pins",
    "artifact_versions",
    "artifacts",
    "model_routes",
    "models",
    "model_providers",
    "connections",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


# ---- part 3b: two clients with an agent, a tool, a database connection and its discovery each


TOOL_SPEC: dict[str, Any] = {
    "executor": "rest.http",
    "input_schema": {
        "type": "object",
        "properties": {"item": {"type": "string"}},
        "required": ["item"],
    },
}


@dataclass(frozen=True)
class Side:
    tenant: UUID
    connection: UUID
    table: str
    agent_artifact: UUID
    agent_version: UUID
    tool_artifact: UUID
    tool_version: UUID


@pytest.fixture
def sides(owner_db: Database, world: World) -> tuple[Side, Side]:
    out: list[Side] = []
    with owner_db.session() as o:
        for label, tenant in (("a", world.a.tenant), ("b", world.b.tenant)):
            values = connection_values(
                tenant, connection_type="database", adapter_key="postgres", config={}
            )
            conn_repo.insert_connection(o, values)
            o.execute(
                text("UPDATE shifde.connections SET status = 'active' WHERE id = :i"),
                {"i": values["id"]},
            )
            table = f"customers_{label}"
            payload = {
                "tables": [
                    {
                        "schema": "public",
                        "name": table,
                        "columns": [
                            {"name": "id", "type": "integer", "nullable": False},
                            {"name": "name", "type": "text", "nullable": False},
                        ],
                    }
                ]
            }
            conn_repo.insert_discovery(
                o, {**discovery_values(tenant, values["id"]), "kind": "schema", "payload": payload}
            )
            g_art, g_ver = add_published(o, tenant, "agents", f"agent-{label}", {"x": 1})
            t_art, t_ver = add_published(o, tenant, "tools", f"tool-{label}", TOOL_SPEC)
            for artifact in (g_art, t_art):
                build_repo.refresh_latest_published(o, tenant, artifact)
            out.append(Side(tenant, values["id"], table, g_art, g_ver, t_art, t_ver))
        o.commit()
    return out[0], out[1]


def _as_app(app_db: Database, owner_db: Database, tenant: UUID) -> AbstractContextManager[Session]:
    return app_session(app_db, tenant)


def _as_owner(
    app_db: Database, owner_db: Database, tenant: UUID
) -> AbstractContextManager[Session]:
    # the schema owner is not subject to row-level security: only the tenant id confines A
    return owner_db.session()


@pytest.fixture(params=[_as_app, _as_owner], ids=["as-app-role", "as-owner-no-rls"])
def opener(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], AbstractContextManager[Session]]:
    make: Callable[[Database, Database, UUID], AbstractContextManager[Session]] = request.param
    return lambda tenant: make(app_db, owner_db, tenant)
