"""`delivery_0008` in the database (issue #286, SP8-1): its downgrade to `delivery_0007` is refused
while any client has a Fabric, Palantir or Docker claim, target or artifact, naming only the count,
and otherwise restores delivery_0007's two-value named CHECKs, on a scratch database built from
this run's template. What the widened CHECKs accept is in `test_platform_claims.py`."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config
from tests._support.tenants import add_tenant, add_workspace
from tests.delivery.sp8 import TARGET_KINDS
from tests.packaging.test_target_kind_db import _scratch

# the platform and target kind CHECKs of the three tables: name and definition
_CHECKS = (
    "SELECT k.conname, pg_get_constraintdef(k.oid) FROM pg_catalog.pg_constraint k "
    "JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.conkey[1] "
    "WHERE n.nspname = 'shifde' AND k.contype = 'c' "
    "AND (c.relname, a.attname) IN (('delivery_platform_claims', 'platform_kind'), "
    "('delivery_targets', 'platform_kind'), ('delivery_artifacts', 'target_kind')) "
    "ORDER BY k.conname"
)
PLATFORM_KINDS = ("databricks", "snowflake", *TARGET_KINDS)
ARTIFACT_KINDS = ("databricks.app/v1", "snowflake.native-app/v1", *TARGET_KINDS.values())


def _any(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'::text" for v in values)
    return f"CHECK (({column} = ANY (ARRAY[{listed}])))"


def _expected(platforms: tuple[str, ...], artifacts: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        ("ck_delivery_artifacts__target_kind", _any("target_kind", artifacts)),
        ("ck_delivery_platform_claims__platform_kind", _any("platform_kind", platforms)),
        ("ck_delivery_targets__platform_kind", _any("platform_kind", platforms)),
    ]


def _client(c: Connection) -> tuple[UUID, UUID]:
    """A new client and its workspace, the connection in the client's context (owner rights)."""
    tenant = add_tenant(c)
    ws = add_workspace(c, tenant)
    c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
    return tenant, ws


def _claim(c: Connection, kind: str) -> UUID:
    tenant, _ = _client(c)
    c.execute(
        text(
            "INSERT INTO shifde.delivery_platform_claims (id, tenant_id, platform_kind, "
            "address_key, created_by) VALUES (:i, :t, :k, :a, :i)"
        ),
        {"i": uuid7(), "t": tenant, "k": kind, "a": "a" * 64},
    )
    return tenant


def _target(c: Connection, kind: str) -> UUID:
    tenant, ws = _client(c)
    c.execute(
        text(
            "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, name, "
            "platform_kind, address, claim_id, environment, bindings, credential_ref, status, "
            "created_by) VALUES (:i, :t, :w, 'x', :k, 'https://x', NULL, 'dev', '{}', 'ref', "
            "'archived', :i)"
        ),
        {"i": uuid7(), "t": tenant, "w": ws, "k": kind},
    )
    return tenant


def _artifact(c: Connection, kind: str) -> UUID:
    tenant, _ = _client(c)
    c.execute(
        text(
            "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, size, "
            "content, created_by) VALUES (:t, :i, :k, :d, 1, :c, :i)"
        ),
        {"i": uuid7(), "t": tenant, "k": TARGET_KINDS[kind], "d": "e" * 64, "c": b"\x00"},
    )
    return tenant


ROWS: dict[str, Callable[[Connection, str], UUID]] = {
    "claim": _claim,
    "target": _target,
    "artifact": _artifact,
}


@pytest.mark.parametrize("kind", TARGET_KINDS)
@pytest.mark.parametrize("row", ROWS)
def test_the_downgrade_is_refused_while_one_new_kind_row_exists(
    run_id: str, admin_url: str, owner_url: str, app_url: str, kind: str, row: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, f"dlv{kind[:2]}{row[:2]}") as url:
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.begin() as c:
                tenant = ROWS[row](c, kind)
            with engine.begin() as c:  # a client with no such row
                _target(c, "databricks")
            with pytest.raises(DBAPIError) as err:
                command.downgrade(cfg, "delivery_0007")
            message = str(err.value.orig).splitlines()[0]
            assert message == (
                "delivery_0008: 1 Fabric, Palantir or Docker claim, target or artifact row(s) exist"
            )
            assert str(tenant) not in str(err.value)
            with engine.connect() as c:  # the downgrade was refused as a whole
                assert [tuple(r) for r in c.execute(text(_CHECKS))] == _expected(
                    PLATFORM_KINDS, ARTIFACT_KINDS
                )
        finally:
            engine.dispose()


def test_the_downgrade_restores_the_two_value_checks_without_new_kind_rows(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "dlvdown") as url:
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.begin() as c:
                _claim(c, "databricks")
            with engine.begin() as c:
                _target(c, "snowflake")
            command.downgrade(cfg, "delivery_0007")
            with engine.connect() as c:
                assert [tuple(r) for r in c.execute(text(_CHECKS))] == _expected(
                    PLATFORM_KINDS[:2], ARTIFACT_KINDS[:2]
                )
            for row in ROWS.values():
                with pytest.raises(DBAPIError) as err, engine.begin() as c:
                    row(c, "fabric")
                assert getattr(err.value.orig, "sqlstate", None) == "23514", err.value
            command.upgrade(cfg, "heads")
            for row in ROWS.values():
                with engine.begin() as c:
                    row(c, "docker")
        finally:
            engine.dispose()
