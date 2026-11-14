"""Fixtures for the adapters tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from shifde.core.db import Database
from tests._support.servers import mcp_server, oai_server, rest_server

__all__ = ["mcp_server", "oai_server", "rest_server", "owner_session"]


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s
