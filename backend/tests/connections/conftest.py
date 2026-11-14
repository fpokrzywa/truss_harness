"""Connections tests, part 1 (data layer): two clients from the tenancy world, each with a secret,
an egress policy and a private vendor written as the schema owner; the assertions run as
`shifde_app` under row-level security (`tests.tenancy.conftest.app_session` asserts the role).

Rows the tests add are removed before and after each test, as the schema owner: every connection,
test and discovery, every client egress policy (the seeded installation default stays), every
private vendor (the seeded global catalog stays) and the secrets these tests created.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any  # Any: SQL parameter values
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.tenancy.conftest import Client, World, world

__all__ = ["world"]

SECRET_PREFIX = "conn-test-"


@dataclass(frozen=True)
class Side:
    """One client's fixtures: its tenancy rows plus a secret, egress policy and private vendor."""

    c: Client
    secret: UUID
    policy: UUID
    vendor: UUID

    @property
    def tenant(self) -> UUID:
        return self.c.tenant


@dataclass(frozen=True)
class Pair:
    a: Side
    b: Side
    installation_secret: UUID
    installation_policy: UUID
    global_vendor: UUID


def owner_secret(owner: Session, tenant: UUID | None) -> UUID:
    sid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (:i, :t, :n, 'env', 'SHIFDE_TEST_UNUSED')"
        ),
        {"i": sid, "t": tenant, "n": f"{SECRET_PREFIX}{sid.hex}"},
    )
    return sid


def owner_policy(owner: Session, tenant: UUID, *, is_default: bool = False) -> UUID:
    pid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.egress_policies (id, tenant_id, name, is_default, created_by) "
            "VALUES (:i, :t, :n, :d, :i)"
        ),
        {"i": pid, "t": tenant, "n": f"policy-{pid.hex}", "d": is_default},
    )
    return pid


def owner_vendor(owner: Session, tenant: UUID) -> UUID:
    vid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.vendors (id, tenant_id, key, name, category, created_by) "
            "VALUES (:i, :t, :k, 'private', 'other', :i)"
        ),
        {"i": vid, "t": tenant, "k": f"v-{vid.hex}"},
    )
    return vid


def connection_values(tenant: UUID, **overrides: Any) -> dict[str, Any]:
    cid = uuid7()
    values: dict[str, Any] = {
        "id": cid,
        "tenant_id": tenant,
        "workspace_id": None,
        "project_id": None,
        "name": f"conn-{cid.hex}",
        "connection_type": "rest",
        "adapter_key": "rest.http",
        "vendor_id": None,
        "config": {"base_url": "https://api.example.com"},
        "auth_method": "none",
        "secret_ref_id": None,
        "egress_policy_id": None,
        "access_mode": "read",
        "created_by": None,
    }
    values.update(overrides)
    return values


def check_values(tenant: UUID, connection: UUID) -> dict[str, Any]:

    now = utcnow()
    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "connection_id": connection,
        "started_at": now,
        "finished_at": now,
        "duration_ms": 1,
        "outcome": "pass",
        "checks": [],
        "error_class": None,
        "redacted_message": None,
        "config_version": 1,
        "config_hash": "h",
        "tested_by": None,
    }


def discovery_values(tenant: UUID, connection: UUID) -> dict[str, Any]:

    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "connection_id": connection,
        "discovered_at": utcnow(),
        "kind": "openapi",
        "payload": {"paths": {}},
        "payload_hash": "h",
        "discovered_by": None,
    }


def _clean(owner_db: Database) -> None:
    with owner_db.session() as s:
        s.execute(text("DELETE FROM shifde.connection_discoveries"))
        s.execute(text("DELETE FROM shifde.connection_tests"))
        s.execute(text("DELETE FROM shifde.connections"))
        s.execute(text("DELETE FROM shifde.egress_policies WHERE tenant_id IS NOT NULL"))
        s.execute(text("DELETE FROM shifde.vendors WHERE created_by IS NOT NULL"))
        s.execute(text("DELETE FROM shifde.secrets WHERE name LIKE :p"), {"p": f"{SECRET_PREFIX}%"})
        s.commit()


@pytest.fixture(autouse=True)
def clean_connections(owner_db: Database) -> Iterator[None]:
    _clean(owner_db)
    yield
    _clean(owner_db)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def pair(owner_db: Database, world: World) -> Pair:
    with owner_db.session() as o:
        sides = [
            Side(c, owner_secret(o, c.tenant), owner_policy(o, c.tenant), owner_vendor(o, c.tenant))
            for c in (world.a, world.b)
        ]
        installation_secret = owner_secret(o, None)
        installation_policy = o.execute(
            text("SELECT id FROM shifde.egress_policies WHERE tenant_id IS NULL AND is_default")
        ).scalar_one()
        global_vendor = o.execute(
            text("SELECT id FROM shifde.vendors WHERE tenant_id IS NULL AND key = 'generic-rest'")
        ).scalar_one()
        o.commit()
    return Pair(sides[0], sides[1], installation_secret, installation_policy, global_vendor)
