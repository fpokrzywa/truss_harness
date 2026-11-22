"""Direct inserts (as the schema owner) of runtime rows for tests that need a real run: a run of an
agent version in a workspace and project of its own client, and the rows that hang off it."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from tests._support.tenants import add_project, add_workspace


def add_agent_version(s: Session, tenant_id: UUID, key: str | None = None) -> tuple[UUID, UUID]:
    """(agent artifact id, published version id) of `tenant_id`."""
    from tests.build.env_support import add_published

    return add_published(s, tenant_id, "agents", key or f"agent-{uuid7().hex}", {})


def add_run(s: Session, tenant_id: UUID, **cols: Any) -> UUID:  # Any: column values
    """A root agent run of `tenant_id` (pending, deadline in an hour). `cols` override any column;
    a workspace, project and agent version of the same client are created unless given."""
    run = uuid7()
    if "workspace_id" not in cols:
        cols["workspace_id"] = add_workspace(s, tenant_id)
    if "project_id" not in cols:
        cols["project_id"] = add_project(s, tenant_id, cols["workspace_id"])
    if "agent_version_id" not in cols and cols.get("kind", "agent") == "agent":
        agent, version = add_agent_version(s, tenant_id)
        cols.setdefault("agent_id", agent)
        cols["agent_version_id"] = version
    values: dict[str, Any] = {
        "kind": "agent",
        "initiated_by_principal_id": uuid7(),
        "principal_type": "user",
        "root_run_id": run,
        "trace_id": run.hex,
        "correlation_id": run.hex,
        "deadline_at": utcnow() + timedelta(hours=1),
        **cols,
    }
    names = ["id", "tenant_id", *values]
    s.execute(
        text(
            f"INSERT INTO shifde.runs ({', '.join(names)}) "
            f"VALUES ({', '.join(':' + n for n in names)})"
        ),
        {"id": run, "tenant_id": tenant_id, **values},
    )
    return run


def lose_rows(admin_url: str, owner_url: str, sql: str, **params: Any) -> None:  # Any: binds
    """Run one setup DELETE that simulates lost runtime rows, committed, as the superuser of
    `admin_url` on the database of `owner_url`, with row triggers off (`session_replication_role
    = replica`, as the per-test cleanup): the retention guards of runtime_0006 refuse such a
    delete for every role (DECISIONS B-2783)."""
    url = make_url(admin_url).set(database=make_url(owner_url).database)
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as c:
            c.execute(text("SET LOCAL session_replication_role = replica"))
            c.execute(text(sql), params)
    finally:
        engine.dispose()
