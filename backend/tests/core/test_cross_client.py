"""Core has no routes. This pins what every route test later relies on: the client context lives
in the transaction. Tenant A set in one transaction is gone in the next transaction on the same
pooled connection, and the connection stays the unprivileged application role."""

from __future__ import annotations

from sqlalchemy import text

from shifde.core.db import Database, get_context, set_tenant
from shifde.core.ids import uuid7

CURRENT = text(
    "SELECT shifde.current_tenant_id(), shifde.current_principal_id(),"
    " shifde.is_platform_admin_ctx(), current_user"
)


def test_tenant_set_in_one_transaction_is_gone_in_the_next_on_the_same_connection(
    app_url: str,
) -> None:
    tenant_a = uuid7()
    db = Database(app_url, pool_size=1, max_overflow=0)  # one connection: reuse is guaranteed
    try:
        with db.session() as s:
            set_tenant(s, tenant_a)
            row = s.execute(CURRENT).one()
            assert row[0] == tenant_a
            pid_a = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
            s.commit()
        with db.session() as s:
            pid_b = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert pid_b == pid_a  # same pooled connection
            row = s.execute(CURRENT).one()
            assert row[0] is None and row[1] is None and row[2] is False
            assert row[3] == "shifde_app"
            assert get_context(s).tenant_id is None
    finally:
        db.dispose()


def test_a_rolled_back_transaction_leaves_no_tenant_behind(app_url: str) -> None:
    db = Database(app_url, pool_size=1, max_overflow=0)
    try:
        with db.session() as s:
            set_tenant(s, uuid7())
            s.execute(CURRENT)
            s.rollback()
        with db.session() as s:
            assert s.execute(CURRENT).one()[0] is None
    finally:
        db.dispose()
