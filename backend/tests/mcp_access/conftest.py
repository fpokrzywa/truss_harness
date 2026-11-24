"""Fixtures of the MCP access tests: the full-app per-test database harness of `tests/app`,
imported by name (as `tests/evaluation`), so each test has the seeded demo world (`demo-a`,
`demo-b`, each with workspace `main` and project `sample`) in a database of its own, and an app
with `SHIFDE_MCP_ENABLED` (`mcp`, a `TestClient` that sends no `Origin` header)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shifde.core.settings import Settings
from tests.app.conftest import (
    _admin_engine,
    _dropper,
    app_db,
    app_engine,
    app_url,
    owner_db,
    owner_engine,
    owner_session,
    owner_url,
    test_database,
    world,
)
from tests.mcp_access.helpers import mcp_client, mcp_settings

__all__ = [
    "_admin_engine",
    "_dropper",
    "app_db",
    "app_engine",
    "app_url",
    "owner_db",
    "owner_engine",
    "owner_session",
    "owner_url",
    "test_database",
    "world",
]


@pytest.fixture
def mcp(settings: Settings, world: object) -> Iterator[TestClient]:
    with mcp_client(mcp_settings(settings)) as c:
        yield c
