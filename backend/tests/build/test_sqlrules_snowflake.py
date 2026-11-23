"""SP6-1: the Snowflake dialect of `core/sqlrules.py` (SP6 design 6.1, call S6-D): golden refusing
and accepting cases for the tokenizer, the statement, table and parameter rules and
`table_name_supported`; the same texts under the default dialect keep their Databricks outcome;
and no function raises on odd input."""

from __future__ import annotations

import random
import string

import pytest

from shifde.adapters.base import AdapterFailure
from shifde.adapters.dataplatforms.sqlapi import check_read_only
from shifde.core.sqlrules import (
    Token,
    check,
    parameter_rules,
    statement_rules,
    table_name_supported,
    table_rules,
    tokenize,
)


def _codes(sql: str, dialect: str = "snowflake") -> list[str]:
    return [i.code for i in statement_rules(sql, dialect=dialect)]  # type: ignore[arg-type]


def _tables(sql: str, tables: list[str]) -> list[tuple[str, str]]:
    return [(i.code, i.message) for i in table_rules(sql, tables, dialect="snowflake")]


# (sql, Snowflake codes, Databricks codes): the acceptance goldens and one case per rule
STATEMENTS = [
    ('select "DEMO_DB".PUBLIC.X from DEMO_DB.PUBLIC.X', [], []),
    ('select "Demo" from DEMO_DB.PUBLIC.X', ["sql_quoted_identifier_unsupported"], []),
    ('select "demo" from t', ["sql_quoted_identifier_unsupported"], []),
    ('select "MY TABLE" from t', ["sql_quoted_identifier_unsupported"], []),
    ('select "A""B" from t', ["sql_quoted_identifier_unsupported"], []),
    ('select "COL$1", "_X" from t', [], []),
    ('select "1A" from t', ["sql_quoted_identifier_unsupported"], []),
    ("select `a` from t", ["sql_syntax"], []),
    ("select $$ ; delete $$ as s", [], ["sql_multiple_statements", "sql_write_statement"]),
    ("select a$$b$$ from t", ["sql_syntax"], []),
    ("select 1 // c", ["sql_comment"], []),
    ("select 1 -- c", ["sql_comment"], ["sql_comment"]),
    ("select 1 /* c */", ["sql_comment"], ["sql_comment"]),
    ("select * from t where id = ?", ["sql_positional_parameter"], []),
    ("select '?' as q", [], []),
    ("select $$?$$ as q", [], []),
    ("select comment from DEMO_DB.PUBLIC.T", [], []),
    ("select list, remove, unset, execute, undrop from t", [], []),
    ("commit", ["sql_not_select", "sql_write_statement"], ["sql_not_select"]),
    ("begin", ["sql_not_select", "sql_write_statement"], ["sql_not_select"]),
    ("rollback", ["sql_not_select", "sql_write_statement"], ["sql_not_select"]),
    ("list @stage", ["sql_not_select", "sql_write_statement"], ["sql_not_select"]),
    ("undrop table t", ["sql_not_select", "sql_write_statement"], ["sql_not_select"]),
    ("comment on table t is 'x'", ["sql_not_select", "sql_write_statement"], ["sql_not_select"]),
    (
        "select 1 from t where x = 1 or delete",
        ["sql_write_statement"],
        ["sql_write_statement"],
    ),
    ('select "DELETE" from t', [], []),  # never a keyword (Databricks: a string)
    ('"SELECT" from t', ["sql_not_select"], ["sql_not_select"]),
    ("select 1; select 2", ["sql_multiple_statements"], ["sql_multiple_statements"]),
    ("select 1;", [], []),
    ("select 'a", ["sql_unterminated_quote"], ["sql_unterminated_quote"]),
    ('select "A', ["sql_unterminated_quote"], ["sql_unterminated_quote"]),
    ("select $$a", ["sql_unterminated_quote"], []),
    ("select 'it''s' as s", [], []),
    ("select 'a\\'; delete' as s", [], []),
    ("select x::int from t", [], []),
]


@pytest.mark.parametrize(("sql", "snowflake", "databricks"), STATEMENTS)
def test_snowflake_statement_rules_golden(
    sql: str, snowflake: list[str], databricks: list[str]
) -> None:
    assert _codes(sql) == snowflake
    assert _codes(sql, "databricks") == databricks
    assert [i.code for i in statement_rules(sql)] == databricks  # the default is Databricks


def test_snowflake_write_messages_name_the_word() -> None:
    issues = statement_rules("commit", dialect="snowflake")
    assert [i.message for i in issues if i.code == "sql_write_statement"] == [
        "COMMIT is not allowed in a read statement"
    ]


def test_snowflake_tokens() -> None:
    assert tokenize('select "DEMO_DB", "Demo", a$b, ?, :p$1 // x', dialect="snowflake") == [
        Token("ident", "select"),
        Token("ident", "demo_db", quoted=True),
        Token("punct", ","),
        Token("quoted_unsupported", "Demo"),
        Token("punct", ","),
        Token("ident", "a$b"),
        Token("punct", ","),
        Token("positional", "?"),
        Token("punct", ","),
        Token("param", "p$1"),
        Token("comment", "//"),
    ]
    # a line comment ends at a lone carriage return, as the runtime check reads it
    assert tokenize("select 1 // c\rfrom t", dialect="snowflake")[-2:] == [
        Token("ident", "from"),
        Token("ident", "t"),
    ]
    assert tokenize("select $$x$$", dialect="snowflake") == [
        Token("ident", "select"),
        Token("string", "x"),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "select $$ ; delete $$ as s",
        "select 'a\\'; delete' as s",
        "select comment from DEMO_DB.PUBLIC.T",
        'select "DEMO_DB".PUBLIC.X from DEMO_DB.PUBLIC.X',
    ],
)
def test_snowflake_publish_and_the_runtime_check_agree_on_accepted_text(sql: str) -> None:
    assert _codes(sql) == []
    check_read_only(sql, dialect="snowflake")


@pytest.mark.parametrize("sql", ["select a$$b$$ from t", "select 1; delete from t"])
def test_snowflake_publish_and_the_runtime_check_agree_on_refused_text(sql: str) -> None:
    assert _codes(sql) != []
    with pytest.raises(AdapterFailure):
        check_read_only(sql, dialect="snowflake")


ALLOWED = ["DEMO_DB.PUBLIC.CUSTOMERS", "demo_db.public.orders"]


def test_snowflake_table_rules_compare_uppercased() -> None:
    sql = (
        'select * from demo_db.public.customers c join "DEMO_DB"."PUBLIC"."ORDERS" o on o.ID = c.id'
    )
    assert _tables(sql, ALLOWED) == []
    assert _tables("select * from DEMO_DB.PUBLIC.ORDERS, Demo_Db.Public.Customers", ALLOWED) == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "select * from TABLE(gen())",
            [("sql_table_function", "table function TABLE is not allowed")],
        ),
        (
            "select * from IDENTIFIER('t')",
            [("sql_table_function", "table function IDENTIFIER is not allowed")],
        ),
        (
            "select * from demo_db.public.fn(1)",
            [("sql_table_function", "table function DEMO_DB.PUBLIC.FN is not allowed")],
        ),
        (
            "select * from DEMO_DB.PUBLIC.SECRETS",
            [("sql_table_not_allowed", "DEMO_DB.PUBLIC.SECRETS is not allowed")],
        ),
        (
            'select * from "DEMO_DB"."PUBLIC"."SECRETS"',
            [("sql_table_not_allowed", "DEMO_DB.PUBLIC.SECRETS is not allowed")],
        ),
        (
            "select (select max(id) from demo_db.sales.invoices) from DEMO_DB.PUBLIC.ORDERS",
            [("sql_table_not_allowed", "DEMO_DB.SALES.INVOICES is not allowed")],
        ),
        (
            "select * from orders",
            [("sql_table_not_qualified", "ORDERS must be named as database.schema.table")],
        ),
        (
            "select * from public.orders",
            [("sql_table_not_qualified", "PUBLIC.ORDERS must be named as database.schema.table")],
        ),
        ("with o as (select * from demo_db.public.orders) select * from o", []),
    ],
)
def test_snowflake_table_rules_golden(sql: str, expected: list[tuple[str, str]]) -> None:
    assert _tables(sql, ALLOWED) == expected


def test_snowflake_table_rules_leave_the_databricks_default_unchanged() -> None:
    assert [i.message for i in table_rules("select * from orders", [])] == [
        "orders must be named as catalog.schema.table"
    ]
    assert [i.message for i in table_rules("select * from MAIN.Default.X", [])] == [
        "main.default.x is not allowed"
    ]


def test_snowflake_parameter_rules_use_the_name_form() -> None:
    sql = "select * from DEMO_DB.PUBLIC.ORDERS where id = :id and x = ':nope'"
    assert parameter_rules(sql, ["id"], dialect="snowflake") == []
    assert [i.code for i in parameter_rules(sql, ["id", "other"], dialect="snowflake")] == [
        "sql_parameter_unused"
    ]
    assert [i.code for i in parameter_rules("select :a", [], dialect="snowflake")] == [
        "sql_parameter_undeclared"
    ]
    assert check(sql, ALLOWED, ["id"], dialect="snowflake") == []


@pytest.mark.parametrize(
    ("name", "supported"),
    [
        ("DEMO_DB.PUBLIC.CUSTOMERS", True),
        ("demo_db.public.customers", True),
        ("DB$1.S_2.T$", True),
        ('DEMO_DB.PUBLIC."My Table"', False),
        ("DEMO_DB.PUBLIC.MY TABLE", False),
        ("DEMO_DB.PUBLIC.1T", False),
        ("DEMO_DB..T", False),
        ("", False),
        ("DEMO_DB.PUBLIC.$T", False),
    ],
)
def test_snowflake_table_name_supported(name: str, supported: bool) -> None:
    assert table_name_supported(name, "snowflake") is supported
    assert table_name_supported(name, "databricks") is True


def test_the_snowflake_dialect_never_raises_on_odd_input() -> None:
    rng = random.Random(6)
    alphabet = string.printable + "$$``\"\"''??::\\\\//--/**/;;é"
    samples = ["", "$", "$$", "a$$", '"', '""', "'", "\\", "//", "/*", "?", ":", "::", "`"]
    samples += [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(400)
    ]
    for sql in samples:
        check(sql, ["A.B.C", sql], [sql], dialect="snowflake")
        tokenize(sql, dialect="snowflake")
        table_name_supported(sql, "snowflake")
