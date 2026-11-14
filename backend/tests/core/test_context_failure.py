"""B-702: a context change the database did not accept must not reach `session.info`, or every
later transaction on the Session would apply it (isolation review 1 of audit, item 2). Run as
`shifde_app`, with a statement failing first so the transaction is aborted."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import (
    Database,
    DbContext,
    get_context,
    platform_admin_context,
    set_platform_admin,
    set_principal,
    set_tenant,
)
from shifde.core.ids import uuid7

Change = Callable[[Session], None]


def _aborted(app_db: Database) -> Session:
    s = app_db.session()
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    with pytest.raises(DBAPIError):
        s.execute(text("SELECT 1/0"))
    return s


@pytest.mark.parametrize(
    "change",
    [
        lambda s: set_platform_admin(s, True),
        lambda s: set_tenant(s, uuid7()),
        lambda s: set_principal(s, uuid7()),
    ],
    ids=["platform-admin", "tenant", "principal"],
)
def test_a_failed_context_change_leaves_the_session_context_unchanged(
    app_db: Database, change: Change
) -> None:
    s = _aborted(app_db)
    before = get_context(s)
    with pytest.raises(DBAPIError):
        change(s)
    assert get_context(s) == before
    s.rollback()
    assert get_context(s) == before
    assert s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one() is False
    s.rollback()
    s.close()


def test_platform_admin_context_that_cannot_start_leaves_no_flag(app_db: Database) -> None:
    s = _aborted(app_db)
    with pytest.raises(DBAPIError), platform_admin_context(s):
        pytest.fail("the body must not run")
    assert get_context(s).platform_admin is False
    s.rollback()
    assert s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one() is False
    s.rollback()
    s.close()


@pytest.mark.parametrize(
    ("setup", "lower", "expect"),
    [
        (
            lambda s, a, b: set_platform_admin(s, True),
            lambda s, a, b: set_platform_admin(s, False),
            lambda c, a, b: c.platform_admin is False,
        ),
        (
            lambda s, a, b: set_tenant(s, a),
            lambda s, a, b: set_tenant(s, None),
            lambda c, a, b: c.tenant_id is None,
        ),
        (
            lambda s, a, b: set_tenant(s, a),
            lambda s, a, b: set_tenant(s, b),
            lambda c, a, b: c.tenant_id is None,
        ),
        (
            lambda s, a, b: set_principal(s, a),
            lambda s, a, b: set_principal(s, None),
            lambda c, a, b: c.principal_id is None,
        ),
        (
            lambda s, a, b: set_principal(s, a),
            lambda s, a, b: set_principal(s, b),
            lambda c, a, b: c.principal_id is None,
        ),
    ],
    ids=[
        "lower-admin",
        "tenant-cleared",
        "tenant-switched",
        "principal-cleared",
        "principal-switched",
    ],
)
def test_a_failed_lowering_of_the_context_fails_closed(
    app_db: Database,
    setup: Callable[[Session, UUID, UUID], None],
    lower: Callable[[Session, UUID, UUID], None],
    expect: Callable[[DbContext, UUID, UUID], bool],
) -> None:
    """A change that cannot reach the database is stored as the lower of old and new (B-703), so
    the old, higher value is never re-applied by a later transaction."""
    a, b = uuid7(), uuid7()
    s = app_db.session()
    setup(s, a, b)
    with pytest.raises(DBAPIError):
        s.execute(text("SELECT 1/0"))
    with pytest.raises(DBAPIError):
        lower(s, a, b)
    assert expect(get_context(s), a, b)
    s.rollback()
    assert expect(get_context(s), a, b)
    assert s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one() is False
    if get_context(s).tenant_id is None:
        assert s.execute(text("SELECT shifde.current_tenant_id()")).scalar_one() is None
    if get_context(s).principal_id is None:
        assert s.execute(text("SELECT shifde.current_principal_id()")).scalar_one() is None
    s.rollback()
    s.close()
