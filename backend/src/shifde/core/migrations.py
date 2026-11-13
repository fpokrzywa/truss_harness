"""Alembic wiring shared by the CLI, API readiness check, and tests."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# backend/src/shifde/core/migrations.py -> parents[3] is `backend/`.
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "migrations"


def migrations_dir() -> Path:
    override = os.environ.get("SHIFDE_MIGRATIONS_DIR")
    return Path(override) if override else _DEFAULT_DIR


def alembic_config(url: str) -> Config:
    """Alembic config for `url` (must be a `shifde_owner` connection URL)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    cfg.attributes["url"] = url
    return cfg


@lru_cache
def code_head() -> str:
    """Head revision of the migration chain shipped with this code."""
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:  # pragma: no cover - chain is never empty
        raise RuntimeError("migration chain has no head")
    return head


@lru_cache
def code_heads() -> frozenset[str]:
    """Every head of the migration graph shipped with this code. Each module has its own Alembic
    branch (DECISIONS B-004), so the graph has several heads once modules are ported; upgrades
    run `alembic upgrade heads`."""
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    return frozenset(ScriptDirectory.from_config(cfg).get_heads())
