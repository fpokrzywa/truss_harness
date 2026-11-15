"""Fixtures for the ledger tests.

The ledger tables are registered with the per-test cleanup (they are truncated as the owner
before each test). `owner_session` is the schema owner (no row-level security) and is never
committed by most tests: their rows (runs included, `tests._support.runs`) vanish with the rollback.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from shifde.core.db import Database
from tests.conftest import CLEAN_TABLES

CLEAN_TABLES.extend(
    [
        "budget_alerts",
        "budget_reservations",
        "budgets",
        "quotas",
        "usage_rollups_daily",
        "usage_events",
        "rate_card_versions",
        "rate_cards",
    ]
)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s
