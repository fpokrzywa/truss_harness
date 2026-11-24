"""#267: the Postgres `read` invoke check (`adapters/postgres.check_read_only`): a read first word
and no write keyword (or `into`) anywhere in the code, quotes and comments read the way Postgres
reads them, and ambiguous text refused. #285: no side-effecting function (backend kill/cancel,
advisory locks, `set_config`, `pg_notify`, large-object import/export, `dblink*`) as a word of
code or a `"..."` identifier, while a string or comment naming one still passes. #312: no
function that runs a SQL string or cursor (`query_to_xml*`, `cursor_to_xml*`, `ts_stat`,
`ts_rewrite`) and no server-file or large-object read (`pg_read_file`, `pg_read_binary_file`,
`pg_ls_dir`, `lo_get`). Pure: no database."""

from __future__ import annotations

from typing import Any  # Any: monkeypatched callables

import pytest

from shifde.adapters import postgres
from shifde.adapters.base import AdapterFailure, ConnectionConfig, ErrorClass, ExecContext
from shifde.adapters.postgres import PostgresSession, check_read_only

ACCEPTED = [
    "SELECT 1",
    "with x as (select 1) select * from x",
    "SHOW search_path",
    "VALUES (1)",
    "TABLE t",
    "EXPLAIN SELECT 1",
    "SELECT 'delete'",
    'SELECT "update" FROM t',
    "/* c */ SELECT 1",
    "SELECT $1",
    "SELECT 'it''s', \"a \"\" drop\" FROM t -- insert",
    "SELECT updated_at, deleted FROM t WHERE id = $2",
    # #285: a side-effecting function named only in a string or a comment
    "SELECT 'pg_terminate_backend(1)'",
    "SELECT 'pg_advisory_lock' -- set_config",
    "SELECT 1 -- pg_notify('c', 'x')",
    "SELECT 1 /* dblink_exec('x') lo_export(1, 'f') */",
    "SELECT my_set_config, pg_notify_count FROM t",
    # a bitwise & before a quoted identifier and UESCAPE in a string are not Unicode escapes
    'SELECT a & "b" FROM t',
    "SELECT 'U&\"x\" UESCAPE'",
    # #312: a SQL-running or file-reading function named only in a string, a comment or a word
    "SELECT 'query_to_xml(''x'', true, true, '''')'",
    "SELECT 1 -- pg_read_file('/etc/passwd')",
    "SELECT 1 /* cursor_to_xml ts_stat ts_rewrite pg_ls_dir lo_get */",
    "SELECT 'pg_read_binary_file' AS f",
    "SELECT query_to_xml_count FROM t",
    "SELECT my_lo_get, pg_ls_dir_x FROM t",
]
# #285: side-effecting functions a READ ONLY transaction still runs
FUNCTIONS = [
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_advisory_lock",
    "pg_try_advisory_xact_lock",
    "set_config",
    "pg_reload_conf",
    "pg_notify",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    # #312: functions that run a SQL string or cursor, server-file and large-object reads
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
]
REFUSED = [
    "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
    "WITH d AS (DELETE FROM t RETURNING 1) SELECT * FROM d",
    "SELECT * INTO t2 FROM t",
    "EXPLAIN ANALYZE DELETE FROM t",
    "DROP TABLE t",
    "SELECT 'a\\' , (DELETE)",
    "SELECT $$x$$",
    "SELECT $tag$x$tag$",
    "SELECT a$1",
    "SELECT 'open",
    'SELECT "open',
    "SELECT 1 /* open",
    "/* a /* b */ */ SELECT 1",
    "with a as (select 1) Insert into t select * from a",
    "SELECT 1 -- x\rDELETE FROM t",
    "-- only a comment",
    "",
    # #285
    "SELECT pg_terminate_backend(1)",
    "SELECT pg_cancel_backend(1)",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_advisory_unlock_all()",
    "SELECT pg_try_advisory_xact_lock(1)",
    "SELECT PG_CATALOG.Set_Config('a','b',false)",
    "SELECT pg_reload_conf()",
    "SELECT pg_notify('c', 'x')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/f')",
    "SELECT * FROM dblink('host=h', 'SELECT 1') AS t(a int)",
    "SELECT DBLINK_EXEC('host=h', 'x')",
    'SELECT "pg_terminate_backend"(1)',
    "SELECT \"PG_CATALOG\".\"set_config\"('a', 'b', false)",
    "WITH a AS (SELECT 1) SELECT \"dblink_connect\"('c') FROM a",
    # a Unicode-escape identifier Postgres decodes to pg_terminate_backend
    r'SELECT U&"pg\005fterminate_backend"(1)',
    r"""SELECT u&"set\+00005Fconfig"('a', 'b', false)""",
    r'SELECT "a\b" FROM t',
    # any Unicode escape is refused, whatever its escape character (UESCAPE)
    """SELECT U&"pg!005fterminate_backend" UESCAPE '!'(1)""",
    """SELECT U&"pg!005fadvisory!005flock" UESCAPE '!' (1)""",
    'SELECT U&"abc" FROM t',
    "SELECT u&'abc'",
    "SELECT a uescape FROM t",
    # #312
    "SELECT query_to_xml('select 1', true, true, '')",
    "SELECT query_to_xmlschema('select 1', true, true, '')",
    "SELECT query_to_xml_and_xmlschema('select 1', true, true, '')",
    "SELECT cursor_to_xml('c', 1, true, true, '')",
    "SELECT cursor_to_xmlschema('c', true, true, '')",
    "SELECT * FROM ts_stat('SELECT v FROM t')",
    "SELECT ts_rewrite('a'::tsquery, 'SELECT t, s FROM aliases')",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT * FROM pg_ls_dir('.')",
    "SELECT lo_get(1)",
    "SELECT PG_CATALOG.Query_To_Xml('select 1', true, true, '')",
    "SELECT Pg_Read_File('/etc/passwd')",
    "SELECT \"pg_read_file\"('/etc/passwd')",
    "SELECT \"pg_catalog\".\"cursor_to_xml\"('c', 1, true, true, '')",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_read_statements_pass(sql: str) -> None:
    check_read_only(sql)


@pytest.mark.parametrize("sql", REFUSED)
def test_refusals_are_capability_and_never_echo_the_sql(sql: str) -> None:
    with pytest.raises(AdapterFailure) as raised:
        check_read_only(sql)
    assert raised.value.error_class is ErrorClass.CAPABILITY
    message = raised.value.message.lower()
    for word in ("delete", "insert", "drop", "into", "$$", "open", "select", *FUNCTIONS):
        assert word not in message, (sql, message)
    assert "advisory" not in message and "config" not in message, (sql, message)


def test_a_write_body_is_refused_before_any_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:  # Any: whatever the adapter passes
        raise AssertionError("no connection may be opened for a refused read")

    monkeypatch.setattr(postgres, "run_query", fail)
    monkeypatch.setattr(postgres, "_connect", fail)
    ctx = ExecContext(tenant_id=None, principal_id=None, egress=None)  # type: ignore[arg-type]
    conn = ConnectionConfig(id=None, name="db", adapter_key="postgres", config={})
    with pytest.raises(AdapterFailure) as raised:
        PostgresSession(ctx, conn).invoke({"sql": "WITH a AS (SELECT 1) DELETE FROM t"})
    assert raised.value.error_class is ErrorClass.CAPABILITY


def test_a_side_effecting_function_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:  # Any: whatever the adapter passes
        raise AssertionError("no connection may be opened for a refused read")

    monkeypatch.setattr(postgres, "run_query", fail)
    monkeypatch.setattr(postgres, "_connect", fail)
    ctx = ExecContext(tenant_id=None, principal_id=None, egress=None)  # type: ignore[arg-type]
    conn = ConnectionConfig(id=None, name="db", adapter_key="postgres", config={})
    with pytest.raises(AdapterFailure) as raised:
        PostgresSession(ctx, conn).invoke({"sql": "SELECT pg_terminate_backend(1)"})
    assert raised.value.error_class is ErrorClass.CAPABILITY
    assert "pg_terminate_backend" not in raised.value.message


def test_a_sql_running_function_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:  # Any: whatever the adapter passes
        raise AssertionError("no connection may be opened for a refused read")

    monkeypatch.setattr(postgres, "run_query", fail)
    monkeypatch.setattr(postgres, "_connect", fail)
    ctx = ExecContext(tenant_id=None, principal_id=None, egress=None)  # type: ignore[arg-type]
    conn = ConnectionConfig(id=None, name="db", adapter_key="postgres", config={})
    with pytest.raises(AdapterFailure) as raised:
        PostgresSession(ctx, conn).invoke(
            {"sql": "SELECT query_to_xml('select 1', true, true, '')"}
        )
    assert raised.value.error_class is ErrorClass.CAPABILITY
    assert "query_to_xml" not in raised.value.message
