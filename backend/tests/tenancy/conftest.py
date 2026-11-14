"""Tenancy test world: two clients, each with two workspaces, a project, a member, a service
account and a SHI-wide group of staff, written as the schema owner.

`tenants`, `workspaces` and `projects` are not in `CLEAN_TABLES`: truncating `tenants` would
also truncate every table keyed to it, the seeded system roles included. Every world uses fresh
ids and slugs instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.ids import uuid7
from tests._support.tenants import add_project, add_tenant, add_workspace
from tests.authz.conftest import owner_insert_group


@dataclass(frozen=True)
class Client:
    tenant: UUID
    slug: str
    ws: UUID
    ws2: UUID
    project: UUID
    member: UUID
    service_account: UUID
    staff: UUID  # in `group`, no membership
    group: UUID


@dataclass(frozen=True)
class World:
    a: Client
    b: Client


def owner_person(owner: Session, label: str) -> UUID:
    uid = uuid7()
    owner.execute(
        text("INSERT INTO shifde.users (id, primary_email, display_name) VALUES (:i, :e, :e)"),
        {"i": uid, "e": f"{label}-{uid.hex}@shi.test"},
    )
    return uid


def owner_member(owner: Session, tenant: UUID, user: UUID, status: str = "active") -> None:
    owner.execute(
        text(
            "INSERT INTO shifde.memberships (id, tenant_id, user_id, status) "
            "VALUES (:i, :t, :u, :s)"
        ),
        {"i": uuid7(), "t": tenant, "u": user, "s": status},
    )


def _client(owner: Session, label: str) -> Client:
    tenant = uuid7()
    slug = f"{label}-{tenant.hex[-12:]}"
    add_tenant(owner, tenant, slug=slug)
    ws, ws2 = add_workspace(owner, tenant), add_workspace(owner, tenant)
    project = add_project(owner, tenant, ws)
    member = owner_person(owner, f"member-{label}")
    owner_member(owner, tenant, member)
    sa = uuid7()
    owner.execute(
        text("INSERT INTO shifde.service_accounts (id, tenant_id, name) VALUES (:i, :t, 'sa')"),
        {"i": sa, "t": tenant},
    )
    staff = owner_person(owner, f"staff-{label}")
    owner.commit()
    group = owner_insert_group(owner, f"fdes-{label}-{tenant.hex[-12:]}", [staff])
    return Client(tenant, slug, ws, ws2, project, member, sa, staff, group)


@pytest.fixture
def world(owner_db: Database) -> World:
    with owner_db.session() as owner:
        return World(_client(owner, "a"), _client(owner, "b"))


@contextmanager
def app_session(
    db: Database, tenant: UUID | None, principal: UUID | None = None
) -> Iterator[Session]:
    """A `shifde_app` session in `tenant`'s context (none if None); asserts the role."""
    with db.session() as s:
        if tenant is not None:
            set_tenant(s, tenant)
        if principal is not None:
            set_principal(s, principal)
        assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        yield s
