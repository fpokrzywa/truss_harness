"""`trg_tenants_settings_guard` (tenancy_0007, issue #39) at the database, as `shifde_app`.

The column grant `UPDATE (settings)` on `shifde.tenants` (B-2550) lets the app role write the
whole settings document. Row-level security confines the write to the client in context; the
guard confines it to the `capture_content` key unless the platform-administrator flag is on.
"""

from __future__ import annotations

import json
from typing import Any  # Any: settings documents
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_platform_admin
from tests.tenancy.conftest import World, app_session

SEED: dict[str, Any] = {"capture_content": "metadata", "unknown_cost_policy": "allow_unmeasured"}


@pytest.fixture
def seeded(owner_db: Database, world: World) -> World:
    with owner_db.session() as owner:
        for tenant in (world.a.tenant, world.b.tenant):
            owner.execute(
                text("UPDATE shifde.tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
                {"s": json.dumps(SEED), "t": tenant},
            )
        owner.commit()
    return world


def _settings(owner_db: Database, tenant: UUID) -> dict[str, Any]:
    with owner_db.session() as owner:  # a fresh snapshot
        settings: dict[str, Any] = owner.execute(
            text("SELECT settings FROM shifde.tenants WHERE id = :t"), {"t": tenant}
        ).scalar_one()
        return settings


def _update(s: Session, tenant: UUID, expr: str) -> int:
    result = s.execute(
        text(f"UPDATE shifde.tenants SET settings = {expr} WHERE id = :t"),  # noqa: S608
        {"t": tenant},
    )
    s.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


def test_a_cross_client_write_changes_nothing(
    app_db: Database, owner_db: Database, seeded: World
) -> None:
    a, b = seeded.a, seeded.b
    with app_session(app_db, a.tenant) as s:
        assert _update(s, b.tenant, """'{"capture_content": "full"}'::jsonb""") == 0
        assert _update(s, b.tenant, """settings || '{"capture_content": "full"}'""") == 0
    assert _settings(owner_db, b.tenant) == SEED


def test_no_client_in_context_changes_nothing(
    app_db: Database, owner_db: Database, seeded: World
) -> None:
    with app_session(app_db, None) as s:
        for tenant in (seeded.a.tenant, seeded.b.tenant):
            assert _update(s, tenant, """'{"capture_content": "full"}'::jsonb""") == 0
    assert _settings(owner_db, seeded.a.tenant) == SEED
    assert _settings(owner_db, seeded.b.tenant) == SEED


@pytest.mark.parametrize(
    "expr",
    [
        """settings || '{"unknown_cost_policy": "deny"}'""",
        """settings || '{"new_key": 1}'""",
        "settings - 'unknown_cost_policy'",
        """'{"capture_content": "full"}'::jsonb""",
        "'[]'::jsonb",
    ],
    ids=["change", "add", "remove", "replace", "non-object"],
)
def test_another_key_is_refused_in_the_clients_own_context(
    app_db: Database, owner_db: Database, seeded: World, expr: str
) -> None:
    a = seeded.a
    with app_session(app_db, a.tenant) as s:
        with pytest.raises(DBAPIError) as exc:
            _update(s, a.tenant, expr)
        assert getattr(exc.value.orig, "sqlstate", None) == "42501"
        s.rollback()
    assert _settings(owner_db, a.tenant) == SEED
    assert _settings(owner_db, seeded.b.tenant) == SEED


def test_capture_content_may_be_set_changed_and_removed(
    app_db: Database, owner_db: Database, seeded: World
) -> None:
    a = seeded.a
    others = {k: v for k, v in SEED.items() if k != "capture_content"}
    with app_session(app_db, a.tenant) as s:
        assert _update(s, a.tenant, """settings || '{"capture_content": "full"}'""") == 1
        assert _settings(owner_db, a.tenant) == {**others, "capture_content": "full"}
        assert _update(s, a.tenant, """settings || '{"capture_content": "metadata"}'""") == 1
        assert _settings(owner_db, a.tenant) == {**others, "capture_content": "metadata"}
        assert _update(s, a.tenant, "settings - 'capture_content'") == 1
        assert _settings(owner_db, a.tenant) == others
        assert _update(s, a.tenant, "settings") == 1
    assert _settings(owner_db, a.tenant) == others
    assert _settings(owner_db, seeded.b.tenant) == SEED


@pytest.mark.parametrize("in_context", [True, False], ids=["client-context", "no-context"])
def test_the_platform_admin_flag_allows_other_keys(
    app_db: Database, owner_db: Database, seeded: World, in_context: bool
) -> None:
    a = seeded.a
    with app_session(app_db, a.tenant if in_context else None) as s:
        set_platform_admin(s, True)
        assert _update(s, a.tenant, """settings || '{"unknown_cost_policy": "deny"}'""") == 1
    assert _settings(owner_db, a.tenant) == {**SEED, "unknown_cost_policy": "deny"}
