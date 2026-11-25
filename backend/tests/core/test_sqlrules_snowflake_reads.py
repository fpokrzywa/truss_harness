"""#375: the Snowflake read rules that `statement_rules` (publish, drafts, render) and the run-time
`check_read_only` share: a comment or quote mark in an unquoted stage path, the functions refused
by name, and `raw_word_rules` (a raw `:word` of a published template, read as the run-time check
reads it). How Snowflake reads such a path is unconfirmed (#275); these are the refusing choices.
No database."""

from __future__ import annotations

import random

import pytest

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.dataplatforms.sqlapi import _NOT_READ, check_read_only
from shifde.build.tools import named_sql, positional_sql
from shifde.core.sqlrules import (
    SNOWFLAKE_REFUSED_FUNCTIONS,
    STAGE_PATH_MESSAGE,
    check,
    raw_word_rules,
    statement_rules,
)


def _codes(sql: str) -> list[str]:
    return [i.code for i in statement_rules(sql, dialect="snowflake")]


def _refused(sql: str, params: object = None) -> str | None:
    """The message of the run-time refusal, None when the statement is accepted."""
    try:
        check_read_only(sql, dialect="snowflake", params=params)
    except AdapterFailure as exc:
        assert exc.error_class is ErrorClass.CAPABILITY
        return exc.message
    return None


# ------------------------------------------------------------------ marks in a stage path
STAGE_MARKS = [
    "select $1 from @s//x ->> remove @s/y",
    "select $1 from @s/a--b ->> remove @s/y",
    "select $1 from @s/*x ->> remove @s/*/y",
    "select $1 from @s/it's ->> remove @s/o'x",
    'select $1 from @s/a"b ->> remove @s/c"d',
    "select $1 from @s/$$a ->> remove @s/b$$",
    "select $1 from @~//x ->> remove @~/y",
    "select $1 from @%t//x",
    'select $1 from @"S"//x',
    "select $1 from @db.sc.s//x",
    "select $1 from @s/a)//x ->> remove @s/y",
    "select $1 from @s/a,'b'",
    "select $1 from @s //x",  # a non-ASCII space does not end the path
    # directly after the `@`
    'select $1 from @"S"/x',
    "select $1 from @'s'",
    "select $1 from @//x",
    "select $1 from @--x",
    "select $1 from @/*x*/s",
    "select $1 from @$$s$$",
    # only a space, a tab, a line feed or a carriage return ends it
    "select $1 from @s(file_format=>'f')",
    "select $1 from (select $1 from @s;//x",
    "select $1 from @s/a 'b'",
    "select $1 from @s/a\x0b//x",
    "select $1 from @s/a\x0c'b'",
    "select $1 from @s/a --x",
    "select $1 from @s/a:b:c?d`e+1.5e3//x",
    "select $1 from @s/a$$b",
    "select $1 from @s/a::1//x",
    "select $1 from @s/a->>b//x",
    "select $1 from @s/@t'x'",
    "select $1 from t@s'x'",
    # a path after a string, a comment or another path
    "select 'a'@s'b'",
    "select '@', $1 from @s//x",
    "select $1 from @s -- c\n, @t//x",
    "select $1 from @s -- c\r, @t//x",
    "select $1 from @s/x, 1 @t\t@u'x'",
    "select 1 /* @ */, $1 from @s//x",
    # a quote or comment that never closes
    "select $1 from @s/it's",
    'select $1 from @s/a"b',
    "select $1 from @s/$$a",
    "select $1 from @s/*x",
]


@pytest.mark.parametrize("sql", STAGE_MARKS)
def test_a_mark_in_an_unquoted_stage_path_is_refused_by_both_checks(sql: str) -> None:
    assert _refused(sql) == STAGE_PATH_MESSAGE
    issues = [
        i for i in statement_rules(sql, dialect="snowflake") if i.message == STAGE_PATH_MESSAGE
    ]
    assert [i.code for i in issues] == ["sql_stage_path"]
    assert _codes(sql).count("sql_stage_path") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "select $1 from @s/x -- c",
        "select $1 from @s/x // c",
        "select $1 from @s /* c */",
        "select $1 from @s (file_format => 'f')",
        "select $1 from @~",
        "select $1 from @%t",
        "select 'a@b'||'c'",
        "select '@s//x'",
        "select $$@s//x$$",
        'select "a@b" from t -- c',
        "select 1 -- @s\n, 'x'",
        "select $1 from @~/x, @%t/y, @s/x/y.csv, @db.sc.s/a-b_c.csv.gz",
        "select $1 from @s\t-- c",
        "select $1 from @s\n// c",
        "select $1 from @s\r'c'",
        "select $1 from @s/a/ /b",
        "select 1 // @s\r, 'x'",
        "select 1 /* @s */, 'x'",
        'select 1 /* @s */"A"',
        "select '@''s' || 'x', \"@\"\"s\" from t",
        "select a/b, a-b, '@'||'b', $$@$$||'c'",
    ],
)
def test_text_without_a_mark_in_a_stage_path_is_read_as_before(sql: str) -> None:
    assert _refused(sql) is None
    assert "sql_stage_path" not in _codes(sql)


def test_a_stage_path_without_a_mark_has_no_statement_issue() -> None:
    assert _codes("select $1 from @s (file_format => 'f')") == []
    assert _codes("select $1 from @~/x, @%t, @s/x/y.csv") == []


def test_the_stage_path_rule_is_snowflakes_only() -> None:
    sql = "select '1' from x@s'a'"
    check_read_only(sql)
    assert statement_rules(sql) == []


def test_both_readers_take_long_stage_paths_in_one_pass() -> None:
    marks = "select $1 from " + "@s'a' " * 8000
    assert _refused(marks) == STAGE_PATH_MESSAGE
    assert _codes(marks) == ["sql_stage_path"]
    path = "select $1 from @" + "a/" * 100000
    assert _refused(path) is None
    assert _codes(path) == []
    assert _refused(path + "/") == STAGE_PATH_MESSAGE
    assert _codes(path + "/") == ["sql_comment", "sql_stage_path"]


def test_the_two_checks_agree_on_random_stage_paths() -> None:
    rng = random.Random(375)
    parts = ["@", "@s", "/", "-", "--", "//", "/*", "*/", "'", '"', "$", "$$", "\\", "x", " "]
    parts += ["\t", "\r", "\n", " ", ";", "(", ",", ":n", "?", "`", "->>", "1"]
    outcomes = set()
    for _ in range(5000):
        sql = "select " + "".join(rng.choice(parts) for _ in range(rng.randint(1, 12)))
        message = _refused(sql)
        codes = _codes(sql)
        outcomes.add(message == STAGE_PATH_MESSAGE)
        if message == STAGE_PATH_MESSAGE:
            assert "sql_stage_path" in codes, sql
        if "sql_stage_path" in codes:
            assert message is not None, sql
    assert outcomes == {True, False}


# ------------------------------------------------------------------ functions refused by name
REFUSED_FUNCTIONS = [
    "select get_ddl('table', 't')",
    "select GET_DDL ('table', 't')",
    "select snowflake.x.Get_Ddl('table','t')",
    "select \"GET_DDL\"('table','t')",
    "select \"get_ddl\"('table','t')",
    "select get_presigned_url(@s, 'f')",
    "select \"Get_Presigned_Url\"('s', 'f')",
    "select seq.nextval",
    "select db.sc.seq.NextVal",
    "select seq . nextval",
    'select "SEQ"."NEXTVAL"',
    'select "seq"."nextval"',
    "select $$a$$nextval",
    "select 'a'get_ddl('table','t')",
    "select a::nextval",
    "select get_ddl, nextval, get_ddl from t",
    "with a as (select seq.nextval as n) select n from a",
]


@pytest.mark.parametrize("sql", REFUSED_FUNCTIONS)
def test_a_function_refused_by_name_is_refused_by_both_checks(sql: str) -> None:
    assert _refused(sql) == _NOT_READ
    issues = statement_rules(sql, dialect="snowflake")
    (issue,) = [i for i in issues if i.code == "sql_refused_function"]
    assert all(name.upper() in issue.message for name in SNOWFLAKE_REFUSED_FUNCTIONS)
    assert sql not in issue.message


@pytest.mark.parametrize(
    "sql",
    [
        "select my_get_ddl, nextval_at from t",
        "select 'get_ddl', $$nextval$$",
        "select $nextval",
        "select getnextval, get_ddl$x, x$get_ddl, nextval1 from t",
    ],
)
def test_a_name_that_only_holds_a_refused_function_is_accepted(sql: str) -> None:
    assert _refused(sql) is None
    assert _codes(sql) == []


def test_the_refused_functions_are_three_and_snowflakes_only() -> None:
    assert {"get_ddl", "get_presigned_url", "nextval"} == SNOWFLAKE_REFUSED_FUNCTIONS
    for name in SNOWFLAKE_REFUSED_FUNCTIONS:
        sql = f"select {name}(1) from t"
        check_read_only(sql)
        assert statement_rules(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "select identifier('GET_DDL')('table','t')",
        "select identifier('sc.seq.nextval')",
        "select identifier( ' Get_Presigned_Url ' )(1)",
        "select identifier('\"GET_DDL\"')(1)",
    ],
)
def test_an_identifier_literal_with_a_refused_function_is_refused(sql: str) -> None:
    assert _refused(sql) == _NOT_READ
    assert _codes(sql) == ["sql_dynamic_name"]
    assert _codes(sql.replace("identifier", "table")) == ["sql_dynamic_name"]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("select identifier(?)", ["SEQ.NEXTVAL"]),
        ("select identifier(?)('table', ?)", ["ordinary", "Get_Ddl"]),
        ("select identifier(:f)(1)", {"f": "get_presigned_url"}),
    ],
)
def test_a_bound_refused_function_is_refused_at_run_time(sql: str, params: object) -> None:
    assert _refused(sql, params) == _NOT_READ


def test_a_bound_value_without_a_refused_function_or_call_is_accepted() -> None:
    assert _refused("select identifier(?)", ["MY_DB.PUBLIC.T"]) is None
    assert _refused("select ?", ["get_ddl"]) is None


# ------------------------------------------------------------------ raw `:word`s of a template
RAW_WORDS = [
    ("select :call", ["CALL is not allowed in a read statement"]),
    ("select src:drop from t", ["DROP is not allowed in a read statement"]),
    ("select :Delete", ["DELETE is not allowed in a read statement"]),
    ("select :procedure", ["PROCEDURE is not allowed in a read statement"]),
    (
        "select :call, :put, :CALL",
        [
            "CALL is not allowed in a read statement",
            "PUT is not allowed in a read statement",
        ],
    ),
    ("select :system$x, :SYSTEM$y", ["system functions are not allowed"]),
    ("select :nextval, :GET_DDL", ["GET_DDL, GET_PRESIGNED_URL and NEXTVAL are not allowed"]),
]
CODES = {
    "system functions are not allowed": "sql_system_function",
    "GET_DDL, GET_PRESIGNED_URL and NEXTVAL are not allowed": "sql_refused_function",
}


@pytest.mark.parametrize(("template", "messages"), RAW_WORDS)
def test_a_raw_word_is_refused_as_the_run_time_check_refuses_it(
    template: str, messages: list[str]
) -> None:
    sql, _ = positional_sql(template)
    assert _refused(sql) == _NOT_READ
    assert [(i.code, i.message) for i in raw_word_rules(sql)] == [
        (CODES.get(message, "sql_write_statement"), message) for message in messages
    ]
    assert _codes(named_sql(template)) == []  # the statement rules read `:word` as before


@pytest.mark.parametrize(
    "template",
    [
        "select :identifier(%(a)s||'x')",
        "select :identifier('SYSTEM$X')",
        "select :Identifier ​ ('t')",
        "select :identifier($x)",
        "select :identifier('get_ddl')(1)",
        "select :identifier(",
    ],
)
def test_a_called_raw_identifier_is_refused_as_at_run_time(template: str) -> None:
    sql, _ = positional_sql(template)
    assert _refused(sql) == _NOT_READ
    assert [i.code for i in raw_word_rules(sql)] == ["sql_dynamic_name"]
    assert _codes(named_sql(template)) == []


def test_a_called_raw_identifier_keeps_publishs_rule_of_a_bound_argument() -> None:
    sql, _ = positional_sql("select :identifier(%(a)s)")
    assert _refused(sql, ["MY_DB.PUBLIC.T"]) is None  # its value is checked at run time
    assert [i.code for i in raw_word_rules(sql)] == ["sql_dynamic_name"]


@pytest.mark.parametrize(
    "template",
    [
        "select %(call)s, %(drop)s, %(nextval)s",
        "select * from t where a = %(procedure)s and b = %(get_ddl)s",
        "select :id, src:customer, :called, :drop_at, :call$x, :my_system$x from t",
        "select ':call', $$:drop$$, x::int from t",
        "select :identifier, :table, :identifier('t'), :identifier(my_var)",
        "select :table($t)",
        "select :callé",
    ],
)
def test_bound_parameters_and_other_raw_words_have_no_issue(template: str) -> None:
    sql, _ = positional_sql(template)
    assert raw_word_rules(sql) == []
    assert _refused(sql) is None


def test_every_raw_word_the_run_time_check_refuses_has_an_issue() -> None:
    words = ["insert", "get", "set", "use", "call", "procedure", "nextval", "get_ddl", "system$y"]
    for word in [*words, "get_presigned_url", "select", "id", "execute", "remove"]:
        sql = f"select :{word}"
        assert bool(raw_word_rules(sql)) is (_refused(sql) is not None), word


def test_a_draft_parameter_of_such_a_name_is_accepted() -> None:
    assert check("select :call, :nextval", [], ["call", "nextval"], dialect="snowflake") == []


def test_the_raw_word_rules_never_raise_on_odd_input() -> None:
    rng = random.Random(3750)
    parts = [":call", ":identifier", ":system$x", ":", "::", "(", ")", "'", '"', "$$", "?", "'a'"]
    parts += [" ", "​", "é", "$", "@", "--", "//", "/*", "\\", "1", ";"]
    for _ in range(2000):
        raw_word_rules("".join(rng.choice(parts) for _ in range(rng.randint(0, 10))))
