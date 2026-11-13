"""Startup check on the database role the application connects as.

Client isolation rests on PostgreSQL row-level security, which a privileged role escapes: a
superuser or a `BYPASSRLS` role ignores every policy, and the owner of a table, schema, function
or database can switch the policies off or rewrite them. The API (and later the worker) must
therefore run as `shifde_app`, which is none of these. `check_app_role` refuses any connection
whose role -- or any role it is a member of, directly or indirectly, i.e. any role it could
`SET ROLE` to -- is privileged in one of those ways.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

# Predefined roles that read or write server files or run server programs. Any of them lets the
# holder read the table files directly, around row-level security.
SERVER_ACCESS_ROLES: tuple[str, ...] = (
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
)

# Schemas that belong to PostgreSQL itself. Temporary schemas (`pg_temp_N`, `pg_toast_temp_N`)
# are per-session scratch space: owning a temporary table grants nothing over other data.
_USER_SCHEMA = (
    "{n}.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND {n}.nspname NOT LIKE 'pg\\_toast%'"
    " AND {n}.nspname NOT LIKE 'pg\\_temp\\_%'"
)

_ROLE_SQL = text(
    f"""
    WITH me AS (
        SELECT oid AS me_oid FROM pg_roles WHERE rolname = session_user
    ),
    reachable AS (
        SELECT r.oid, r.rolname, r.rolsuper, r.rolbypassrls
        FROM pg_roles r CROSS JOIN me
        WHERE pg_has_role(me.me_oid, r.oid, 'MEMBER')
    )
    SELECT
        r.rolname,
        r.rolname = session_user AS is_self,
        r.rolsuper,
        r.rolbypassrls,
        r.rolname = ANY(:server_roles) AS server_access,
        (SELECT min(quote_ident(n.nspname) || '.' || quote_ident(c.relname))
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE c.relowner = r.oid AND {_USER_SCHEMA.format(n="n")}) AS owned_relation,
        (SELECT min(quote_ident(n.nspname))
           FROM pg_namespace n
          WHERE n.nspowner = r.oid AND {_USER_SCHEMA.format(n="n")}) AS owned_schema,
        (SELECT min(quote_ident(n.nspname) || '.' || quote_ident(p.proname))
           FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
          WHERE p.proowner = r.oid AND {_USER_SCHEMA.format(n="n")}) AS owned_function,
        EXISTS (SELECT 1 FROM pg_database d
                 WHERE d.datname = current_database() AND d.datdba = r.oid) AS owns_database
    FROM reachable r
    ORDER BY is_self DESC, r.rolname
    """
)


class UnsafeDatabaseRole(Exception):
    """The connected database role could bypass or rewrite row-level security."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_app_role(engine: Engine) -> None:
    """Raise `UnsafeDatabaseRole` unless the role `engine` connects as is safe to serve from.

    Refused when the role, or any role it is a member of (directly or indirectly):
    is a superuser; has BYPASSRLS; owns a relation, schema or function outside PostgreSQL's own
    schemas (this includes the `public` schema); owns the current database; or is one of the
    predefined server-file / server-program roles. Every reason found is reported; the message
    names roles and objects only, never connection details."""
    with engine.connect() as conn:
        rows = conn.execute(_ROLE_SQL, {"server_roles": list(SERVER_ACCESS_ROLES)}).all()
        session_role = conn.execute(text("SELECT session_user")).scalar_one()
    if not rows:  # pragma: no cover - session_user always has a pg_roles row
        raise UnsafeDatabaseRole(f"role {session_role!r} not found in pg_roles")
    reasons: list[str] = []
    for row in rows:
        who = f"role {row.rolname!r}" if row.is_self else f"member of role {row.rolname!r}"
        if row.rolsuper:
            reasons.append(f"{who} is a superuser")
        if row.rolbypassrls:
            reasons.append(f"{who} has BYPASSRLS")
        if row.server_access:
            reasons.append(f"{who} grants server file/program access")
        if row.owns_database:
            reasons.append(f"{who} owns the database")
        if row.owned_schema:
            reasons.append(f"{who} owns schema {row.owned_schema}")
        if row.owned_relation:
            reasons.append(f"{who} owns relation {row.owned_relation}")
        if row.owned_function:
            reasons.append(f"{who} owns function {row.owned_function}")
    if reasons:
        raise UnsafeDatabaseRole(
            f"database role {session_role!r} may bypass row-level security: " + "; ".join(reasons)
        )


def check_database_url(url: str) -> None:
    """`check_app_role` for a connection URL, on a short-lived engine that is always disposed."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        check_app_role(engine)
    finally:
        engine.dispose()
