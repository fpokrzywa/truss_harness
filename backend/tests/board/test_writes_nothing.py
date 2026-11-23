"""The board writes nothing (sub-project 7 design section 3): its repository holds no write
statement and no transaction end, and a board request leaves the plans and deployments (counts and
`updated_at`) and the audit events as they were, an expired plan included (it is not settled)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any  # Any: row values

from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from tests.board.conftest import (
    deployment,
    get_board,
    org_board,
    plan,
    project_board,
    solution,
    target,
)
from tests.connections.routes import as_user
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws_slug

REPOSITORY = Path(__file__).resolve().parents[2] / "src" / "shifde" / "board" / "repository.py"
WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE)\b|commit")


def test_the_repository_has_no_write_statement() -> None:
    source = REPOSITORY.read_text(encoding="utf-8")
    assert source.strip()
    assert WRITES.findall(source) == []


def _state(owner_db: Database, tenant: object) -> list[tuple[Any, ...]]:
    with owner_db.session() as o:
        return [
            tuple(r)
            for sql in (
                "SELECT id, status, updated_at FROM shifde.delivery_plans WHERE tenant_id = :t "
                "ORDER BY id",
                "SELECT id, status, updated_at FROM shifde.delivery_deployments "
                "WHERE tenant_id = :t ORDER BY id",
                "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t",
            )
            for r in o.execute(text(sql), {"t": tenant}).all()
        ]


def test_a_board_request_changes_no_row(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t = target(owner_db, a.tenant, a.ws, a.project)
    plan(owner_db, a.tenant, t, sol.first, "sealed", sealed_age="1 hour")  # expired, unsettled
    deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t, "unknown_outcome")
    deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t)  # queued
    h = as_user(client, people.owner_a)
    before = _state(owner_db, a.tenant)
    assert len(before) == 6  # three plans, two deployments, the audit count
    for url in (org_board(a.slug), project_board(a.slug, ws_slug(a), project_slug(a))):
        get_board(client, h, url)
    assert _state(owner_db, a.tenant) == before
