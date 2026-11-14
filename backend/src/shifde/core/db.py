"""Database engine, per-request tenant context, and migration-state helpers.

Tenant context (`app.tenant_id`, `app.principal_id`, `app.request_id`,
`app.platform_admin`) is applied
with ``set_config(name, value, true)`` -- transaction-local, never session-level -- in a Session
``after_begin`` hook, so a pooled connection can never carry context into another transaction.

`Database` also guards the pool (isolation review 1, items 4-6; review 2, item 8): it refuses to
build on a role that could bypass row-level security (`shifde.core.dbrole.check_app_role`), and
on every pool checkout it runs ``DISCARD ALL`` (dropping any session-level setting, temporary
object, prepared statement, `SET ROLE` or `LISTEN` a previous user left behind) and then refuses
a connection that still carries `app.tenant_id`, `app.principal_id` or `app.platform_admin` --
i.e. one preset by the role, the database, the connection options or the server configuration.
A refused connection is discarded, so the refusal repeats until the preset is removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, MetaData, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, SessionTransaction, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from shifde.core.dbrole import assert_no_preset_context, check_app_role

SCHEMA = "shifde"
_CTX_KEY = "shifde_ctx"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


@dataclass(frozen=True)
class DbContext:
    tenant_id: UUID | None = None
    principal_id: UUID | None = None
    request_id: str | None = None
    platform_admin: bool = False


_APPLY_SQL = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true),"
    " set_config('app.principal_id', :principal_id, true),"
    " set_config('app.request_id', :request_id, true),"
    " set_config('app.platform_admin', :platform_admin, true)"
)


def _params(ctx: DbContext) -> dict[str, str]:
    return {
        "tenant_id": str(ctx.tenant_id) if ctx.tenant_id else "",
        "principal_id": str(ctx.principal_id) if ctx.principal_id else "",
        "request_id": ctx.request_id or "",
        "platform_admin": "on" if ctx.platform_admin else "",
    }


@event.listens_for(Session, "after_begin")
def _apply_context_on_begin(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    ctx = session.info.get(_CTX_KEY)
    if ctx is not None:
        connection.execute(_APPLY_SQL, _params(ctx))


def get_context(session: Session) -> DbContext:
    ctx = session.info.get(_CTX_KEY)
    return ctx if isinstance(ctx, DbContext) else DbContext()


def _update(session: Session, **changes: object) -> None:
    ctx = replace(get_context(session), **changes)  # type: ignore[arg-type]
    session.info[_CTX_KEY] = ctx
    # Apply immediately to the current (or newly begun) transaction; later transactions on
    # this Session re-apply from `session.info` via the after_begin hook.
    session.execute(_APPLY_SQL, _params(ctx))


def init_context(session: Session, request_id: str | None) -> None:
    """Pre-load context for later transactions without touching the database."""
    session.info[_CTX_KEY] = DbContext(request_id=request_id)


def set_tenant(session: Session, tenant_id: UUID | None) -> None:
    _update(session, tenant_id=tenant_id)


def set_principal(session: Session, principal_id: UUID | None) -> None:
    _update(session, principal_id=principal_id)


def set_request_id(session: Session, request_id: str | None) -> None:
    _update(session, request_id=request_id)


def set_platform_admin(session: Session, enabled: bool) -> None:
    """Only call after the PDP has confirmed ``platform:admin`` for the principal."""
    _update(session, platform_admin=enabled)


def forget_platform_admin(session: Session) -> None:
    """Drop the flag from the Session's context without touching the database (the next
    transaction applies the context again)."""
    session.info[_CTX_KEY] = replace(get_context(session), platform_admin=False)


@contextmanager
def platform_admin_context(session: Session) -> Iterator[None]:
    set_platform_admin(session, True)
    try:
        yield
    finally:
        # Always forget the flag for later transactions. Only touch the database if the
        # transaction is still usable: after a failed statement PostgreSQL rejects further
        # commands, and issuing set_config here would mask the original error. (The rollback
        # that follows discards the transaction-local setting anyway.)
        session.info[_CTX_KEY] = replace(get_context(session), platform_admin=False)
        if session.is_active:
            with suppress(SQLAlchemyError):
                session.execute(_APPLY_SQL, _params(get_context(session)))


def _clean_checkout(
    dbapi_connection: Any, connection_record: ConnectionPoolEntry, connection_proxy: object
) -> None:
    """Pool `checkout` hook: discard session state, then refuse a preset context. SETH keeps no
    session state across a checkout (advisory locks are transaction-level, LISTEN uses its own
    connection); psycopg never prepares statements here (`prepare_threshold=None`, B-603), because
    its cache could outlive the server-side statements DISCARD ALL drops.
    Raising here makes SQLAlchemy invalidate (close) the connection."""
    autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        dbapi_connection.execute("DISCARD ALL")
    finally:
        dbapi_connection.autocommit = autocommit
    assert_no_preset_context(dbapi_connection)


class Database:
    """Owns the engine and hands out Sessions (one per request / unit of work)."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 5,
        verify_role: bool = True,
        connect_timeout: int = 10,
        statement_timeout_ms: int = 0,
    ) -> None:
        """`verify_role=False` only for the schema owner's own tooling (migrations, test
        fixtures); everything that serves requests or runs jobs keeps the default.
        `connect_timeout` (seconds, libpq) bounds a connection attempt to an unreachable server and
        `statement_timeout_ms` (0 = none) the run time of any one statement (audit Item 18); both
        are startup parameters, so they survive the checkout `DISCARD ALL`."""
        options = "-c timezone=UTC"  # timestamps are always UTC
        if statement_timeout_ms > 0:
            options += f" -c statement_timeout={int(statement_timeout_ms)}"
        self.engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args={
                "options": options,
                "connect_timeout": connect_timeout,
                # The checkout hook's DISCARD ALL drops server-side prepared statements but
                # psycopg's cache outlived it after a commit: never prepare (DECISIONS B-603).
                "prepare_threshold": None,
            },
        )
        event.listen(self.engine, "checkout", _clean_checkout)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)
        if verify_role:
            try:
                check_app_role(self.engine)
            except BaseException:
                self.engine.dispose()
                raise

    def session(self) -> Session:
        return self._factory()

    def dispose(self) -> None:
        self.engine.dispose()


def read_db_revisions(session: Session) -> frozenset[str]:
    """Every revision stamped in the database (one per migration branch head, DECISIONS B-004)."""
    rows = session.execute(text(f"SELECT version_num FROM {SCHEMA}.alembic_version")).scalars()
    return frozenset(str(r) for r in rows)


def read_db_revision(session: Session) -> str | None:
    row = session.execute(text(f"SELECT version_num FROM {SCHEMA}.alembic_version")).first()
    return str(row[0]) if row else None
