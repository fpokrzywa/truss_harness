"""Fixtures of the workspace scoping route test (F-08, DECISIONS B-2653): the full-app per-test
database harness of `tests/app`, imported by name rather than copied (as `tests/perf`, B-2633).
`tests/authz/conftest.py` defines an `owner_session` of its own; these override it here."""

from __future__ import annotations

from tests.app.conftest import (
    _admin_engine,
    _dropper,
    app_url,
    client,
    owner_engine,
    owner_session,
    owner_url,
    rest_server,
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
    "rest_server",
    "test_database",
    "world",
]
