"""#267: the Postgres `read` invoke check (`adapters/postgres.check_read_only`): a read first word
and no write keyword (or `into`) anywhere in the code, quotes and comments read the way Postgres
reads them, and ambiguous text refused. Pure: no database."""

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
    for word in ("delete", "insert", "drop", "into", "$$", "open"):
        assert word not in message, (sql, message)


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
