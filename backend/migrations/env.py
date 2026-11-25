"""Alembic environment. Migrations run as `shifde_owner` (SHIFDE_MIGRATION_DATABASE_URL)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, text

from shifde.core.settings import get_settings

config = context.config


def _url() -> str:
    attr_url = config.attributes.get("url")
    if isinstance(attr_url, str) and attr_url:
        return attr_url
    return get_settings().effective_migration_url


def run_migrations_online() -> None:
    engine = create_engine(_url(), hide_parameters=True)
    try:
        with engine.connect() as connection:
            # The version table lives in the shifde schema, which must exist first.
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS shifde"))
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=None,
                version_table="alembic_version",
                version_table_schema="shifde",
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
run_migrations_online()
