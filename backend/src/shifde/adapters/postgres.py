"""`postgres` data-platform adapter (psycopg 3).

* Connects to the address pinned by the EgressGuard (libpq ``hostaddr``) while keeping the host
  name for TLS verification.
* Queries run in a READ ONLY transaction with a statement timeout and a row cap unless the caller
  was granted write access (access_mode ``read_write`` + ``connection:write``).
* Query plans compile to parameterized SQL with every identifier quoted.
"""

from __future__ import annotations

import datetime
import decimal
import os
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any, ClassVar  # Any: database values and JSON documents

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from shifde.adapters.base import (
    AdapterDescriptor,
    AdapterFailure,
    Capability,
    CheckRecorder,
    CompiledQuery,
    ConfigError,
    ConnectionConfig,
    DiscoveryResult,
    ErrorClass,
    ExecContext,
    FilterGroup,
    FilterNode,
    InvokeResult,
    ProbeOutcome,
    SourceQueryPlan,
    validate_against_schema,
)
from shifde.adapters.httputil import classify_exception
from shifde.core.egress import EgressViolation
from shifde.core.sqlrules import WRITE_KEYWORDS

DEFAULT_MAX_ROWS = 1000
HARD_MAX_ROWS = 10_000
CURSOR_PREFIX = re.compile(r"^\s*(select|with|values|table)\b", re.I)  # DECLARE-able
DEFAULT_MAX_BYTES = 5_000_000
FETCH_BATCH = 256
READ_WORDS = frozenset({"select", "with", "show", "values", "table", "explain"})
# `SELECT ... INTO` creates a table
_REFUSED_WORDS = WRITE_KEYWORDS | {"into"}
# functions with side effects even inside a READ ONLY transaction (#285), functions that run a
# SQL string or cursor and server-file / large-object reads (#312); a best-effort deny-list
_REFUSED_FUNCTIONS = frozenset(
    {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_shared",
        "pg_advisory_unlock_all",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
        "set_config",
        "pg_reload_conf",
        "pg_notify",
        "lo_import",
        "lo_export",
        "dblink",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "ts_stat",
        "ts_rewrite",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_get",
    }
)
_REFUSED_FUNCTION_PREFIXES = ("dblink_",)
_WORD = re.compile(r"\w+")
_FIRST_WORD = re.compile(r"\s*(\w+)")
_LINE_END = re.compile(r"[\r\n]")
_POSITIONAL = re.compile(r"\$[0-9]+")
_NOT_READ = "only read statements are allowed (read_write access and connection:write needed)"
_UNTERMINATED = "the statement has an unterminated quote or comment"
_NESTED = "nested block comments are not allowed"
_BACKSLASH = "a backslash inside a string or quoted identifier is not allowed in a read statement"
_UNICODE_ESCAPE = "a Unicode escape (U&) is not allowed in a read statement"
_DOLLAR = "a $ is allowed only as a positional parameter ($1) in a read statement"
_OPS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "like": "LIKE"}


def check_read_only(sql: str) -> None:
    """Refuse (CAPABILITY) anything but a statement whose first word of code is a read keyword
    and whose code holds no word of `WRITE_KEYWORDS` and not `into`, and no side-effecting
    function of `_REFUSED_FUNCTIONS` or starting with `dblink_` (#285), no function that runs a
    SQL string or cursor (`query_to_xml`, `query_to_xmlschema`, `query_to_xml_and_xmlschema`,
    `cursor_to_xml`, `cursor_to_xmlschema`, `ts_stat`, `ts_rewrite`) and no server-file or
    large-object read (`pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_get`) (#312), as
    a word of code or as a whole `"..."` identifier. The function deny-list is best-effort: the
    least-privileged read credential (B-1507) and the READ ONLY transaction are the limiting
    controls. `'...'` strings, `"..."` identifiers (a doubled quote stays inside)
    and `--`/`/* */` comments are otherwise never read as code.
    Text Postgres could read differently is refused (fail closed): an unterminated quote or
    comment, a nested `/*`, a backslash in a string (E-strings, `standard_conforming_strings`)
    or in a `"..."` identifier, any `U&'...'`/`U&"..."` Unicode escape and the word `uescape`
    (whatever the escape character), and a `$` that is not a positional parameter (dollar
    quotes). Messages never echo the SQL; the READ ONLY transaction stays the
    backstop."""
    code: list[str] = []
    identifiers: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            if sql[max(i - 2, 0) : i].lower() == "u&":
                raise AdapterFailure(ErrorClass.CAPABILITY, _UNICODE_ESCAPE)
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if sql.startswith(ch, j + 1):
                        j += 2
                        continue
                    break
                if sql[j] == "\\":
                    raise AdapterFailure(ErrorClass.CAPABILITY, _BACKSLASH)
                j += 1
            if j >= n:
                raise AdapterFailure(ErrorClass.CAPABILITY, _UNTERMINATED)
            if ch == '"':
                identifiers.append(sql[i + 1 : j].replace('""', '"'))
            code.append(" ")
            i = j + 1
        elif sql.startswith("--", i):
            found = _LINE_END.search(sql, i)
            code.append(" ")
            i = n if found is None else found.start()
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise AdapterFailure(ErrorClass.CAPABILITY, _UNTERMINATED)
            if "/*" in sql[i + 2 : end]:
                raise AdapterFailure(ErrorClass.CAPABILITY, _NESTED)
            code.append(" ")
            i = end + 2
        elif ch == "$":
            param = _POSITIONAL.match(sql, i)
            if param is None or (i and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                raise AdapterFailure(ErrorClass.CAPABILITY, _DOLLAR)
            code.append(" ")
            i = param.end()
        else:
            code.append(ch)
            i += 1
    bare = "".join(code)
    first = _FIRST_WORD.match(bare)
    if first is None or first.group(1).lower() not in READ_WORDS:
        raise AdapterFailure(ErrorClass.CAPABILITY, _NOT_READ)
    words = [word.lower() for word in _WORD.findall(bare)]
    if "uescape" in words:
        raise AdapterFailure(ErrorClass.CAPABILITY, _UNICODE_ESCAPE)
    if any(word in _REFUSED_WORDS for word in words):
        raise AdapterFailure(ErrorClass.CAPABILITY, _NOT_READ)
    for name in words + [identifier.lower() for identifier in identifiers]:
        if name in _REFUSED_FUNCTIONS or name.startswith(_REFUSED_FUNCTION_PREFIXES):
            raise AdapterFailure(ErrorClass.CAPABILITY, _NOT_READ)


def _filter_columns(filters: Sequence[FilterNode]) -> list[str]:
    out: list[str] = []
    for f in filters:
        if isinstance(f, FilterGroup):
            out.extend(_filter_columns(f.items))
        else:
            out.append(f.column)
    return out


def _json_value(value: Any) -> Any:  # Any: database value
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    return value


def classify_pg(exc: BaseException) -> AdapterFailure:
    if isinstance(exc, AdapterFailure):
        return exc
    if isinstance(exc, EgressViolation):
        return classify_exception(exc)
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return AdapterFailure(ErrorClass.TIMEOUT, "the statement exceeded the time limit")
    if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
        return AdapterFailure(ErrorClass.CAPABILITY, "writes are not permitted on this connection")
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return AdapterFailure(ErrorClass.CAPABILITY, "the database role lacks the privilege")
    if isinstance(exc, psycopg.errors.Error):
        state = exc.sqlstate or ""
        if state.startswith("28"):
            return AdapterFailure(ErrorClass.AUTH, "the database rejected the credentials")
        if state == "3D000":
            return AdapterFailure(ErrorClass.CAPABILITY, "the database does not exist")
        if state.startswith(("42", "22", "23", "0A")):
            return AdapterFailure(ErrorClass.CAPABILITY, f"SQL error ({state})")
    if isinstance(exc, psycopg.OperationalError):
        text = str(exc).lower()
        if (
            "authentication failed" in text
            or "no pg_hba.conf entry" in text
            or "authentication method requirement" in text
            or "require_auth" in text
        ):
            return AdapterFailure(ErrorClass.AUTH, "the database rejected the credentials")
        if "ssl" in text or "certificate" in text:
            return AdapterFailure(ErrorClass.TLS, "TLS negotiation failed")
        if "timeout" in text or "timed out" in text:
            return AdapterFailure(ErrorClass.TIMEOUT, "the connection timed out")
        return AdapterFailure(ErrorClass.NETWORK, "could not connect to the database server")
    return AdapterFailure(ErrorClass.UPSTREAM, f"unexpected error: {type(exc).__name__}")


def _connect(ctx: ExecContext, conn: ConnectionConfig, *, write: bool) -> psycopg.Connection[Any]:
    cfg = conn.config
    target = ctx.egress.resolve_pinned(str(cfg["host"]), int(cfg.get("port", 5432)), "postgresql")
    timeout_ms = int(cfg.get("statement_timeout_ms", 15_000))
    left_call = ctx.egress.remaining()
    connect_s = ctx.egress.timeout_seconds
    if left_call is not None:
        if left_call <= 0:
            raise AdapterFailure(ErrorClass.TIMEOUT, "the call exceeded its deadline")
        timeout_ms = max(1, min(timeout_ms, int(left_call * 1000)))
        connect_s = min(connect_s, left_call)  # libpq's granularity is one second
    password = conn.credential.reveal() if conn.credential else None
    # Without a configured credential never fall back to the server's PGPASSWORD / ~/.pgpass:
    # `require_auth=none` makes libpq refuse any password challenge, `passfile` points nowhere.
    no_fallback: dict[str, Any] = (  # Any: libpq options
        {} if password is not None else {"require_auth": "none", "passfile": os.devnull + ".none"}
    )
    connection = psycopg.connect(
        host=target.host,
        hostaddr=target.ip,
        port=target.port,
        dbname=str(cfg["database"]),
        user=cfg.get("user") or None,
        password=password,
        sslmode=cfg.get("sslmode", "prefer"),
        connect_timeout=max(1, int(connect_s)),
        application_name="shifde",
        options=f"-c statement_timeout={timeout_ms} -c idle_in_transaction_session_timeout=30000",
        row_factory=tuple_row,
        **no_fallback,
    )
    connection.read_only = not write
    return connection


class PostgresSession:
    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        self._ctx = ctx
        self._conn = conn

    def invoke(self, request: dict[str, Any], *, write: bool = False) -> InvokeResult:
        statement = str(request.get("sql", "")).strip()
        params = request.get("params")
        if not statement:
            raise AdapterFailure(ErrorClass.CONFIG, "request needs `sql`")
        body = statement.rstrip(";").rstrip()
        if ";" in body:
            raise AdapterFailure(ErrorClass.CONFIG, "exactly one statement is allowed")
        if not write:
            check_read_only(body)
        max_rows = min(int(self._conn.config.get("max_rows", DEFAULT_MAX_ROWS)), HARD_MAX_ROWS)
        max_bytes = int(self._conn.config.get("max_result_bytes", DEFAULT_MAX_BYTES))
        return run_query(
            self._ctx, self._conn, body, params, max_rows=max_rows, write=write, max_bytes=max_bytes
        )

    def close(self) -> None:
        return None


def _row_size(row: tuple[Any, ...]) -> int:  # Any: database values
    return sum(len(str(v)) + 2 for v in row)


def run_query(
    ctx: ExecContext,
    conn: ConnectionConfig,
    statement: str,
    params: Any,  # Any: positional list or named mapping of bound values
    *,
    max_rows: int,
    write: bool,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> InvokeResult:
    """Run one statement. Reads use a SERVER-SIDE cursor and fetch in small batches, so the row
    and byte caps stop the transfer instead of truncating an already-downloaded result."""
    columns: list[str] = []
    rows: list[tuple[Any, ...]] = []
    truncated = False
    affected = 0
    budget = int(conn.config.get("statement_timeout_ms", 15_000)) / 1000
    left_call = ctx.egress.remaining()
    if left_call is not None:  # the caller's call deadline (a tool's `timeout_s`) also bounds it
        budget = min(budget, left_call)
    if budget <= 0:
        raise AdapterFailure(ErrorClass.TIMEOUT, "the call exceeded its deadline")
    deadline = time.monotonic() + budget

    def arm(pg: psycopg.Connection[Any]) -> None:
        """One overall budget: each DECLARE/FETCH gets only what is left of it."""
        left_ms = int((deadline - time.monotonic()) * 1000)
        if left_ms <= 0:
            raise AdapterFailure(ErrorClass.TIMEOUT, "the statement exceeded the time limit")
        pg.execute("SELECT set_config('statement_timeout', %s, true)", (str(left_ms),))

    try:
        with _connect(ctx, conn, write=write) as pg:
            # server-side cursor for plain reads only: writes (and data-modifying CTEs)
            # cannot be DECLAREd
            use_server_cursor = not write and bool(CURSOR_PREFIX.match(statement))
            with pg.cursor(name="shifde_cur") if use_server_cursor else pg.cursor() as cur:
                arm(pg)
                cur.execute(statement, params)
                if cur.description is None:
                    affected = max(cur.rowcount, 0)
                else:
                    columns = [d.name for d in cur.description]
                    size = 0
                    done = False
                    while not done:
                        arm(pg)
                        batch = cur.fetchmany(FETCH_BATCH)
                        if not batch:
                            break
                        for row in batch:
                            size += _row_size(row)
                            if len(rows) >= max_rows or size > max_bytes:
                                truncated = True
                                done = True
                                break
                            rows.append(row)
            pg.commit() if write else pg.rollback()
    except Exception as exc:  # noqa: BLE001 - classified; message never echoes SQL/params
        raise classify_pg(exc) from exc
    if not columns:
        return InvokeResult({"columns": [], "rows": [], "row_count": affected})
    return InvokeResult(
        {
            "columns": columns,
            "rows": [[_json_value(v) for v in row] for row in rows],
            "row_count": len(rows),
        },
        truncated=truncated,
    )


class PostgresAdapter:
    descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
        key="postgres",
        kind="connection",
        version="1.0.0",
        display_name="PostgreSQL",
        capabilities=frozenset({Capability.QUERY_SQL, Capability.DISCOVER_SCHEMA}),
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["host", "database"],
            "properties": {
                "host": {"type": "string", "minLength": 1, "title": "Host"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 5432},
                "database": {"type": "string", "minLength": 1, "title": "Database"},
                "user": {"type": "string", "title": "User"},
                "sslmode": {
                    "type": "string",
                    "enum": ["disable", "prefer", "require", "verify-ca", "verify-full"],
                    "default": "prefer",
                },
                "statement_timeout_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 300000,
                    "default": 15000,
                },
                "max_result_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 50000000,
                    "default": DEFAULT_MAX_BYTES,
                    "title": "Result size cap (bytes)",
                },
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": HARD_MAX_ROWS,
                    "default": DEFAULT_MAX_ROWS,
                },
                "schemas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "title": "Schemas to discover (empty = all non-system)",
                },
            },
        },
        secret_fields=("password",),
        destination_fields=("host", "port", "sslmode"),
        auth_methods=("none", "basic"),
        connection_type="database",
    )

    def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        return validate_against_schema(self.descriptor, config, extra=lambda _cfg: [])

    # ------------------------------------------------------------------ test
    def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
        rec = CheckRecorder()
        errors = self.validate_config(conn.config)
        if errors:
            rec.fail("config", "; ".join(f"{e.path}: {e.message}" for e in errors)[:400])
            return ProbeOutcome(rec.finish(), ErrorClass.CONFIG, "configuration is invalid")
        rec.ok("config", "configuration is valid")
        try:
            ctx.egress.resolve_pinned(
                str(conn.config["host"]), int(conn.config.get("port", 5432)), "postgresql"
            )
        except EgressViolation as exc:
            if exc.code == "dns":
                rec.ok("egress", "scheme, port and host are permitted")
                rec.fail("dns", str(exc))
            else:
                rec.fail("egress", str(exc))
            return ProbeOutcome(
                rec.finish(), ErrorClass.DNS if exc.code == "dns" else ErrorClass.EGRESS, str(exc)
            )
        rec.ok("egress", "scheme, port and host are permitted")
        rec.ok("dns", "resolved once; every address is permitted; connection is pinned")
        sslmode = conn.config.get("sslmode", "prefer")
        try:
            with _connect(ctx, conn, write=False) as pg, pg.cursor() as cur:
                tls_in_use = pg.pgconn.ssl_in_use
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute("SHOW transaction_read_only")
                read_only = (cur.fetchone() or ("",))[0]
                pg.rollback()
        except Exception as exc:  # noqa: BLE001 - classified
            failure = classify_pg(exc)
            if failure.error_class is ErrorClass.TLS:
                rec.fail("tls", failure.message)
            elif failure.error_class is ErrorClass.AUTH:
                rec.ok("tls", "transport negotiated")
                rec.fail("auth", failure.message)
            elif failure.error_class in (ErrorClass.NETWORK, ErrorClass.TIMEOUT):
                rec.fail("dns", failure.message)
            else:
                rec.ok("tls", "transport negotiated")
                rec.ok("auth", "credentials accepted")
                rec.fail("capability", failure.message)
            return ProbeOutcome(rec.finish(), failure.error_class, failure.message)
        rec.ok("tls", "TLS in use" if tls_in_use else f"no TLS (sslmode={sslmode})")
        rec.ok("auth", "credentials accepted")
        rec.ok("capability", f"SELECT 1 succeeded (default transaction_read_only={read_only})")
        return ProbeOutcome(rec.finish())

    # ------------------------------------------------------------------ discover
    def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
        wanted = list(conn.config.get("schemas") or [])
        statement = (
            "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, "
            "c.is_nullable = 'YES' "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_schema = c.table_schema "
            "AND t.table_name = c.table_name "
            "WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND c.table_schema NOT LIKE 'pg\\_%%' "
            "AND (cardinality(%s::text[]) = 0 OR c.table_schema = ANY(%s::text[])) "
            "ORDER BY c.table_schema, c.table_name, c.ordinal_position"
        )
        result = run_query(
            ctx, conn, statement, [wanted, wanted], max_rows=HARD_MAX_ROWS * 5, write=False
        )
        tables: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for schema, table, column, dtype, nullable in result.result["rows"]:
            tables.setdefault((schema, table), []).append(
                {"name": column, "type": dtype, "nullable": bool(nullable)}
            )
        payload = {
            "tables": [
                {"schema": s, "name": t, "columns": cols} for (s, t), cols in tables.items()
            ][:1000]
        }
        return DiscoveryResult("schema", payload)

    def open(self, ctx: ExecContext, conn: ConnectionConfig) -> PostgresSession:
        return PostgresSession(ctx, conn)

    # ------------------------------------------------------------------ DataPlatformAdapter
    def compile(self, plan: SourceQueryPlan) -> CompiledQuery:
        names = [plan.table, plan.schema or "", *plan.columns, *_filter_columns(plan.filters)]
        names += [c for c, _ in plan.order_by]
        if any("%" in n or "\x00" in n for n in names):
            raise AdapterFailure(ErrorClass.CONFIG, "identifiers must not contain '%' or NUL")
        table = (
            sql.Identifier(plan.schema, plan.table) if plan.schema else sql.Identifier(plan.table)
        )
        cols = (
            sql.SQL(", ").join(sql.Identifier(c) for c in plan.columns)
            if plan.columns
            else sql.SQL("*")
        )
        params: list[Any] = []
        clauses: list[sql.Composable] = []
        for f in plan.filters:
            clauses.append(self._filter(f, params))
        query = sql.SQL("SELECT {cols} FROM {table}").format(cols=cols, table=table)
        if clauses:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        if plan.order_by:
            order = sql.SQL(", ").join(
                sql.SQL("{} {}").format(
                    sql.Identifier(c), sql.SQL("DESC" if d == "desc" else "ASC")
                )
                for c, d in plan.order_by
            )
            query += sql.SQL(" ORDER BY ") + order
        if plan.limit is not None:
            if plan.limit < 0:
                raise AdapterFailure(ErrorClass.CONFIG, "limit must not be negative")
            query += sql.SQL(" LIMIT %s")
            params.append(int(plan.limit))
        if plan.offset is not None:
            if plan.offset < 0:
                raise AdapterFailure(ErrorClass.CONFIG, "offset must not be negative")
            query += sql.SQL(" OFFSET %s")
            params.append(int(plan.offset))
        return CompiledQuery(sql=query.as_string(), params=tuple(params))

    @staticmethod
    def _filter(f: FilterNode, params: list[Any]) -> sql.Composable:
        if isinstance(f, FilterGroup):
            if not f.items or (f.op == "not" and len(f.items) != 1):
                raise AdapterFailure(ErrorClass.CONFIG, "a filter group needs its operands")
            parts = [PostgresAdapter._filter(item, params) for item in f.items]
            if f.op == "not":
                return sql.SQL("(NOT {})").format(parts[0])
            glue = sql.SQL(" AND " if f.op == "and" else " OR ")
            return sql.SQL("({})").format(glue.join(parts))
        col = sql.Identifier(f.column)
        if f.op == "is_null":
            return sql.SQL("{} IS NULL").format(col)
        if f.op == "in":
            values = list(f.value or [])
            if not values:
                return sql.SQL("FALSE")
            params.append(values)
            return sql.SQL("{} = ANY(%s)").format(col)
        if f.op not in _OPS:
            raise AdapterFailure(ErrorClass.CONFIG, f"unsupported filter operator {f.op!r}")
        params.append(f.value)
        return sql.SQL("{} {} %s").format(col, sql.SQL(_OPS[f.op]))

    def execute(
        self, ctx: ExecContext, conn: ConnectionConfig, q: CompiledQuery, *, max_rows: int
    ) -> InvokeResult:
        return run_query(ctx, conn, q.sql, list(q.params), max_rows=max_rows, write=False)
