"""SP6-1: the Snowflake dialect of `core/sqlrules.py` (SP6 design 6.1, call S6-D): golden refusing
and accepting cases for the tokenizer, the statement, table and parameter rules and
`table_name_supported`; the same texts under the default dialect keep their Databricks outcome;
no function raises on odd input; and the rules shared with the runtime check (#358: the flow
operator, `SYSTEM$`, `procedure`, `IDENTIFIER(...)`) refuse what it refuses; and the table rules
fail closed on FROM clauses (every list, parentheses, stages, `IDENTIFIER`/`TABLE` literals) in one
pass."""

from __future__ import annotations

import random
import string

import pytest

from shifde.adapters.base import AdapterFailure
from shifde.adapters.dataplatforms.sqlapi import _POSITIONAL_BIND, check_read_only
from shifde.core import sqlrules
from shifde.core.sqlrules import (
    FLOW_OPERATOR_MESSAGE,
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
    ("select 1; \n\t", [], []),
    # text after the `;` that makes no token (Databricks: unchanged)
    ("select 1;1", ["sql_multiple_statements"], []),
    ("select 1; + ", ["sql_multiple_statements"], []),
    ("select 1;\u200b", ["sql_multiple_statements"], []),
    ("select 'a", ["sql_unterminated_quote"], ["sql_unterminated_quote"]),
    ('select "A', ["sql_unterminated_quote"], ["sql_unterminated_quote"]),
    ("select $$a", ["sql_unterminated_quote"], []),
    ("select 'it''s' as s", [], []),
    ("select 'a\\'; delete' as s", [], []),
    ("select x::int from t", [], []),
    # a `:` directly followed by a digit, as the runtime check refuses it (#325)
    ("select :1", ["sql_positional_bind"], []),
    ("select * from t where id = :12", ["sql_positional_bind"], []),
    ("select :1abc", ["sql_positional_bind"], []),
    ("select col:1 from t", ["sql_positional_bind"], []),
    ("select x::1 from t", ["sql_positional_bind"], []),
    ("select ':1'", [], []),
    ("select $$:1$$", [], []),
    ('select ":1" from t', ["sql_quoted_identifier_unsupported"], []),
    ("select 1 -- :1", ["sql_comment"], ["sql_comment"]),
    ("select :id from t", [], []),
    (
        "select * from t where a = ? and b = :1",
        ["sql_positional_parameter", "sql_positional_bind"],
        [],
    ),
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


@pytest.mark.parametrize(
    "sql",
    [
        "select a$$b$$ from t",
        "select 1; delete from t",
        "select 1;1",
        "select 1; +",
        "select 1;\u200b",
    ],
)
def test_snowflake_publish_and_the_runtime_check_agree_on_refused_text(sql: str) -> None:
    assert _codes(sql) != []
    with pytest.raises(AdapterFailure):
        check_read_only(sql, dialect="snowflake")


def test_snowflake_positional_bind_has_the_runtime_message() -> None:
    issues = statement_rules("SELECT :1", dialect="snowflake")
    assert [(i.code, i.message) for i in issues] == [("sql_positional_bind", _POSITIONAL_BIND)]
    assert statement_rules("select :1") == []  # the Databricks default is unchanged


@pytest.mark.parametrize(
    "sql",
    [
        "select :1",
        "select * from t where id = :12",
        "select :1abc",
        "select col:1 from t",
        "select x::1 from t",
        "select ':1'",
        "select $$:1$$",
        'select ":1" from t',
        "select 1 -- :1",
        "select x::int from t",
        "select :id from t",
        "select * from t where a = ? and b = :1",
    ],
)
def test_snowflake_publish_and_the_runtime_check_agree_on_positional_binds(sql: str) -> None:
    published = "sql_positional_bind" in _codes(sql)
    try:
        check_read_only(sql, dialect="snowflake")
        runtime = False
    except AdapterFailure as exc:
        runtime = exc.message == _POSITIONAL_BIND
    assert published is runtime


NEW_CODES = {"sql_flow_operator", "sql_system_function", "sql_dynamic_name"}
PROCEDURE_SQL = "with p as procedure () returns varchar language sql as $$ select 1 $$ select 1"
# (sql, Snowflake codes) of the #358 rules
READ_RULES = [
    # the flow operator: contiguous characters in code
    ("select 1 ->> execute immediate 'drop table x'", ["sql_flow_operator"]),
    ("select 1 ->> remove @s", ["sql_flow_operator"]),
    ("select 1 ->> undrop table x", ["sql_flow_operator"]),
    ("select 1->>select 2 ->> select 3", ["sql_flow_operator"]),
    ("select a ->>> b", ["sql_flow_operator"]),
    ("select $$a$$->>select 1", ["sql_flow_operator"]),
    ("select '->>'", []),
    ("select $$->>$$", []),
    ('select "->>" from t', ["sql_quoted_identifier_unsupported"]),
    ("select 1 -- ->> x", ["sql_comment"]),
    ("select 1 /* ->> */", ["sql_comment"]),
    ("select filter(a, x -> x > 1) from t", []),
    ("select a >> b from t", []),
    ("select a - >> b from t", []),
    ("select a -> > b from t", []),
    # SYSTEM$ names, unquoted or a whole quoted identifier
    ("select system$wait(5)", ["sql_system_function"]),
    ("select SYSTEM$WAIT(5), System$Wait(6)", ["sql_system_function"]),
    ("select snowflake.x.system$wait(5)", ["sql_system_function"]),
    ('select "SYSTEM$WAIT"(5)', ["sql_system_function"]),
    ('select "system$wait"(5)', ["sql_quoted_identifier_unsupported", "sql_system_function"]),
    ('select "SYSTEM$ X"(5)', ["sql_quoted_identifier_unsupported", "sql_system_function"]),
    ("select $$a$$system$wait(5)", ["sql_system_function"]),
    ("select a::system$x", ["sql_system_function"]),
    ("select my_system$x from t", []),
    ("select $system$x", []),
    ('select "MY_SYSTEM$X" from t', []),
    ('select "a""SYSTEM$x" from t', ["sql_quoted_identifier_unsupported"]),
    ("select 'SYSTEM$X'", []),
    ("select $$SYSTEM$X$$", []),
    ("select system from t", []),
    # the word procedure, listed with the write words
    (PROCEDURE_SQL, ["sql_write_statement"]),
    ("select Procedure, PROCEDURE from t", ["sql_write_statement"]),
    ('select "PROCEDURE" from t', []),
    ("select procedure_id, my$procedure from t", []),
    ("select 'procedure', $$procedure$$", []),
    # IDENTIFIER( and TABLE( with anything but one plain literal or name
    ("select * from identifier(:t)", ["sql_dynamic_name"]),
    ("select * from identifier(:region||'x')", ["sql_dynamic_name"]),
    ("select * from identifier(?)", ["sql_positional_parameter", "sql_dynamic_name"]),
    ("select * from identifier(:1)", ["sql_positional_bind", "sql_dynamic_name"]),
    ("select * from identifier($t)", ["sql_dynamic_name"]),
    ("select * from identifier($$t$$)", ["sql_dynamic_name"]),
    ('select * from identifier("T")', ["sql_dynamic_name"]),
    ("select * from identifier('a'||'b')", ["sql_dynamic_name"]),
    ("select * from identifier('a', 'b')", ["sql_dynamic_name"]),
    ("select * from identifier('a''b')", ["sql_dynamic_name"]),
    ("select * from identifier('a\\b')", ["sql_dynamic_name"]),
    ("select * from identifier('SYSTEM$X')", ["sql_dynamic_name"]),
    ("select * from identifier('snowflake.x.system$x')", ["sql_dynamic_name"]),
    ("select * from identifier()", ["sql_dynamic_name"]),
    ("select * from identifier( )", ["sql_dynamic_name"]),
    ("select * from identifier('t'", ["sql_dynamic_name"]),
    ("select * from identifier(", ["sql_dynamic_name"]),
    ("select * from identifier(a.b)", ["sql_dynamic_name"]),
    ("select * from identifier(1)", ["sql_dynamic_name"]),
    ("select * from identifier(concat('a', 'b'))", ["sql_dynamic_name"]),
    ("select * from identifier(('t'))", ["sql_dynamic_name"]),
    ("select * from identifier((select 't'))", ["sql_dynamic_name"]),
    ("select * from IDENTIFIER \n (:t), identifier(:u)", ["sql_dynamic_name"]),
    ("select $$a$$identifier(:t)", ["sql_dynamic_name"]),
    ("select * from table(:t)", ["sql_dynamic_name"]),
    ("select * from table($t)", ["sql_dynamic_name"]),
    ("select * from table('a'||:t)", ["sql_dynamic_name"]),
    ("select * from table(concat('a','b'))", ["sql_dynamic_name"]),
    ("select * from table(identifier('t'))", ["sql_dynamic_name"]),
    # a character before the parenthesis that is neither visible ASCII nor whitespace
    ("select * from identifier\u200b(:t||'x')", ["sql_dynamic_name"]),
    ("select * from identifier \u200b ('t')", ["sql_dynamic_name"]),
    ("select * from identifier\x01('t')", ["sql_dynamic_name"]),
    ("select * from table\u2060(my_table)", ["sql_dynamic_name"]),
    ("select * from identifier('db.s.t')", []),
    ("select * from identifier( 'MY_TABLE' )", []),
    ("select * from IDENTIFIER\t(\n'MY_TABLE'\n)", []),
    ("select * from identifier('a)b')", []),
    ("select * from identifier('a(b')", []),
    ("select * from identifier(my_var)", []),
    ("select * from identifier(my_var$1)", []),
    ("select * from table('db.s.t')", []),
    ("select * from Table(my_table)", []),
    ("select identifier from t", []),
    ("select identifier, (:a) from t", []),
    ("select identifier + (:a) from t", []),
    ("select my_identifier(:a), my_table(:b), $identifier(:c)", []),
    ('select "IDENTIFIER"(:a), "TABLE"(:b)', []),
    ("select 'IDENTIFIER(:x)'", []),
    ("select $$TABLE(:x)$$", []),
    # several rules, and a name directly after a `:` is a parameter
    (
        "select system$x, identifier(:a) ->> call procedure",
        [
            "sql_flow_operator",
            "sql_system_function",
            "sql_dynamic_name",
            "sql_write_statement",
            "sql_write_statement",
        ],
    ),
    ("select :identifier, :table, :procedure, :system$x", []),
]


@pytest.mark.parametrize(("sql", "codes"), READ_RULES)
def test_snowflake_read_rules_golden(sql: str, codes: list[str]) -> None:
    assert _codes(sql) == codes
    issues = statement_rules(sql, dialect="snowflake")
    assert not any(sql in i.message or "system$" in i.message.lower() for i in issues)
    # the Databricks default has none of these rules
    assert not NEW_CODES & set(_codes(sql, "databricks"))
    assert not any("PROCEDURE" in i.message for i in statement_rules(sql))


def test_snowflake_procedure_is_listed_with_the_write_words() -> None:
    issues = statement_rules("select procedure, delete, procedure", dialect="snowflake")
    assert [i.message for i in issues] == [
        "PROCEDURE is not allowed in a read statement",
        "DELETE is not allowed in a read statement",
    ]


def test_a_snowflake_flow_token_takes_the_whole_operator() -> None:
    tokens = tokenize("a->>b ->c", dialect="snowflake")
    assert tokens == [
        Token("ident", "a"),
        Token("flow", "->>"),
        Token("ident", "b"),
        Token("ident", "c"),
    ]
    assert tokenize("a->>b") == [Token("ident", "a"), Token("ident", "b")]


# #285, #312 and the flow operator: each text with its run-time outcome
RUNTIME_REFUSED = [
    "SELECT SYSTEM$CANCEL_ALL_QUERIES()",
    "select system$cancel_all_queries()",
    "SELECT SNOWFLAKE.X.SYSTEM$CANCEL_ALL_QUERIES()",
    "WITH a AS (SELECT System$Cancel_All_Queries()) SELECT * FROM a",
    'SELECT "SYSTEM$CANCEL_ALL_QUERIES"()',
    'SELECT "system$abort_session"(1)',
    "SELECT $$a$$SYSTEM$X()",
    "SELECT 'a'SYSTEM$X()",
    "SELECT a::SYSTEM$X",
    PROCEDURE_SQL,
    "SELECT procedure FROM t",
    "SELECT * FROM TABLE(IDENTIFIER('SYSTEM$CANCEL_ALL_QUERIES'))",
    "SELECT IDENTIFIER('system$abort_session')(1)",
    "select identifier( '  System$Cancel_All_Queries ' )()",
    "SELECT IDENTIFIER /* c */ ('SYSTEM$X')()",
    "SELECT IDENTIFIER('SNOWFLAKE.X.SYSTEM$X')()",
    "SELECT IDENTIFIER('\"SYSTEM$X\"')()",
    "SELECT IDENTIFIER('\\SYSTEM$X')()",
    "SELECT IDENTIFIER($$SYSTEM$X$$)()",
    "SELECT IDENTIFIER('SYS' || 'TEM$X')()",
    "SELECT $$a$$IDENTIFIER('SYS' || 'TEM$X')()",
    "SELECT a.IDENTIFIER('SYS' || 'TEM$X')()",
    "SELECT IDENTIFIER\u00a0(:a || 'TEM$X')()",
    "SELECT IDENTIFIER(:a || 'TEM$X')()",
    "SELECT IDENTIFIER\u200b(:a || 'TEM$X')()",
    "SELECT IDENTIFIER \u200b\x01 ('t')",
    "SELECT IDENTIFIER('a''b')",
    "SELECT * FROM IDENTIFIER($t)",
    "SELECT * FROM IDENTIFIER()",
    "SELECT * FROM IDENTIFIER('t'",
    'SELECT * FROM IDENTIFIER("t")',
    "SELECT * FROM IDENTIFIER('a', 'b')",
    "SELECT * FROM IDENTIFIER(CONCAT('a', 'b'))",
    "select 1 ->> execute immediate 'drop table x'",
    "select 1 ->> remove @s",
    "select 1 ->> undrop table x",
    "SHOW TABLES ->> SELECT * FROM $1",
]
RUNTIME_ACCEPTED = [
    "SELECT 'SYSTEM$CANCEL_ALL_QUERIES()'",
    "SELECT $$SYSTEM$CANCEL_ALL_QUERIES()$$",
    "SELECT 1 -- SYSTEM$CANCEL_ALL_QUERIES()",
    "SELECT my_system$x FROM t",
    'SELECT "my_system$x" FROM t',
    'SELECT "a""SYSTEM$x" FROM t',
    "SELECT $system$x, $identifier(:a)",
    "SELECT procedure_id, 'procedure' FROM t",
    'SELECT "PROCEDURE" FROM t',
    "SELECT * FROM IDENTIFIER('db.schema.table')",
    "SELECT * FROM identifier( 'MY_TABLE' )",
    "SELECT * FROM IDENTIFIER(my_var)",
    "SELECT * FROM IDENTIFIER /* c */ ( 'a)b' )",
    "SELECT * FROM IDENTIFIER('a)b')",
    "SELECT identifier FROM t",
    "SELECT identifier + (:a) FROM t",
    "SELECT my_identifier('SYSTEM$X') FROM t",
    "SELECT 'IDENTIFIER($$x$$)'",
    "SELECT \"IDENTIFIER('SYSTEM$X')\" FROM t",
    "select '->>'",
    "select $$->>$$",
    'SELECT "->>" FROM t',
    "SELECT 1 -- ->> x",
    "SELECT filter(a, x -> x > 1), a >> b, a - >> b FROM t",
]
# the two places publish is stricter: a bound IDENTIFIER, and any TABLE( argument but a plain one
PUBLISH_STRICTER = [
    "SELECT * FROM IDENTIFIER(?)",
    "SELECT * FROM IDENTIFIER(:tbl)",
    "SELECT * FROM TABLE(IDENTIFIER(:fn))",
    "SELECT * FROM TABLE(:t)",
    "SELECT * FROM TABLE($t)",
    "SELECT * FROM TABLE(FLATTEN(a))",
    "SELECT * FROM TABLE(IDENTIFIER('t'))",
]


def _runtime_refuses(sql: str) -> bool:
    try:
        check_read_only(sql, dialect="snowflake")
    except AdapterFailure:
        return True
    return False


@pytest.mark.parametrize("sql", RUNTIME_REFUSED + RUNTIME_ACCEPTED + PUBLISH_STRICTER)
def test_snowflake_publish_refuses_what_the_runtime_check_refuses(sql: str) -> None:
    published = _codes(sql)
    refused = _runtime_refuses(sql)
    assert refused is (sql in RUNTIME_REFUSED)
    # refused at run time implies refused at publish; accepted at publish, accepted at run time
    assert not (refused and published == [])
    # stricter only for a bound IDENTIFIER and for TABLE(
    assert ("sql_dynamic_name" in published and not refused) is (sql in PUBLISH_STRICTER)
    if not refused:
        assert not {"sql_flow_operator", "sql_system_function", "sql_write_statement"} & set(
            published
        )


@pytest.mark.parametrize("word", ["identifier", "system$x", "procedure"])
def test_a_snowflake_word_directly_after_a_colon_is_a_parameter(word: str) -> None:
    sql = f"select :{word}"
    assert _codes(sql) == []
    assert [i.code for i in check(sql, [], [], dialect="snowflake")] == ["sql_parameter_undeclared"]


def test_snowflake_flow_operator_has_the_runtime_message() -> None:
    sql = "select 1 ->> execute immediate 'drop table x'"
    assert [(i.code, i.message) for i in statement_rules(sql, dialect="snowflake")] == [
        ("sql_flow_operator", FLOW_OPERATOR_MESSAGE)
    ]
    with pytest.raises(AdapterFailure) as raised:
        check_read_only(sql, dialect="snowflake")
    assert raised.value.message == FLOW_OPERATOR_MESSAGE


def test_the_databricks_default_accepts_the_flow_operator_characters() -> None:
    check_read_only("SELECT a ->> b FROM t")
    assert statement_rules("select a ->> b from t") == []


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


CUSTOMERS, ORDERS = "DEMO_DB.PUBLIC.CUSTOMERS", "DEMO_DB.PUBLIC.ORDERS"
JOINED = f"select * from {CUSTOMERS} c join {ORDERS} o on o.customer_id = c.id"
SECRETS = "'DEMO_DB.PUBLIC.SECRETS'"
NAMED, FUNCTION = "sql_table_not_qualified", "sql_table_function"
OTHER, SYNTAX = "sql_table_not_allowed", "sql_syntax"
# every form passes the statement rules: only the table rules stand between it and the app
TABLE_RULES_REFUSE = [
    # IDENTIFIER( and TABLE( anywhere, a plain literal or name included
    (f"select * from (identifier({SECRETS})) s", FUNCTION),
    (f"{JOINED}, identifier({SECRETS}) s", FUNCTION),
    (f"{JOINED}, table({SECRETS}) s", FUNCTION),
    (f"select * from {CUSTOMERS} sample (10), identifier({SECRETS})", FUNCTION),
    (f"select * from {CUSTOMERS} at(offset => -60), identifier({SECRETS})", FUNCTION),
    (f"select * from (table({SECRETS}) s)", FUNCTION),
    (f"select * from {CUSTOMERS} c (a), table({SECRETS})", FUNCTION),
    (f'with "LATERAL" as (select 1) {JOINED}, lateral table({SECRETS}) s', FUNCTION),
    (f"select identifier('DEMO_DB.PUBLIC.UDF')(1) from {CUSTOMERS}", FUNCTION),
    (f"select (identifier ({SECRETS})) from {CUSTOMERS}", FUNCTION),
    (f"select identifier(my_var) from {CUSTOMERS}", FUNCTION),
    # every entry of a list of tables
    (f"select s.* from {CUSTOMERS} c, (APP_DATA.USAGE) s", NAMED),
    (f"{JOINED}, APP_DATA.USAGE u", NAMED),
    (f"{JOINED}, (select 1) s, secrets u where o.id = 1", NAMED),
    (f"{JOINED} join ({CUSTOMERS} d, secrets) on true", NAMED),
    ("select * from (secrets)", NAMED),
    ("select * from ((secrets s))", NAMED),
    ("select * from ((select 1 a) x join secrets s on true)", NAMED),
    (f"select * from ({CUSTOMERS}, secrets)", NAMED),
    (f"select * from {CUSTOMERS} c (a, b), secrets s", NAMED),
    (f"select * from {CUSTOMERS} sample (10), secrets s", NAMED),
    (f"select * from {CUSTOMERS} at(offset => -60), secrets s", NAMED),
    (f"select * from {CUSTOMERS} x, {ORDERS} pivot(sum(a) for q in ('b', 'c')), secrets", NAMED),
    (f"select id from {CUSTOMERS} where id in (select id from (secrets))", NAMED),
    (f'with "LATERAL" as (select 1) select * from {CUSTOMERS}, lateral secrets s', NAMED),
    (f"select * from {CUSTOMERS} c lateral flatten(input => c.x)", NAMED),
    # no alias and no member name ends the list
    (f"select * from {CUSTOMERS} as limit, secrets", NAMED),
    (f"select * from {CUSTOMERS} offset, secrets", NAMED),
    (f"select * from {CUSTOMERS} as start, secrets", NAMED),
    (f"select * from {CUSTOMERS} c join {ORDERS} o on o.where = c.id, secrets", NAMED),
    # a CTE name counts only after its own definition
    ("with a as (select * from b), b as (select 1 x) select * from a", NAMED),
    ("with b as (select * from b) select * from b", NAMED),
    ("select * from (with b as (select 1 x) select * from b)", NAMED),
    # a name with an empty part
    ("select * from OTHER..SECRETS", OTHER),
    ("select * from (OTHER..SECRETS)", OTHER),
    ("with other as (select 1 a) select * from other..secrets", OTHER),
    (f"select other..fn(1) from {CUSTOMERS}", NAMED),
    # a name glued to a number
    ("select *, 1from APP_DATA.USAGE u", SYNTAX),
    ("select 1from secrets", SYNTAX),
    ("select 1e5from secrets", SYNTAX),
    ("select $1from secrets", SYNTAX),
    # the same through a decimal point and an exponent
    ("select u.*, 1.e5from APP_DATA.USAGE u", SYNTAX),
    ("select 1.e5from secrets", SYNTAX),
    ("select 1.E5FROM secrets", SYNTAX),
    ("select 1.e5from(SECRETS)", SYNTAX),
    ("select $1, 1.e0from '@DEMO_DB.PUBLIC.STG/x'", SYNTAX),
    ("select $1, 1.e0from @~", SYNTAX),
    ("select $1, 1.e0from @~/x", SYNTAX),
    ("select 1.e0from lateral flatten(1)", SYNTAX),
    (f"{JOINED} and 1 = 1.e1join secrets", SYNTAX),
    (f"select * from {CUSTOMERS} where 1=1.e1union select 1.e1from secrets", SYNTAX),
    ("select -1.e5from secrets", SYNTAX),
    ("select 1.efrom secrets", SYNTAX),
    ("select 1.e5_from secrets", SYNTAX),
    ("select 1.e5$from secrets", SYNTAX),
    ("select 1.e5e5from secrets", SYNTAX),
    ("select 1.e5.e5from secrets", SYNTAX),
    ("select 1.5.e5from secrets", SYNTAX),
    ("select 1e5.e5from secrets", SYNTAX),
    ("select 1e.e5from secrets", SYNTAX),
    ("select 1..e5from secrets", SYNTAX),
    ("select .5.e5from secrets", SYNTAX),
    ("select 0x1.e5from secrets", SYNTAX),
    ("select 1_0.e5from secrets", SYNTAX),
    ("select $1.e5from secrets", SYNTAX),
    ("select c.$1.e5from secrets", SYNTAX),
    ("select 'a'1.e5from secrets", SYNTAX),
    ("select (1)2.e5from secrets", SYNTAX),
    ("select 1 -2.e5from secrets", SYNTAX),
    ("select 1.from secrets", SYNTAX),
    ("select 1.5from secrets", SYNTAX),
    ("select .5from secrets", SYNTAX),
    ("select 1e-5from secrets", SYNTAX),
    ("select 1.e-5from secrets", SYNTAX),
    ("select 1.5e+5from secrets", SYNTAX),
    # a clause word directly before a comma, or after `::`, `$` or an operator, ends no list
    (f"select * from {CUSTOMERS} qualify, secrets", NAMED),
    (f"select * from {CUSTOMERS} where, secrets", NAMED),
    (f"select * from {CUSTOMERS} having, secrets", NAMED),
    (f"select * from {CUSTOMERS} select, secrets", NAMED),
    (f"select * from {CUSTOMERS} group by, secrets", NAMED),
    (f"select * from {CUSTOMERS} order by, secrets", NAMED),
    (f"select * from {CUSTOMERS} connect by, secrets", NAMED),
    (f"select * from {CUSTOMERS} start with, secrets", NAMED),
    ("select * from (select 1) qualify, secrets", NAMED),
    (f"select * from {CUSTOMERS} qualify 1, secrets", NAMED),
    (f"select * from {CUSTOMERS} where 1 = 1, secrets", NAMED),
    (f"select * from {CUSTOMERS} select 1, secrets", NAMED),
    (f"select * from {CUSTOMERS} union select, secrets", NAMED),
    ("select * from (select 1) select 1, secrets", NAMED),
    ("select * from (select 1) $where, secrets", NAMED),
    (f"{JOINED} and $where = 1, SECRETS s", NAMED),
    (f"{JOINED} and $ where = 1, SECRETS s", NAMED),
    (f"{JOINED} and c.x::where, SECRETS s", NAMED),
    (f"{JOINED} and c.x:: where = 1, SECRETS s", NAMED),
    (f"{JOINED} and c.x::qualify = 1, SECRETS s", NAMED),
    (f"{JOINED} and $select = 1, SECRETS s", NAMED),
    (f"{JOINED} and $group by = 1, SECRETS s", NAMED),
    (f"{JOINED} and c.x = where + 1, SECRETS s", NAMED),
    # refused though harmless (B-4067)
    (f"select sum(a) over v from {CUSTOMERS} window w as (order by a), v as (order by b)", NAMED),
    (f"select substring(a from 2) from {CUSTOMERS}", OTHER),
    (f"select identifier('A') from {CUSTOMERS}", FUNCTION),
    # stages and anything else that is no name
    ("select $1 from '@DEMO_DB.PUBLIC.STG/secrets.csv'", OTHER),
    ("select $1 from $$@DEMO_DB.PUBLIC.STG/secrets.csv$$", OTHER),
    (f"select $1 from @{CUSTOMERS}", OTHER),
    ("select $1 from @~", OTHER),
    (f"{JOINED}, '@s'", OTHER),
    (f"{JOINED} join @s on true", OTHER),
    (f"select * from {CUSTOMERS}@s", OTHER),
    ("select * from $t", OTHER),
    ("select * from :t", OTHER),
    ("select * from (", OTHER),
    (f"select * from {CUSTOMERS},", OTHER),
    (f"select * from {CUSTOMERS}.", OTHER),
    (f"select * from {CUSTOMERS} c join", OTHER),
    # characters the platform may read as part of a name or skip
    (f"select * from {CUSTOMERS}\u00e9", SYNTAX),
    ("select * from\u200bsecrets", OTHER),
    (f"select * from {CUSTOMERS} c jo\u0131n secrets", SYNTAX),
    (f"select * from {CUSTOMERS} c, jo\x01in secrets", SYNTAX),
]


@pytest.mark.parametrize(("sql", "code"), TABLE_RULES_REFUSE)
def test_the_snowflake_table_rules_fail_closed(sql: str, code: str) -> None:
    assert statement_rules(sql, dialect="snowflake") == []
    assert code in [c for c, _ in _tables(sql, ALLOWED)]


@pytest.mark.parametrize(
    "sql",
    [
        f"select c.id, c.name from {CUSTOMERS} c, {ORDERS} as o where c.region in (1, 2) "
        "group by c.id, c.name having count(*) > 1 order by c.id, c.name limit 10 offset 5",
        f"select * from ({CUSTOMERS} c join {ORDERS} o on o.customer_id = c.id)",
        f"select * from (select id from {ORDERS}) s, {CUSTOMERS} c",
        "select * from ((select 1 a) union all (select 2 a)) u",
        f"with recent as (select * from {ORDERS}), big (n, m) as (select n, 1 from recent) "
        "select * from recent r join big b on true",
        f"with a as (select 1 x), b as (select * from a) select * from (select * from b), {ORDERS}",
        f"select a, b from {CUSTOMERS} union select a, b from {ORDERS} order by 1, 2",
        f"select a, b from {CUSTOMERS} except select a, b from {ORDERS}",
        f"select * from {CUSTOMERS} c (a, b), {ORDERS} o (x, y)",
        f"select * from {CUSTOMERS} tablesample bernoulli (10), {ORDERS}",
        f"select * from {CUSTOMERS} at(offset => -60) c",
        f"select * from {ORDERS} pivot(sum(amount) for q in ('a', 'b')) p",
        f"select 1e5, 1.5e-3, 1e+5, .5, $1, t.$2 from {CUSTOMERS} t",
        f"select 1.e5, 1.E5, 1.5e3, 2., 1.e-5, 1.e+5, .5e3, 1.e5 + t.a from {CUSTOMERS} t",
        f"select a[0].e5x, t1.e5x from {CUSTOMERS} t join {ORDERS} t1 on t.a = t1.a",
        f"select 2. from {CUSTOMERS} c join {ORDERS} o on c.a = 1 where 1.e5 = c.b order by 1, 2.",
        f"select * from {CUSTOMERS} c join {ORDERS} o on c.a = o.a[0] where c.a = 1 order by 1, 2",
        f"select * from {CUSTOMERS} c join {ORDERS} o on 1.e5 = c.a group by 1, 2",
        f"select 1, a from {CUSTOMERS} union select 1, b from {ORDERS} group by 1, b",
        f"select 1, a from {CUSTOMERS} union all by name select 2, b from {ORDERS} o "
        f"intersect select distinct 3, c from {ORDERS} minus select -4, d from {ORDERS}",
        f"select * from {CUSTOMERS} where 1 = 1 and a in (1, 2) order by 1.5, 2 limit 1",
        f"select listagg(name, ',') within group (order by name, id) from {CUSTOMERS}",
        f"select row_number() over (partition by a order by b, c) r from {CUSTOMERS} qualify r = 1",
        f"select c.*from {CUSTOMERS} c left outer join {ORDERS} o using (id, day), {ORDERS}",
        f"select * from {CUSTOMERS} c start with c.id = 1 connect by c.id = prior c.parent",
        f"select 'from secrets, caf\u00e9', \"IDENTIFIER\" from {CUSTOMERS};",
        f"select identifier + (c.a), c.table from {CUSTOMERS} c",
    ],
)
def test_the_snowflake_table_rules_accept_ordinary_reads(sql: str) -> None:
    assert check(sql, ALLOWED, [], dialect="snowflake") == []


@pytest.mark.parametrize("part", ["from a,", "from(", "join a,", "from (a,"])
def test_the_snowflake_table_rules_take_one_pass(
    part: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work and the issues grow with the text, not with its square."""
    calls = 0
    real = sqlrules._punct

    def counted(token: Token | None, value: str) -> bool:
        nonlocal calls
        calls += 1
        return real(token, value)

    monkeypatch.setattr(sqlrules, "_punct", counted)
    sql = "select " + part * 500
    issues = table_rules(sql, ALLOWED, dialect="snowflake")
    count = len(tokenize(sql, dialect="snowflake"))
    assert 0 < len(issues) <= count and calls <= 20 * count


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


def test_the_snowflake_read_rules_never_raise_on_odd_input() -> None:
    rng = random.Random(358)
    parts = ["identifier", "table", "procedure", "system$", "system$x", "->>", "->", "(", ")", "$"]
    parts += [" ", "'a'", "'", '"SYSTEM$X"', '"', "$$x$$", "$$", ":n", ":", "?", "é", "1", "\\"]
    for _ in range(2000):
        sql = "".join(rng.choice(parts) for _ in range(rng.randint(1, 10)))
        check(sql, ["A.B.C"], ["n"], dialect="snowflake")


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
