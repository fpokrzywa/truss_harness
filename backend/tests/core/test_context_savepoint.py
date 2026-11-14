"""B-703: `session.info` follows the database across savepoints. A rolled-back savepoint discards
the transaction-local settings it made, so the Session's context returns to the value the savepoint
began with; a released savepoint keeps them. Run as `shifde_app`."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import (
    Database,
    get_context,
    set_platform_admin,
    set_principal,
    set_tenant,
)
from shifde.core.ids import uuid7

_DB = text(
    "SELECT shifde.current_tenant_id(), shifde.current_principal_id(),"
    " shifde.is_platform_admin_ctx()"
)


def _assert_in_sync(s: Session) -> None:
    tenant, principal, admin = s.execute(_DB).one()
    ctx = get_context(s)
    assert (ctx.tenant_id, ctx.principal_id, ctx.platform_admin) == (tenant, principal, admin)


def _changes() -> list[Callable[[Session], None]]:
    return [
        lambda s: set_tenant(s, uuid7()),
        lambda s: set_tenant(s, None),
        lambda s: set_principal(s, uuid7()),
        lambda s: set_principal(s, None),
        lambda s: set_platform_admin(s, True),
        lambda s: set_platform_admin(s, False),
    ]


@pytest.mark.parametrize(
    "idx",
    range(6),
    ids=["tenant-b", "tenant-none", "principal-b", "principal-none", "admin-on", "admin-off"],
)
def test_a_rolled_back_savepoint_restores_the_context(app_db: Database, idx: int) -> None:
    s = app_db.session()
    set_tenant(s, uuid7())
    set_principal(s, uuid7())
    set_platform_admin(s, idx == 5)
    before = get_context(s)
    sp = s.begin_nested()
    _changes()[idx](s)
    _assert_in_sync(s)
    sp.rollback()
    assert get_context(s) == before
    _assert_in_sync(s)
    s.rollback()
    s.close()


def test_nested_savepoints_each_restore_their_own_context(app_db: Database) -> None:
    a, b, c = uuid7(), uuid7(), uuid7()
    s = app_db.session()
    set_tenant(s, a)
    outer = s.begin_nested()
    set_tenant(s, b)
    inner = s.begin_nested()
    set_tenant(s, c)
    inner.rollback()
    assert get_context(s).tenant_id == b
    _assert_in_sync(s)
    outer.rollback()
    assert get_context(s).tenant_id == a
    _assert_in_sync(s)
    s.rollback()
    s.close()


def test_a_released_savepoint_keeps_the_new_context(app_db: Database) -> None:
    b = uuid7()
    s = app_db.session()
    set_tenant(s, uuid7())
    sp = s.begin_nested()
    set_tenant(s, b)
    sp.commit()
    assert get_context(s).tenant_id == b
    _assert_in_sync(s)
    s.commit()
    assert get_context(s).tenant_id == b
    assert s.execute(_DB).one()[0] == b
    s.rollback()
    s.close()
