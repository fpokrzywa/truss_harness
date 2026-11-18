"""Delivery tests: the deployment target routes over a minimal FastAPI app (B-1020 pattern), with
the tenancy route-test world (two clients, fresh slugs and ids per test) and its people.

Targets, claims, sign-ins and audit rows are removed before each test (`tests/conftest.py:
clean_data`). Every address is unique per test (`address()`), so no test depends on another's
claim.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from tests.conftest import CLEAN_TABLES
from tests.delivery.routes import delivery_client
from tests.tenancy.conftest import World, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

for _table in (
    "delivery_targets",
    "delivery_platform_claims",
    "login_lockouts",
    "user_identities",
    "sessions",
    "local_credentials",
    "memberships",
    "idp_configs",
    "users",
    "audit_events",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with delivery_client(settings) as c:
        yield c


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s
