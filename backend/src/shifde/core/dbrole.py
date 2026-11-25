"""Startup check on the database role the application connects as.

Client isolation rests on PostgreSQL row-level security, which a privileged role escapes: a
superuser or a `BYPASSRLS` role ignores every policy; the owner of a table, schema, function
or database can switch the policies off or rewrite them; a `CREATEROLE` role can grant itself
such a role (PostgreSQL 15 and earlier) and a `REPLICATION` role can stream every row; a role that
may `CREATE` in a schema or the database can plant objects (or a schema named after itself, first
on the default `"$user"` search path) that shadow names other code resolves. The API (and
later the worker) must therefore run as `shifde_app`, which is none of these. `check_app_role`
refuses any connection whose role -- or any role it is a member of, directly or indirectly, i.e.
any role it could `SET ROLE` to -- is privileged in one of those ways.

It also refuses a connection that starts with a tenant context already set. The policy helpers
fail closed only when `app.tenant_id`, `app.principal_id` and `app.platform_admin` are unset; a
value preset through `ALTER ROLE/DATABASE ... SET`, the connection's `options`, `PGOPTIONS` or
`postgresql.conf` would be what every transaction without its own context runs as, and what
`set_config(..., true)` reverts to. `assert_no_preset_context` is the same check for one DBAPI
connection; `shifde.core.db.Database` runs it on every pool checkout.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

# Predefined roles that read or write server files or run server programs. Any of them lets the
# holder read the table files directly, around row-level security.
SERVER_ACCESS_ROLES: tuple[str, ...] = (
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
)

# Predefined roles that read other sessions' statistics, including the text of their queries
# (which can carry client data in literals). pg_monitor is a member of pg_read_all_stats.
STATISTICS_ROLES: tuple[str, ...] = ("pg_read_all_stats", "pg_monitor")

# Predefined roles with rights on every table: SELECT / INSERT, UPDATE, DELETE on all of them
# (row-level security still applies, but every grant this codebase withholds is given), or
# MAINTAIN (VACUUM, ANALYZE, REFRESH MATERIALIZED VIEW, LOCK, ...) on all of them.
DATA_ROLES: tuple[str, ...] = ("pg_read_all_data", "pg_write_all_data", "pg_maintain")

# The per-transaction context settings the row-level security helpers read (base_0001).
CONTEXT_SETTINGS: tuple[str, ...] = ("app.tenant_id", "app.principal_id", "app.platform_admin")

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
        SELECT r.oid, r.rolname, r.rolsuper, r.rolbypassrls, r.rolreplication, r.rolcreaterole
        FROM pg_roles r CROSS JOIN me
        WHERE pg_has_role(me.me_oid, r.oid, 'MEMBER')
    )
    SELECT
        r.rolname,
        r.rolname = session_user AS is_self,
        r.rolsuper,
        r.rolbypassrls,
        r.rolreplication,
        r.rolcreaterole,
        r.rolname = ANY(:server_roles) AS server_access,
        r.rolname = ANY(:stats_roles) AS stats_access,
        r.rolname = ANY(:data_roles) AS data_access,
        pg_catalog.has_database_privilege(
            r.oid, pg_catalog.current_database(), 'CREATE') AS create_database,
        (SELECT min(quote_ident(n.nspname) || '.' || quote_ident(c.relname))
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE c.relowner = r.oid AND {_USER_SCHEMA.format(n="n")}) AS owned_relation,
        (SELECT min(quote_ident(n.nspname))
           FROM pg_namespace n
          WHERE n.nspowner = r.oid AND {_USER_SCHEMA.format(n="n")}) AS owned_schema,
        (SELECT min(quote_ident(n.nspname) || '.' || quote_ident(p.proname))
           FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
          WHERE p.proowner = r.oid AND {_USER_SCHEMA.format(n="n")}) AS owned_function,
        (SELECT min(quote_ident(n.nspname))
           FROM pg_namespace n
          WHERE pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE')
            AND {_USER_SCHEMA.format(n="n")}) AS create_schema,
        EXISTS (SELECT 1 FROM pg_database d
                 WHERE d.datname = current_database() AND d.datdba = r.oid) AS owns_database
    FROM reachable r
    ORDER BY is_self DESC, r.rolname
    """  # nosec B608  # _USER_SCHEMA is a module constant; values are bound
)


# Rows of pg_db_role_setting that apply to this session: ALTER ROLE <session_user> [IN DATABASE
# <this>] SET, ALTER DATABASE <this> SET, and ALTER ROLE ALL [IN DATABASE <this>] SET.
_PRESET_SQL = text(
    """
    SELECT CASE WHEN s.setrole = 0 AND s.setdatabase = 0 THEN 'ALTER ROLE ALL'
                WHEN s.setrole = 0 THEN 'ALTER DATABASE'
                ELSE 'ALTER ROLE' END AS source,
           pg_catalog.lower(pg_catalog.split_part(cfg, '=', 1)) AS name
      FROM pg_catalog.pg_db_role_setting s
     CROSS JOIN LATERAL pg_catalog.unnest(s.setconfig) AS cfg
     WHERE s.setdatabase IN (0, (SELECT oid FROM pg_catalog.pg_database
                                  WHERE datname = pg_catalog.current_database()))
       AND s.setrole IN (0, (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = session_user))
       AND pg_catalog.lower(cfg) LIKE 'app.%'
     ORDER BY 1, 2
    """
)

_CONTEXT_SQL = "SELECT " + ", ".join(
    f"pg_catalog.current_setting('{name}', true)" for name in CONTEXT_SETTINGS
)


def preset_context(values: tuple[Any, ...]) -> list[str]:
    """Names of the `CONTEXT_SETTINGS` whose value (in that order) is set, i.e. not NULL/''."""
    return [name for name, value in zip(CONTEXT_SETTINGS, values, strict=True) if value]


class UnsafeDatabaseRole(Exception):
    """The connected database role could bypass or rewrite row-level security."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_app_role(engine: Engine) -> None:
    """Raise `UnsafeDatabaseRole` unless the role `engine` connects as is safe to serve from.

    Refused when the role, or any role it is a member of (directly or indirectly):
    is a superuser; has BYPASSRLS, REPLICATION or CREATEROLE; owns a relation, schema or function
    outside PostgreSQL's own schemas (this includes the `public` schema); may CREATE in such a
    schema or in the database itself; owns the current database; or is one of the predefined
    server-file / server-program / statistics / all-data / maintain roles. Also refused when a
    fresh connection already carries one of the `CONTEXT_SETTINGS`, or `pg_db_role_setting`
    presets any `app.*` setting for this role or database. Every reason found is reported; the
    message names roles, objects and setting names only, never connection details or setting
    values."""
    with engine.connect() as conn:
        # First statement on the connection: nothing has set a context yet.
        preset = preset_context(tuple(conn.execute(text(_CONTEXT_SQL)).one()))
        rows = conn.execute(
            _ROLE_SQL,
            {
                "server_roles": list(SERVER_ACCESS_ROLES),
                "stats_roles": list(STATISTICS_ROLES),
                "data_roles": list(DATA_ROLES),
            },
        ).all()
        presets = conn.execute(_PRESET_SQL).all()
        session_role = conn.execute(text("SELECT session_user")).scalar_one()
    if not rows:  # pragma: no cover - session_user always has a pg_roles row
        raise UnsafeDatabaseRole(f"role {session_role!r} not found in pg_roles")
    reasons: list[str] = [f"{name} is preset on a new connection" for name in preset]
    reasons += [f"{row.source} ... SET {row.name}" for row in presets]
    for row in rows:
        who = f"role {row.rolname!r}" if row.is_self else f"member of role {row.rolname!r}"
        if row.rolsuper:
            reasons.append(f"{who} is a superuser")
        if row.rolbypassrls:
            reasons.append(f"{who} has BYPASSRLS")
        if row.rolreplication:
            reasons.append(f"{who} has REPLICATION")
        if row.rolcreaterole:
            reasons.append(f"{who} has CREATEROLE")
        if row.server_access:
            reasons.append(f"{who} grants server file/program access")
        if row.stats_access:
            reasons.append(f"{who} reads other sessions' statistics")
        if row.data_access:
            reasons.append(f"{who} has rights on every table")
        if row.create_database:
            reasons.append(f"{who} may create schemas in the database")
        if row.create_schema:
            reasons.append(f"{who} may create objects in schema {row.create_schema}")
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


def sees_all_rows(session: Session) -> bool:
    """True when the current (effective) role ignores row-level security: a superuser or a
    `BYPASSRLS` role. Read only; BYPASSRLS is never inherited through role membership."""
    return bool(
        session.execute(
            text(
                "SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles "
                "WHERE rolname = current_user"
            )
        ).scalar_one()
    )


def check_database_url(url: str) -> None:
    """`check_app_role` for a connection URL, on a short-lived engine that is always disposed."""
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        check_app_role(engine)
    finally:
        engine.dispose()


def assert_no_preset_context(dbapi_connection: Any) -> None:
    """Raise `UnsafeDatabaseRole` if the DBAPI (psycopg) connection carries a tenant context
    outside a transaction. Leaves the connection as it found it (no open transaction)."""
    autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(_CONTEXT_SQL)
            row = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        dbapi_connection.autocommit = autocommit
    preset = preset_context(tuple(row or ()))
    if preset:
        raise UnsafeDatabaseRole(
            "database connection carries a preset tenant context: "
            + "; ".join(f"{name} is preset on a new connection" for name in preset)
        )
