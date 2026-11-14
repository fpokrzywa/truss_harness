"""B-703: `session.info` follows the database across savepoints. A rolled-back savepoint discards
the transaction-local settings it made, so the Session's context returns to the value the savepoint
began with; a released savepoint keeps them. Run as `shifde_app`."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import (
    Database,
    get_context,
    platform_admin_context,
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


# Whole-transaction rollback with a savepoint open (isolation review 1 of core-ctx, items 1-2):
# `Session.rollback()` rolls the savepoint back first, but the database then holds nothing, so
# the Session keeps its current (possibly lowered) context and never a higher one.


def _next_transaction_is_not_admin(s: Session) -> None:
    assert get_context(s).platform_admin is False
    assert s.execute(_DB).one()[2] is False
    s.rollback()


def test_session_rollback_with_a_savepoint_open_keeps_a_lowered_admin_flag(
    app_db: Database,
) -> None:
    s = app_db.session()
    set_platform_admin(s, True)
    s.begin_nested()
    set_platform_admin(s, False)
    s.rollback()
    _next_transaction_is_not_admin(s)
    s.close()


def test_session_rollback_keeps_the_fail_closed_value_of_a_refused_lowering(
    app_db: Database,
) -> None:
    s = app_db.session()
    set_platform_admin(s, True)
    s.begin_nested()
    with pytest.raises(DBAPIError):
        s.execute(text("SELECT 1/0"))
    with pytest.raises(DBAPIError):
        set_platform_admin(s, False)
    s.rollback()
    _next_transaction_is_not_admin(s)
    s.close()


def test_a_raising_savepoint_inside_platform_admin_context_leaves_no_admin_flag(
    app_db: Database,
) -> None:
    s = app_db.session()
    with pytest.raises(ZeroDivisionError), platform_admin_context(s):
        s.begin_nested()
        raise ZeroDivisionError
    s.rollback()
    _next_transaction_is_not_admin(s)
    s.close()


@pytest.mark.parametrize("to", ["other", "none"])
def test_session_rollback_with_a_savepoint_open_keeps_the_current_tenant(
    app_db: Database, to: str
) -> None:
    a = uuid7()
    new = uuid7() if to == "other" else None
    s = app_db.session()
    set_tenant(s, a)
    s.begin_nested()
    set_tenant(s, new)
    s.rollback()
    assert get_context(s).tenant_id == new
    assert s.execute(_DB).one()[0] == new
    s.rollback()
    s.close()


def test_a_savepoint_context_manager_leaving_by_exception_restores_the_context(
    app_db: Database,
) -> None:
    a = uuid7()
    s = app_db.session()
    set_tenant(s, a)
    set_platform_admin(s, True)
    with pytest.raises(ZeroDivisionError), s.begin_nested():
        set_tenant(s, uuid7())
        set_platform_admin(s, False)
        raise ZeroDivisionError
    ctx = get_context(s)
    assert (ctx.tenant_id, ctx.platform_admin) == (a, True)
    _assert_in_sync(s)
    s.rollback()
    s.close()
