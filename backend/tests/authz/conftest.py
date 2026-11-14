"""Fixtures for the authz tests.

Until the identity and tenancy ports exist there are no users, memberships, workspaces or
projects: tests use fresh ids for them. `check_role_binding()` (authz_0001, SETH's verbatim) reads
`memberships`, `service_accounts`, `workspaces` and `projects`, so it fails closed for user or
service-account subjects and workspace or project scopes (plpgsql plans the whole condition, so
in fact for every binding). `owner_insert_bindings` therefore inserts through a superuser
connection on this worker's database with `SET LOCAL session_replication_role = replica` (row
triggers off for that one transaction only). When identity and tenancy land, these tests move to
their real fixtures (docs/build/carry_forward.md).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde.core.db import Database
from shifde.core.ids import uuid7


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def seed_engine(worker_admin_url: str) -> Iterator[Engine]:
    """Superuser engine on this worker's database, for `owner_insert_bindings` only."""
    engine = create_engine(worker_admin_url, poolclass=NullPool)
    yield engine
    engine.dispose()


def owner_insert_bindings(seed: Engine, rows: list[Mapping[str, object]]) -> list[UUID]:
    """Insert role bindings (system role by `role_key`, or `role_id`) with row triggers off for
    this one transaction (see the module docstring); commits; returns the ids."""
    ids: list[UUID] = []
    with seed.begin() as owner:
        owner.execute(text("SET LOCAL session_replication_role = replica"))
        for row in rows:
            bid = uuid7()
            role_id = (
                row.get("role_id")
                or owner.execute(
                    text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"),
                    {"k": row["role_key"]},
                ).scalar_one()
            )
            owner.execute(
                text(
                    "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                    "role_id, scope_type, scope_id, resource_type, expires_at) VALUES "
                    "(:id, :t, :st, :sid, :r, :sc, :scid, :rt, :exp)"
                ),
                {
                    "id": bid,
                    "t": row["tenant_id"],
                    "st": row.get("subject_type", "user"),
                    "sid": row["subject_id"],
                    "r": role_id,
                    "sc": row["scope_type"],
                    "scid": row["scope_id"],
                    "rt": row.get("resource_type"),
                    "exp": row.get("expires_at"),
                },
            )
            ids.append(bid)
    return ids


def owner_insert_group(
    owner: Session, slug: str, members: list[UUID], status: str = "active"
) -> UUID:
    """A SHI-wide (platform-level) group with `members`, inserted as the owner; commits.

    Groups have no client since identity_0005 (DECISIONS B-928): a group reaches a client only
    through role bindings stored there. Members are people of the staff directory, so each id
    in `members` gets a `users` row if it has none (`group_members.user_id -> users`)."""
    gid = uuid7()
    owner.execute(
        text("INSERT INTO shifde.groups (id, slug, name, status) VALUES (:id, :s, :s, :st)"),
        {"id": gid, "s": slug, "st": status},
    )
    for user_id in members:
        owner.execute(
            text(
                "INSERT INTO shifde.users (id, primary_email, display_name) "
                "VALUES (:u, :e, :e) ON CONFLICT (id) DO NOTHING"
            ),
            {"u": user_id, "e": f"{user_id.hex}@staff.test"},
        )
        owner.execute(
            text("INSERT INTO shifde.group_members (group_id, user_id) VALUES (:g, :u)"),
            {"g": gid, "u": user_id},
        )
    owner.commit()
    return gid
