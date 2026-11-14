"""Item 18 (core part): connect and statement timeouts on `Database`; ARCH m1: `repr(settings)`
must not expose database passwords."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from shifde.core.db import Database
from shifde.core.settings import Settings


def _settings(**overrides: Any) -> Settings:  # Any: Settings keyword overrides
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://x",
        "env": "test",
        **overrides,
    }
    return Settings(**kwargs)


def test_database_applies_connect_and_statement_timeouts(app_url: str) -> None:
    db = Database(app_url, connect_timeout=7, statement_timeout_ms=1234)
    try:
        with db.session() as s:
            assert s.execute(text("SHOW statement_timeout")).scalar_one() == "1234ms"
            assert s.execute(text("SHOW timezone")).scalar_one() == "UTC"
            raw: Any = s.connection().connection.dbapi_connection  # Any: psycopg3 connection
            assert raw.info.get_parameters()["connect_timeout"] == "7"
    finally:
        db.dispose()


def test_timeouts_survive_the_checkout_discard_all(app_url: str) -> None:
    db = Database(app_url, pool_size=1, max_overflow=0, statement_timeout_ms=4321)
    try:
        for _ in range(3):  # same pooled connection each time, DISCARD ALL in between
            with db.session() as s:
                assert s.execute(text("SHOW statement_timeout")).scalar_one() == "4321ms"
    finally:
        db.dispose()


def test_statement_timeout_actually_cancels_a_slow_statement(app_url: str) -> None:
    db = Database(app_url, statement_timeout_ms=200)
    try:
        with db.session() as s, pytest.raises(OperationalError, match="statement timeout"):
            s.execute(text("SELECT pg_sleep(5)"))
    finally:
        db.dispose()


def test_default_timeout_settings_are_set() -> None:
    s = _settings()
    assert s.db_connect_timeout_seconds > 0
    assert s.db_statement_timeout_ms > 0


def test_repr_and_str_of_settings_hide_database_passwords() -> None:
    s = _settings(
        database_url="postgresql+psycopg://app:hunter2app@db:5432/x",
        migration_database_url="postgresql+psycopg://owner:hunter2owner@db:5432/x",
    )
    for text_ in (repr(s), str(s)):
        assert "hunter2app" not in text_ and "hunter2owner" not in text_
        assert "db:5432" in text_  # still useful for diagnostics
