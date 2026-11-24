"""Fixtures of the assist tests: the full-app per-test database harness of `tests/app`, imported
by name (as `tests/appgen`), so each test has the seeded demo world (`demo-a`, `demo-b`, each
with workspace `main` and project `sample`) in a database of its own. The assist tables are
registered in `CLEAN_TABLES` like every module's client data."""

from __future__ import annotations

from tests.app.conftest import (
    _admin_engine,
    _dropper,
    app_db,
    app_url,
    client,
    owner_db,
    owner_engine,
    owner_session,
    owner_url,
    test_database,
    world,
)
from tests.conftest import CLEAN_TABLES

for _table in ("assist_proposals", "assist_requests", "assist_specialists", "assist_settings"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

__all__ = [
    "_admin_engine",
    "_dropper",
    "app_db",
    "app_url",
    "client",
    "owner_db",
    "owner_engine",
    "owner_session",
    "owner_url",
    "test_database",
    "world",
]
