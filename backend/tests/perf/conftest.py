"""Fixtures of the latency tests: the full-app per-test database harness of `tests/app`,
imported by name rather than copied (DECISIONS B-2633). Each test gets its own database cloned
from the run's migrated template and seeded with the demo world."""

from __future__ import annotations

from tests.app.conftest import (
    _admin_engine,
    _dropper,
    app_url,
    client,
    owner_engine,
    owner_session,
    owner_url,
    test_database,
    world,
)

__all__ = [
    "_admin_engine",
    "_dropper",
    "app_url",
    "client",
    "owner_engine",
    "owner_session",
    "owner_url",
    "test_database",
    "world",
]
