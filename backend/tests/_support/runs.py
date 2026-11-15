"""Direct inserts (as the schema owner) of runtime rows for tests that need a real run: a run of an
agent version in a workspace and project of its own client, and the rows that hang off it."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

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
