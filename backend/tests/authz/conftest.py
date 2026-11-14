"""Fixtures for the authz tests.

`seed_bindings` builds the world a binding needs through real rows and inserts the bindings as
`shifde_owner` with every row trigger active: `check_role_binding()` reads `memberships`,
`service_accounts`, `workspaces` and `projects`, so each person (a user with an active
membership), service account, workspace and project a binding names is created first when it
does not exist. Group bindings run in the platform-administrator context (design 5.3, B-1010).

`owner_insert_bindings` (superuser, `SET LOCAL session_replication_role = replica`: row triggers
off for one transaction) stays only for rows the triggers and CHECKs now refuse on purpose and
the PDP must still ignore: the `platform_administrator` template bound in a client
(`test_platform_only.py`, B-1201) and a binding naming another client's role
(`tests/tenancy/test_service.py`, a hostile-owner row).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.tenants import add_tenants, add_workspace


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def seed_engine(worker_admin_url: str) -> Iterator[Engine]:
    """Superuser engine on this worker's database: the connection source of `seed_bindings`
    (which drops to `shifde_owner`) and of `owner_insert_bindings`."""
    engine = create_engine(worker_admin_url, poolclass=NullPool)
    yield engine
    engine.dispose()


def _exists(c: Connection, sql: str, **params: object) -> bool:
    return c.execute(text(sql), params).first() is not None


def _seed_subject(c: Connection, tenant: UUID, subject_type: str, subject: UUID) -> None:
    if subject_type == "user":
        c.execute(
            text(
                "INSERT INTO shifde.users (id, primary_email, display_name) VALUES (:u, :e, :e) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"u": subject, "e": f"{subject.hex}@seed.test"},
        )
        if not _exists(
            c,
            "SELECT 1 FROM shifde.memberships WHERE tenant_id = :t AND user_id = :u",
            t=tenant,
            u=subject,
        ):
            c.execute(
                text(
                    "INSERT INTO shifde.memberships (id, tenant_id, user_id, status) "
                    "VALUES (:i, :t, :u, 'active')"
                ),
                {"i": uuid7(), "t": tenant, "u": subject},
            )
    elif subject_type == "group":
        c.execute(
            text(
                "INSERT INTO shifde.groups (id, slug, name) VALUES (:g, :s, :s) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"g": subject, "s": f"g-{subject.hex}"},
        )
    elif subject_type == "service_account" and not _exists(
        c, "SELECT 1 FROM shifde.service_accounts WHERE id = :s", s=subject
    ):
        c.execute(
            text("INSERT INTO shifde.service_accounts (id, tenant_id, name) VALUES (:i, :t, 'sa')"),
            {"i": subject, "t": tenant},
        )


def _seed_scope(c: Connection, tenant: UUID, scope_type: str, scope: UUID) -> None:
    if scope_type == "workspace" and not _exists(
        c, "SELECT 1 FROM shifde.workspaces WHERE id = :i", i=scope
    ):
        add_workspace(c, tenant, scope)
    elif scope_type == "project" and not _exists(
        c, "SELECT 1 FROM shifde.projects WHERE id = :i", i=scope
    ):
        c.execute(
            text(
                "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
                "VALUES (:i, :t, :w, :s, :s)"
            ),
            {"i": scope, "t": tenant, "w": add_workspace(c, tenant), "s": f"p-{scope.hex}"},
        )


def seed_bindings(seed: Engine, rows: list[Mapping[str, object]]) -> list[UUID]:
    """Create what each row names (client, person with membership, service account, workspace,
    project, platform group; existing ones are kept), then insert the bindings (system role by `role_key`, or
    `role_id`) as `shifde_owner` with the triggers active; commits; returns the ids. `seed` is
    only the connection source: the superuser role is left before the first statement."""
    ids: list[UUID] = []
    with seed.begin() as c:
        c.execute(text("SET LOCAL ROLE shifde_owner"))
        c.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        for row in rows:
            tenant = cast(UUID, row["tenant_id"])
            add_tenants(c, tenant)
            _seed_subject(
                c, tenant, str(row.get("subject_type", "user")), cast(UUID, row["subject_id"])
            )
            _seed_scope(c, tenant, str(row["scope_type"]), cast(UUID, row["scope_id"]))
            bid = uuid7()
            c.execute(
                text(
                    "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                    "role_id, scope_type, scope_id, resource_type, expires_at) "
                    "SELECT :id, :t, :st, :sid, COALESCE(:r, (SELECT id FROM shifde.roles "
                    "WHERE tenant_id IS NULL AND key = :k)), :sc, :scid, :rt, :exp"
                ),
                {
                    "id": bid,
                    "t": tenant,
                    "st": row.get("subject_type", "user"),
                    "sid": row["subject_id"],
                    "r": row.get("role_id"),
                    "k": row.get("role_key"),
                    "sc": row["scope_type"],
                    "scid": row["scope_id"],
                    "rt": row.get("resource_type"),
                    "exp": row.get("expires_at"),
                },
            )
            ids.append(bid)
    return ids


def owner_insert_bindings(seed: Engine, rows: list[Mapping[str, object]]) -> list[UUID]:
    """Insert role bindings (system role by `role_key`, or `role_id`) with row triggers off for
    this one transaction, for the rows named in the module docstring only; commits; returns the ids."""
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
