"""models_admin tests (data layer and routes): two clients from the tenancy world, each with a model
connection and a private vendor written as the schema owner; the assertions run as `shifde_app`
under row-level security (`tests.tenancy.conftest.app_session` asserts the role).

The route tests (part 2a) mount `models_admin.api.router` with the connections routes in the
connections harness (`tests/models_admin/routes.py`); importing `tests.connections.conftest` adds
the sessions, audit and secret tables to the per-test truncation.

Rows the tests add are removed before and after each test, as the schema owner: every row of the
module's tables (the SHI provider included: the service creates it, no migration seeds it), every
connection and every private vendor (the seeded global catalog stays).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any  # Any: SQL parameter values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.models_admin import repository
from tests.conftest import CLEAN_TABLES
from tests.connections.conftest import owner_vendor
from tests.tenancy.conftest import Client, World, app_session, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

# part 2b: an invoke writes usage rows and (on a budget refusal) authorization decisions; the
# pricing tests add rate cards. Truncated with the rest before each test.
for _table in (
    "usage_rollups_daily",
    "usage_events",
    "rate_card_versions",
    "rate_cards",
    "policy_decisions",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

MODULE_TABLES = (
    "model_allowlist_models",
    "model_allowlists",
    "model_routes",
    "models",
    "model_providers",
    "rate_limit_windows",
    "concurrency_leases",
    "shi_provider_access",
)


@dataclass(frozen=True)
class Side:
    """One client's fixtures: its tenancy rows plus a model connection and a private vendor."""

    c: Client
    connection: UUID
    vendor: UUID

    @property
    def tenant(self) -> UUID:
        return self.c.tenant


@dataclass(frozen=True)
class Pair:
    a: Side
    b: Side
    global_vendor: UUID


def owner_connection(owner: Session, tenant: UUID) -> UUID:
    cid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, name, connection_type, adapter_key) "
            "VALUES (:i, :t, :n, 'model_api', 'fake')"
        ),
        {"i": cid, "t": tenant, "n": f"conn-{cid.hex}"},
    )
    return cid


def provider_values(tenant: UUID | None, connection: UUID | None, **over: Any) -> dict[str, Any]:
    pid = uuid7()
    values: dict[str, Any] = {
        "id": pid,
        "tenant_id": tenant,
        "vendor_id": None,
        "name": f"prov-{pid.hex}",
        "adapter_key": "fake",
        "connection_id": connection,
        "settings": {},
        "created_by": None,
    }
    values.update(over)
    return values


def model_values(tenant: UUID | None, provider: UUID, **over: Any) -> dict[str, Any]:
    mid = uuid7()
    values: dict[str, Any] = {
        "id": mid,
        "tenant_id": tenant,
        "provider_id": provider,
        "model_key": f"m-{mid.hex}",
        "display_name": "model",
        "family": None,
        "version_label": None,
        "capabilities": ["chat"],
        "context_window": 1000,
        "max_output_tokens": 100,
        "lifecycle": "active",
        "deprecation_date": None,
        "rate_limits": {},
        "pricing_ref": None,
        "created_by": None,
    }
    values.update(over)
    return values


def route_values(tenant: UUID, primary: UUID, **over: Any) -> dict[str, Any]:
    rid = uuid7()
    values: dict[str, Any] = {
        "id": rid,
        "tenant_id": tenant,
        "name": f"route-{rid.hex}",
        "scope_type": "tenant",
        "scope_id": tenant,
        "primary_model_id": primary,
        "fallback_model_ids": [],
        "conditions": {},
        "created_by": None,
    }
    values.update(over)
    return values


def start_of_window(owner: Session, margin_seconds: int = 30) -> None:
    """Block until the DB clock is early in a minute (at least `margin_seconds` of it remain).

    The limiter's window is `date_trunc('minute', now())` on the DB clock, which a test cannot pin;
    a test that needs several calls to land in one window calls this first, so a slow run (load)
    cannot roll the window over between them unless it takes longer than the margin.
    """
    seconds = float(
        owner.execute(text("SELECT extract(second FROM clock_timestamp())")).scalar_one()
    )
    owner.rollback()
    if seconds > 60 - margin_seconds:
        time.sleep(60 - seconds + 0.2)


def make_shi(app_db: Database, tenant: UUID | None = None) -> UUID:
    """The SHI provider and one shared model, written in the platform administrator context;
    returns the shared model's id."""
    with app_session(app_db, tenant) as s, platform_admin_context(s):
        repository.ensure_shi_provider(s, adapter_key="fake", created_by=None)
        values = model_values(None, repository.SHI_PROVIDER_ID, model_key="shi-model")
        repository.insert_model(s, values)
        s.commit()
    return UUID(str(values["id"]))


def _clean(owner_db: Database) -> None:
    with owner_db.session() as s:
        for table in MODULE_TABLES:
            s.execute(text(f"DELETE FROM shifde.{table}"))
        s.execute(text("DELETE FROM shifde.connection_discoveries"))
        s.execute(text("DELETE FROM shifde.connection_tests"))
        s.execute(text("DELETE FROM shifde.connections"))
        s.execute(text("DELETE FROM shifde.vendors WHERE created_by IS NOT NULL"))
        s.commit()


@pytest.fixture(autouse=True)
def clean_models(owner_db: Database) -> Iterator[None]:
    _clean(owner_db)
    yield
    _clean(owner_db)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """The connections harness plus `models_admin.api.router` (part 2a route tests)."""
    from tests.models_admin.routes import models_client  # routes.py imports this module

    with models_client(settings) as c:
        yield c


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def pair(owner_db: Database, world: World) -> Pair:
    with owner_db.session() as o:
        sides = [
            Side(c, owner_connection(o, c.tenant), owner_vendor(o, c.tenant))
            for c in (world.a, world.b)
        ]
        global_vendor = o.execute(
            text("SELECT id FROM shifde.vendors WHERE tenant_id IS NULL ORDER BY key LIMIT 1")
        ).scalar_one()
        o.commit()
    return Pair(sides[0], sides[1], global_vendor)
