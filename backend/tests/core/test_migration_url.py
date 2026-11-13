"""Migrations run only as the schema owner: there is no fallback to the application URL
(isolation review 1, item 13)."""

from __future__ import annotations

import pytest

from shifde.core.settings import MigrationUrlMissing, Settings

APP = "postgresql+psycopg://shifde_app@127.0.0.1:1/x"
OWNER = "postgresql+psycopg://shifde_owner@127.0.0.1:1/x"


def test_missing_migration_url_does_not_fall_back_to_the_app_url() -> None:
    settings = Settings(_env_file=None, env="test", database_url=APP)
    with pytest.raises(MigrationUrlMissing, match="SHIFDE_MIGRATION_DATABASE_URL is not set"):
        _ = settings.effective_migration_url


def test_migration_url_is_used_when_set() -> None:
    settings = Settings(_env_file=None, env="test", database_url=APP, migration_database_url=OWNER)
    assert settings.effective_migration_url == OWNER
