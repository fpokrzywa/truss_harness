"""Alembic wiring shared by the CLI, API readiness check, and tests."""

from __future__ import annotations

import os
from collections.abc import Sequence
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
    """The revisions `alembic_version` holds once the database is at every head of the migration
    graph shipped with this code. Each module has its own Alembic branch (DECISIONS B-004), so the
    graph has several heads once modules are ported; upgrades run `alembic upgrade heads`.

    A head that another revision names in `depends_on` (identity_0001 depends on secrets_0001)
    is an ancestor of that revision, and Alembic leaves it out of `alembic_version`; such a head
    is left out here too (DECISIONS B-906)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir()))
    script = ScriptDirectory.from_config(cfg)
    heads = set(script.get_heads())
    reached: set[str] = set()
    pending = list(heads)
    while pending:
        revision = script.get_revision(pending.pop())
        if revision is None:
            continue
        for parent in (*_names(revision.down_revision), *_names(revision.dependencies)):
            parent_rev = script.get_revision(parent)  # a dependency may name a branch label
            if parent_rev is not None and parent_rev.revision not in reached:
                reached.add(parent_rev.revision)
                pending.append(parent_rev.revision)
    return frozenset(heads - reached)


def _names(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)
