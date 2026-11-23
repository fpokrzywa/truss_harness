"""SP5-2 (design 9.1): `appgen_requests_guard` in the database.

As `shifde_app` in client A's context, an app request is refused (23514) when its discovery is
client B's, or is of another of A's connections; when its project is not in its workspace; or
when its connection is scoped to another project or another workspace. A request whose
discovery, project, workspace and connection agree is stored (client-wide, workspace and project
connections), so each refusal is the guard's and not a missing grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: SQL parameter values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, B
from tests._support.p5util import user_session
from tests._support.tenants import add_project, add_workspace
from tests.appgen.helpers import TABLES, databricks, wire
from tests.appgen.helpers import add_project as add_project_route
from tests.tenancy.conftest import app_session

_INSERT = text(
    "INSERT INTO shifde.appgen_requests (tenant_id, id, workspace_id, project_id, "
    "connection_id, discovery_id, tables, brief, model_id, requested_by) VALUES (:t, :id, :w, "
    ":p, :c, :d, CAST(:tables AS jsonb), 'guard test', :m, :u)"
)


@dataclass(frozen=True)
class World2:
    tenant: UUID
    ws: UUID  # `main`
    p1: UUID  # `sample` in `main`
    p2: UUID  # `other` in `main`
    ws2: UUID  # a second workspace
    ws2_project: UUID  # a project in `ws2`
    client_wide: tuple[UUID, UUID]  # (connection, its latest discovery)
    other: tuple[UUID, UUID]  # a second client-wide connection of A
    workspace: tuple[UUID, UUID]  # scoped to `main`
    project: tuple[UUID, UUID]  # scoped to `sample`
    b: tuple[UUID, UUID]  # client B's


def _discovered(owner: Session, cid: str) -> tuple[UUID, UUID]:
    owner.rollback()
    did = owner.execute(
        text(
            "SELECT id FROM shifde.connection_discoveries WHERE connection_id = :c "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"c": cid},
    ).scalar_one()
    owner.rollback()
    return UUID(cid), UUID(str(did))


def _id(owner: Session, sql: str, **params: Any) -> UUID:  # Any: SQL parameter values
    return UUID(str(owner.execute(text(sql), params).scalar_one()))


@pytest.fixture
def w(client: TestClient, world: World, owner_session: Session) -> World2:
    wire(client)
    a = user_session(client, world.passwords, ADMIN)
    c = a.client
    add_project_route(c, a, "other")
    client_wide = databricks(c, a, "lake-1")
    other = databricks(c, a, "lake-2")
    workspace = databricks(c, a, "lake-ws", scope={"workspace": "main"})
    project = databricks(c, a, "lake-p1", scope={"workspace": "main", "project": "sample"})
    bh = user_session(client, world.passwords, ADMIN_B)
    b = databricks(bh.client, bh, "lake-b", org=B)
    owner_session.rollback()
    tenant = UUID(str(tenant_id(owner_session, "demo-a")))
    ws = _id(
        owner_session,
        "SELECT id FROM shifde.workspaces WHERE tenant_id = :t AND slug = 'main'",
        t=tenant,
    )
    project_of = "SELECT id FROM shifde.projects WHERE workspace_id = :w AND slug = :s"
    p1 = _id(owner_session, project_of, w=ws, s="sample")
    p2 = _id(owner_session, project_of, w=ws, s="other")
    ws2 = add_workspace(owner_session, tenant)
    ws2_project = add_project(owner_session, tenant, ws2)
    owner_session.commit()
    return World2(
        tenant,
        ws,
        p1,
        p2,
        ws2,
        ws2_project,
        _discovered(owner_session, client_wide),
        _discovered(owner_session, other),
        _discovered(owner_session, workspace),
        _discovered(owner_session, project),
        _discovered(owner_session, b),
    )


def _insert(
    app_db: Database, w: World2, ws: UUID, project: UUID, connection: UUID, discovery: UUID
) -> str | None:
    """Insert a request as `shifde_app` in A's context; None when stored, else the SQLSTATE."""
    values = {
        "t": w.tenant,
        "id": uuid7(),
        "w": ws,
        "p": project,
        "c": connection,
        "d": discovery,
        "tables": f'["{TABLES[0]}"]',
        "m": uuid7(),
        "u": uuid7(),
    }
    with app_session(app_db, w.tenant) as s:
        try:
            s.execute(_INSERT, values)
        except DBAPIError as err:
            s.rollback()
            return getattr(err.orig, "sqlstate", None)
        s.commit()
    return None


def test_a_request_whose_parts_agree_is_stored(w: World2, app_db: Database) -> None:
    assert _insert(app_db, w, w.ws, w.p1, *w.client_wide) is None
    assert _insert(app_db, w, w.ws2, w.ws2_project, *w.client_wide) is None
    assert _insert(app_db, w, w.ws, w.p2, *w.workspace) is None
    assert _insert(app_db, w, w.ws, w.p1, *w.project) is None


def test_another_clients_discovery_is_refused(w: World2, app_db: Database) -> None:
    connection, _ = w.client_wide
    assert _insert(app_db, w, w.ws, w.p1, connection, w.b[1]) == "23514"


def test_another_connections_discovery_is_refused(w: World2, app_db: Database) -> None:
    connection, _ = w.client_wide
    assert _insert(app_db, w, w.ws, w.p1, connection, w.other[1]) == "23514"


def test_a_project_connection_in_another_project_is_refused(w: World2, app_db: Database) -> None:
    assert _insert(app_db, w, w.ws, w.p2, *w.project) == "23514"


def test_a_project_outside_the_workspace_is_refused(w: World2, app_db: Database) -> None:
    # `sample` is in `main`, not in `ws2`: refused even with a client-wide connection
    assert _insert(app_db, w, w.ws2, w.p1, *w.client_wide) == "23514"
    assert _insert(app_db, w, w.ws, w.ws2_project, *w.client_wide) == "23514"


def test_a_workspace_connection_in_another_workspace_is_refused(
    w: World2, app_db: Database
) -> None:
    assert _insert(app_db, w, w.ws2, w.ws2_project, *w.workspace) == "23514"
    assert _insert(app_db, w, w.ws2, w.ws2_project, *w.project) == "23514"
