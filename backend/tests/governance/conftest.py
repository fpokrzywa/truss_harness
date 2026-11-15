"""governance tests (part 1: data layer, rate-card routes): two clients from the tenancy world;
the assertions run as `shifde_app` under row-level security (`tests.tenancy.conftest.app_session`
asserts the role).

Rows the tests add are removed before each test, as the schema owner. Importing
`tests.connections.conftest` adds the sessions, audit and secret tables to the per-test
truncation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from tests.conftest import CLEAN_TABLES
from tests.connections.conftest import owner_vendor
from tests.tenancy.conftest import World, world
from tests.tenancy.routes import People, make_people

__all__ = ["owner_vendor", "world"]

for _table in (
    "retention_policies",
    "governance_policies",
    "budget_alerts",
    "budget_reservations",
    "budgets",
    "quotas",
    "usage_rollups_daily",
    "usage_events",
    "rate_card_versions",
    "rate_cards",
    "policy_decisions",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    from tests.governance.routes import governance_client  # routes.py imports this module

    with governance_client(settings) as c:
        yield c


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)
