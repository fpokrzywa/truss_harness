"""SP5-1: `build/sqlrules.py`, the pure SELECT-only rules of design 5.4: the tokenizer, every
statement, table and parameter code with a refusing and an accepting example, and no exception on
any input."""

from __future__ import annotations

import pytest

from shifde.build.sqlrules import (
    SqlIssue,
    Token,
    check,
    parameter_rules,
    statement_rules,
    table_rules,
    tokenize,
)

TABLES = ["main.default.customers", "main.default.orders", "main.sales.invoices"]


def _codes(issues: list[SqlIssue]) -> list[str]:
    return [i.code for i in issues]


# ------------------------------------------------------------------ tokenizer
def test_the_tokenizer_kinds() -> None:
    tokens = tokenize(
        "SELECT `My Col`, 'it''s', \"x y\" FROM Main.Default.T WHERE a = :Id AND b::INT > 1;"
    )
    assert tokens == [
        Token("ident", "select"),
        Token("ident", "my col", quoted=True),
        Token("punct", ","),
        Token("string", "it''s"),
        Token("punct", ","),
        Token("string", "x y"),
        Token("ident", "from"),
        Token("ident", "main"),
        Token("punct", "."),
        Token("ident", "default"),
        Token("punct", "."),
        Token("ident", "t"),
        Token("ident", "where"),
        Token("ident", "a"),
        Token("param", "Id"),
        Token("ident", "and"),
        Token("ident", "b"),
        Token("ident", "int"),
        Token("punct", ";"),
    ]


def test_comments_are_recorded_and_unterminated_quotes_end_the_tokens() -> None:
    assert [t.kind for t in tokenize("select 1 -- x\n, 2 /* y */")] == [
        "ident",
        "comment",
        "punct",
        "comment",
    ]
    assert tokenize("select 'abc")[-1] == Token("unterminated", "'")
    assert tokenize("select `abc")[-1] == Token("unterminated", "`")


def test_backslash_escapes_and_raw_literals_read_as_databricks_reads_them() -> None:
    # `\'` stays inside a '...' or "..." string; a raw literal and a backtick have no escapes
    assert tokenize(r"select '\'' as q") == [
        Token("ident", "select"),
        Token("string", r"\'"),
        Token("ident", "as"),
        Token("ident", "q"),
    ]
    assert tokenize(r'select "a\"b" x')[1] == Token("string", r"a\"b")
    assert tokenize(r"select r'a\' as q") == [
        Token("ident", "select"),
        Token("string", "a\\"),
        Token("ident", "as"),
        Token("ident", "q"),
    ]
    assert tokenize(r'select R"a\" x')[1] == Token("string", "a\\")
    assert tokenize(r"select xr'a\'' q")[1:3] == [Token("ident", "xr"), Token("string", r"a\'")]
    assert tokenize(r"select `a\` x")[1] == Token("ident", "a\\", quoted=True)
    assert tokenize(r"select '\\' x")[1] == Token("string", r"\\")


@pytest.mark.parametrize(
    "sql", ["", ";", "'", "`", '"', ":", "::", "--", "/*", "((((", "))", "\x00é", "a." * 50]
)
def test_no_rule_raises_on_odd_input(sql: str) -> None:
    check(sql, TABLES, ["x"])
    tokenize(sql)


# ------------------------------------------------------------------ statement rules
def test_the_acceptance_examples() -> None:
    codes = _codes(statement_rules("select 1; drop table x"))
    assert "sql_multiple_statements" in codes and "sql_write_statement" in codes
    assert statement_rules("select 'drop' as x") == []
    assert "sql_table_not_qualified" in _codes(
        table_rules(
            "select * from main.default.customers c join orders o on c.id = o.customer_id",
            ["main.default.customers"],
        )
    )
    assert parameter_rules("select * from t where id = :id and x::int > 0", ["id"]) == []


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("select 1 -- note", "sql_comment"),
        ("select /* note */ 1", "sql_comment"),
        ("select 1; select 2", "sql_multiple_statements"),
        ("select 1;;", "sql_multiple_statements"),
        ("show tables", "sql_not_select"),
        ("", "sql_not_select"),
        ("`select` 1", "sql_not_select"),
        ("select * from t where 'a' = 'b", "sql_unterminated_quote"),
        ("select `a from t", "sql_unterminated_quote"),
        (
            r"with a as (select '\'' as q) insert into main.default.orders select * from a "
            r"where q <> '\''",
            "sql_write_statement",
        ),
        (
            r"select '\'' ; drop table main.default.orders; select '\''",
            "sql_multiple_statements",
        ),
        (
            r"select '\'' ; drop table main.default.orders; select '\''",
            "sql_write_statement",
        ),
        (r"select r'\'; drop table t", "sql_write_statement"),
        (r"select 'a\'", "sql_unterminated_quote"),
    ],
)
def test_statement_rules_refuse(sql: str, code: str) -> None:
    assert code in _codes(statement_rules(sql))


@pytest.mark.parametrize(
    "word",
    [
        "insert",
        "update",
        "delete",
        "merge",
        "create",
        "alter",
        "drop",
        "truncate",
        "grant",
        "revoke",
        "copy",
        "put",
        "get",
        "call",
        "optimize",
        "vacuum",
        "set",
        "use",
        "refresh",
        "msck",
        "analyze",
        "cache",
        "uncache",
        "restore",
    ],
)
def test_every_write_keyword_is_refused_as_a_bare_identifier(word: str) -> None:
    issues = statement_rules(f"select 1 from main.default.orders where {word.upper()} = 1")
    assert _codes(issues) == ["sql_write_statement"]
    # inside a string or backticks it is data, not a keyword
    assert statement_rules(f"select '{word}' as x, `{word}` from main.default.orders") == []
    assert statement_rules(f'select "{word} it" as y') == []


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "SELECT 1;",  # a trailing semicolon is allowed
        "select 1 ;  ",
        "with t as (select 1 as n) select n from t",
        "select 'a; b -- c /* d' as x",
        "select x::int from main.default.orders",
        "select `--weird` from main.default.orders",
        r"select 'it\'s; drop' as x",
        r"select r'C:\dir' as p, 'x' as q",
    ],
)
def test_statement_rules_accept(sql: str) -> None:
    assert statement_rules(sql) == []


# ------------------------------------------------------------------ table rules
@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("select * from main.default.secrets", "sql_table_not_allowed"),
        ("select * from main.default.orders, main.other.t", "sql_table_not_allowed"),
        ("select * from main.default.orders o join hive.x.y z on o.id = z.id", "sql_table_not_allowed"),
        ("select * from default.orders", "sql_table_not_qualified"),
        ("select * from orders", "sql_table_not_qualified"),
        ("select * from main.default.orders, orders", "sql_table_not_qualified"),
        ("select * from read_files('s3://bucket/x')", "sql_table_function"),
        ("select * from main.default.orders o join range(10) r on o.id = r.id", "sql_table_function"),
        (
            "select * from main.default.orders where id in (select id from main.private.t)",
            "sql_table_not_allowed",
        ),
        ("select (select max(id) from x.y.z) as m from main.default.orders", "sql_table_not_allowed"),
        ("select main.other.t.id from main.default.orders", "sql_table_not_allowed"),
        ("with t as (select 1) select * from u", "sql_table_not_qualified"),
    ],
)  # fmt: skip
def test_table_rules_refuse(sql: str, code: str) -> None:
    assert code in _codes(table_rules(sql, TABLES))


@pytest.mark.parametrize(
    "sql",
    [
        "select * from main.default.orders",
        "select * from MAIN.Default.ORDERS as o",
        "select * from `main`.`default`.`orders` o",
        "select * from main.default.orders o, main.default.customers as c where o.customer_id = c.id",
        "select * from main.default.orders o join main.sales.invoices i on i.order_id = o.id "
        "left join main.default.customers c on c.id = o.customer_id",
        "with recent as (select * from main.default.orders), big (n) as (select 1) "
        "select * from recent r join big b on true",
        "select * from (select id from main.default.orders) s",
        "select * from main.default.orders where id in (select order_id from main.sales.invoices)",
        "select o.id, c.name from main.default.orders o join main.default.customers c "
        "on o.customer_id = c.id",
        "select 'from orders' as x from main.default.orders",
        "select cast(id as string)::string from main.default.orders",
    ],
)  # fmt: skip
def test_table_rules_accept(sql: str) -> None:
    assert table_rules(sql, TABLES) == []


def test_a_subquery_after_from_is_not_a_table_reference() -> None:
    assert table_rules("select * from (select 1) as s", []) == []


# ------------------------------------------------------------------ parameter rules
@pytest.mark.parametrize(
    ("sql", "declared", "codes"),
    [
        ("select * from t where id = :id", [], ["sql_parameter_undeclared"]),
        ("select * from t where id = :Id", ["id"], ["sql_parameter_undeclared", "sql_parameter_unused"]),
        ("select * from t", ["id"], ["sql_parameter_unused"]),
        ("select ':id' as x", ["id"], ["sql_parameter_unused"]),
        ("select x::id from t", ["id"], ["sql_parameter_unused"]),
    ],
)  # fmt: skip
def test_parameter_rules_refuse(sql: str, declared: list[str], codes: list[str]) -> None:
    assert _codes(parameter_rules(sql, declared)) == codes


@pytest.mark.parametrize(
    ("sql", "declared"),
    [
        ("select * from t where id = :id and d >= :since", ["id", "since"]),
        ("select * from t where id = :id or other = :id", ["id"]),
        ("select x::int, :n::bigint from t", ["n"]),
        ("select 1", []),
    ],
)
def test_parameter_rules_accept(sql: str, declared: list[str]) -> None:
    assert parameter_rules(sql, declared) == []


def test_check_concatenates_the_three_rule_sets_in_order() -> None:
    sql = "delete from orders where id = :x"
    assert _codes(check(sql, TABLES, ["y"])) == [
        "sql_not_select",
        "sql_write_statement",
        "sql_table_not_qualified",
        "sql_parameter_undeclared",
        "sql_parameter_unused",
    ]
    assert check("select * from main.default.orders where id = :id", TABLES, ["id"]) == []
