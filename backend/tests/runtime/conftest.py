"""Fixtures for the runtime tests: two clients, each with a run and the rows that hang off it,
written as the schema owner and committed; every runtime table is truncated before each test."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from tests._support.runs import add_run
from tests._support.tenants import add_tenants
from tests.build.env_support import add_published
from tests.conftest import CLEAN_TABLES

TABLES = ("runs", "run_events", "run_artifacts", "run_steps", "tool_invocations", "approvals",
          "jobs")  # fmt: skip
for _table in (*TABLES, "workers"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@dataclass(frozen=True)
class Client:
    tenant: UUID
    run: UUID
    workspace: UUID
    project: UUID
    tool_version: UUID
    job: UUID
    step: UUID
    invocation: UUID
    approval: UUID


def insert(s: Session, table: str, **values: Any) -> None:  # Any: column values
    names = list(values)
    s.execute(
        text(
            f"INSERT INTO shifde.{table} ({', '.join(names)}) "
            f"VALUES ({', '.join(':' + n for n in names)})"
        ),
        values,
    )


def add_client_rows(s: Session, tenant: UUID) -> Client:
    run = add_run(s, tenant)
    ws, proj = s.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": run}
    ).one()
    _, tool_version = add_published(s, tenant, "tools", f"tool-{uuid7().hex}", {})
    job, step, invocation, approval = uuid7(), uuid7(), uuid7(), uuid7()
    insert(s, "run_events", id=uuid7(), tenant_id=tenant, run_id=run, seq=1, type="created")
    s.execute(text("UPDATE shifde.runs SET event_seq = 1 WHERE id = :r"), {"r": run})
    insert(s, "run_artifacts", id=uuid7(), tenant_id=tenant, run_id=run, kind="input",
           size_bytes=2, content="{}", sha256="x")  # fmt: skip
    insert(s, "tool_invocations", id=invocation, tenant_id=tenant, run_id=run, call_ref="c1",
           tool_version_id=tool_version, side_effect_class="write_non_idempotent",
           idempotency_key=f"k-{invocation}", status="started", request_hash="h")  # fmt: skip
    insert(s, "run_steps", id=step, tenant_id=tenant, run_id=run, seq=1, kind="tool_call",
           name="t", tool_invocation_id=invocation, tool_version_id=tool_version)  # fmt: skip
    insert(s, "approvals", id=approval, tenant_id=tenant, run_id=run, workspace_id=ws,
           project_id=proj, call_ref="c0", reason="tool_side_effect", requested_by=uuid7(),
           expires_at=utcnow() + timedelta(hours=1))  # fmt: skip
    insert(s, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=run,
           dedupe_key=f"run:{run}:1")  # fmt: skip
    return Client(tenant, run, ws, proj, tool_version, job, step, invocation, approval)


@dataclass(frozen=True)
class Two:
    a: Client
    b: Client


@pytest.fixture
def two(owner_db: Database) -> Two:
    a, b = uuid7(), uuid7()
    with owner_db.session() as s:
        add_tenants(s, a, b)
        pair = Two(add_client_rows(s, a), add_client_rows(s, b))
        s.commit()
    return pair


def app_session(db: Database, tenant: UUID | None) -> Session:
    """A `shifde_app` session, with `tenant`'s context when given."""
    s = db.session()
    if tenant is not None:
        set_tenant(s, tenant)
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    return s
