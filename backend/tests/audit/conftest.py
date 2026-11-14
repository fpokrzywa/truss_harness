"""Fixtures for the audit tests.

`audit_events` is append-only (an UPDATE or DELETE trigger refuses), but the per-test cleanup
TRUNCATEs as the schema owner, which row triggers do not see; so the table is registered like
any other.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from shifde.core.db import Database
from tests.conftest import CLEAN_TABLES

CLEAN_TABLES.append("audit_events")


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s
