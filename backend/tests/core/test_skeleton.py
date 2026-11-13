"""Skeleton smoke tests: health endpoint and migration state of the per-worker database."""

from __future__ import annotations

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from shifde.core.migrations import alembic_config, code_heads


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_worker_database_is_at_every_code_head(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        versions = set(c.execute(text("SELECT version_num FROM shifde.alembic_version")).scalars())
    assert versions == set(code_heads())
    assert "base_0001" in versions


def test_app_role_reads_context_helpers(app_engine: Engine) -> None:
    """Baseline helpers fail closed when the per-transaction settings are unset."""
    with app_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT shifde.current_tenant_id(), shifde.current_principal_id(),"
                " shifde.is_platform_admin_ctx()"
            )
        ).one()
        assert tuple(row) == (None, None, False)
        c.execute(
            text(
                "SELECT set_config('app.tenant_id', '01900000-0000-7000-8000-0000000000aa', true),"
                " set_config('app.platform_admin', 'on', true)"
            )
        )
        row = c.execute(
            text("SELECT shifde.current_tenant_id()::text, shifde.is_platform_admin_ctx()")
        ).one()
        assert tuple(row) == ("01900000-0000-7000-8000-0000000000aa", True)


def test_every_migration_downgrades_and_upgrades_again(owner_url: str) -> None:
    """Every revision has a working `downgrade()`: the worker database goes down to the root and
    back up to every head (and is left at every head for the tests that follow)."""
    cfg = alembic_config(owner_url)
    command.downgrade(cfg, "base")
    try:
        assert _versions(owner_url) == set()
    finally:
        command.upgrade(cfg, "heads")
    assert _versions(owner_url) == set(code_heads())


def _versions(url: str) -> set[str]:
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as c:
            return set(c.execute(text("SELECT version_num FROM shifde.alembic_version")).scalars())
    finally:
        engine.dispose()
