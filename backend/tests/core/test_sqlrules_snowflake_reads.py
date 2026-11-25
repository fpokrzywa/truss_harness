"""#375: the Snowflake read rules that `statement_rules` (publish, drafts, render) and the run-time
`check_read_only` share: a comment or quote mark in an unquoted stage path, the functions refused
by name, and `raw_word_rules` (a raw `:word` of a published template, read as the run-time check
reads it). How Snowflake reads such a path is unconfirmed (#275); these are the refusing choices.
No database."""

from __future__ import annotations

import random
import re
import unicodedata

import pytest

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.dataplatforms.sqlapi import (
    _COLON_COMMENT,
    _NOT_READ,
    _REFUSED_CHARACTER,
    check_read_only,
)
from shifde.build.tools import named_sql, positional_sql
from shifde.core.sqlrules import (
    SNOWFLAKE_BLANK_LETTERS,
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
    # a path end directly after a backslash does not end the path (#398)
    "select $1 from @s/a\\ //x ->> remove @y",
    "select $1 from @s/a\\\t//x ->> remove @y",
    "select $1 from @s/a\\\r//x ->> remove @y",
    "select $1 from @s/a\\\n//x ->> remove @y",
    "select $1 from @\\ //x",
    "select $1 from @s/a\\\\ //x",  # two backslashes
    "select $1 from @s/a\\ 'b'",
    "select $1 from @s/a\\ $$b$$",
    "select $1 from @s/a\\ --x",
    "select $1 from @s/a\\ /*x*/",
    'select $1 from @s/a\\ "b"',
    "select $1 from @s/a\\ 'b",
    "select $1 from @s/a\\ b\\\t//x",
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


@pytest.mark.parametrize(
    "sql",
    [
        "select $1 from @s/a\\  //x",  # an escaped space, then a plain one
        "select $1 from @s/a\\b //x",
        "select $1 from @s/a\\ \t//x",
        "select $1 from @s/a\\\r\n//x",  # only the one character is affected
        "select $1 from @s/a\\ b 'c'",
        "select a\\ //x",  # no stage path
        "select 1 -- @s/a\\ \n, 'x'",
    ],
)
def test_only_the_character_after_the_backslash_stays_in_the_path(sql: str) -> None:
    assert _refused(sql) is None
    assert "sql_stage_path" not in _codes(sql)


def test_both_readers_take_a_long_escaped_stage_path_in_one_pass() -> None:
    path = "select $1 from @" + "a\\ " * 100000
    assert _refused(path) is None
    assert "sql_stage_path" not in _codes(path)
    assert _refused(path + "//") == STAGE_PATH_MESSAGE
    assert "sql_stage_path" in _codes(path + "//")


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


# ------------------------------------------------------------------ `//` directly after a `:` (#398)
@pytest.mark.parametrize(
    "sql",
    [
        "select $1 from snow://a/b ->> remove @s/y",
        "select a://x",
        "select x:://c",
        "select :n, a:///x",
        "select 'a' ://x",
        "select :://",
        "select 1 /* c */ a://x\n, 2",
    ],
)
def test_slashes_directly_after_a_colon_are_refused_at_run_time(sql: str) -> None:
    assert _refused(sql) == _COLON_COMMENT
    assert sql not in _COLON_COMMENT
    assert _codes(sql).count("sql_comment") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "select a: //x",
        "select a:\n//x",
        "select 'a://b'",
        "select $$a://b$$",
        'select "a://b" from t',
        "select 1 -- a://b",
        "select 1 // a://b",
        "select 1 /* a://b */",
        "select 1 /* a: *///c",
        "select 1 -- a:\n//c",
        "select 'a:'//c",
        "select :id//c",
        "select x::int // c",
        "select a:/b/c",
    ],
)
def test_other_slashes_and_colons_are_read_as_before(sql: str) -> None:
    assert _refused(sql) is None


def test_a_stage_path_mark_keeps_its_message_after_a_colon() -> None:
    sql = "select $1 from @s/a://x"
    assert _refused(sql) == STAGE_PATH_MESSAGE
    assert "sql_stage_path" in _codes(sql)


def test_publish_refuses_the_colon_comment_as_a_comment() -> None:
    sql = "select $1 from snow://a/b ->> remove @s/y"
    assert _codes(sql) == ["sql_comment"]


# ------------------------------------------------------------------ functions refused by name
REFUSED_FUNCTIONS_MESSAGE = (
    "GET_DDL, GET_PRESIGNED_URL, NEXTVAL, GETNEXTVAL, RESULT_SCAN and LAST_QUERY_ID are not allowed"
)
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
    # (#398)
    "select getnextval(s)",
    "select * from table(GetNextVal(s))",
    "select * from result_scan('x')",
    "select * from table(result_scan(last_query_id()))",
    "select last_query_id()",
    'select "LAST_QUERY_ID"()',
    "select \"result_scan\"('x')",
    "select getnextval, result_scan, last_query_id from t",
]


@pytest.mark.parametrize("sql", REFUSED_FUNCTIONS)
def test_a_function_refused_by_name_is_refused_by_both_checks(sql: str) -> None:
    assert _refused(sql) == _NOT_READ
    issues = statement_rules(sql, dialect="snowflake")
    (issue,) = [i for i in issues if i.code == "sql_refused_function"]
    assert all(name.upper() in issue.message for name in SNOWFLAKE_REFUSED_FUNCTIONS)
    assert issue.message == REFUSED_FUNCTIONS_MESSAGE
    assert sql not in issue.message


@pytest.mark.parametrize(
    "sql",
    [
        "select my_get_ddl, nextval_at from t",
        "select 'get_ddl', $$nextval$$",
        "select $nextval",
        "select my_getnextval, get_ddl$x, x$get_ddl, nextval1 from t",
        "select my_getnextval, result_scan_at, last_query_ids from t",
        "select 'result_scan', $$last_query_id$$",
    ],
)
def test_a_name_that_only_holds_a_refused_function_is_accepted(sql: str) -> None:
    assert _refused(sql) is None
    assert _codes(sql) == []


def test_the_refused_functions_are_six_and_snowflakes_only() -> None:
    assert {
        "get_ddl",
        "get_presigned_url",
        "nextval",
        "getnextval",
        "result_scan",
        "last_query_id",
    } == SNOWFLAKE_REFUSED_FUNCTIONS
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
        "select identifier('LAST_QUERY_ID')()",
        "select identifier('Result_Scan')('x')",
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
        ("select identifier(?)", ["Result_Scan"]),
        ("select identifier(?)()", ["Last_Query_Id"]),
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
    ("select :nextval, :GET_DDL", [REFUSED_FUNCTIONS_MESSAGE]),
    ("select :last_query_id", [REFUSED_FUNCTIONS_MESSAGE]),
]
CODES = {
    "system functions are not allowed": "sql_system_function",
    REFUSED_FUNCTIONS_MESSAGE: "sql_refused_function",
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
        "select %(last_query_id)s",
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


# ------------------------------------------------------------------ non-ASCII in code (#398, run time)
@pytest.mark.parametrize(
    "sql",
    [
        "select get_ddl\u3164('a')",
        "select get_ddl\u115f('a')",
        "select get_ddl\u1160('a')",
        "select get_ddl\uffa0('a')",
        "select get_\u3164ddl('a')",
        "select get_d\u200bdl('a')",
        "select\u00a0a from t",
        "select a\u0301 from t",
        "select :\u3164call",
        "select 1\u2003",
        "select \u20ac",
        "select a -- c\n, b\u2060 from t",
        "select \ud800",
        # the blank letters U+13441 and U+13442 (#425)
        "select get_ddl\U00013441('a')",
        "select get_ddl\U00013442('a')",
        "select get_\U00013441ddl('a')",
        "select get_\U00013442ddl('a')",
        "select :\U00013441call",
        "select :\U00013442call",
    ],
)
def test_a_non_ascii_character_that_is_no_visible_letter_is_refused_at_run_time(sql: str) -> None:
    assert _refused(sql) == _REFUSED_CHARACTER
    assert sql not in _REFUSED_CHARACTER
    # the raw word rules have no such issue; of these characters the statement rules refuse the blank
    # letters only (#425), so a tool with another one publishes and never runs
    assert raw_word_rules(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "select :call\u00e9",
        "select gr\u00f6\u00dfe from t",
        "select \u0430 from t",
        "select '\u3164', '\u200b'",
        'select "\u3164", "\u200b" from t',
        "select $$\u3164\u200b$$",
        "select 1 -- \u3164\u200b",
        "select 1 // \u3164\u200b",
        "select 1 /* \u3164\u200b */",
        # (#425)
        "select '\U00013441', '\U00013442'",
        'select "\U00013441", "\U00013442" from t',
        "select $$\U00013441\U00013442$$",
        "select 1 -- \U00013441\U00013442",
        "select 1 // \U00013441\U00013442",
        "select 1 /* \U00013441\U00013442 */",
    ],
)
def test_a_visible_letter_in_code_and_any_character_in_quotes_is_accepted(sql: str) -> None:
    assert _refused(sql) is None


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("select get_ddl('a'), x\u3164", _NOT_READ),
        ("select identifier\u200b('t')", _NOT_READ),
        ("drop table t\u3164", _NOT_READ),
        ("select get_ddl('a'), x\U00013441", _NOT_READ),
        ("drop table t\U00013442", _NOT_READ),
        ("select a\u00a0://x", _COLON_COMMENT),
        ("select $1 from @s\u00a0//x", STAGE_PATH_MESSAGE),
    ],
)
def test_the_non_ascii_refusal_is_the_last_one(sql: str, message: str) -> None:
    assert _refused(sql) == message


# ------------------------------------------------------------------ the blank letters (#425)
BLANK_MESSAGE = "invisible letters are not allowed outside quoted text"
BLANK_LETTERS = [0x115F, 0x1160, 0x3164, 0xFFA0, 0x13441, 0x13442]


@pytest.mark.parametrize("point", BLANK_LETTERS, ids=lambda p: f"U+{p:04X}")
def test_a_blank_letter_in_code_has_one_issue_at_publish(point: int) -> None:
    letter = chr(point)
    for sql in (
        f"select get_ddl{letter}('a')",
        f"select get_{letter}ddl('a'), '{letter}'",
        f"select :{letter}id",
        f"select {letter}",
        f"select a from t {letter}",
        f"select a{letter}, {letter}b{letter} from t{letter}",
        f"select 1{letter}",
    ):
        issues = [(i.code, i.message) for i in statement_rules(sql, dialect="snowflake")]
        assert issues == [("sql_syntax", BLANK_MESSAGE)], ascii(sql)
    for sql in (f"select '{letter}'", f"select $${letter}$$ from t", f"select 'a\\'{letter}'"):
        assert _codes(sql) == []
    # quoted text and a comment keep their own issue, and only that
    assert _codes(f'select "{letter}" from t') == ["sql_quoted_identifier_unsupported"]
    for sql in (f"select 1 -- {letter}", f"select 1 // {letter}", f"select 1 /* {letter} */"):
        assert _codes(sql) == ["sql_comment"]


def test_another_non_ascii_character_still_has_no_issue_at_publish() -> None:
    for sql in ("select \u0430 from t", "select gr\u00f6\u00dfe from t", "select a\u200b from t"):
        assert _codes(sql) == []


# Default_Ignorable_Code_Point: DerivedCoreProperties.txt of the Unicode Character Database 15.1.0
# (`unicodedata` does not carry the property)
DEFAULT_IGNORABLE = [
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
]
BLANK_WORDS = {
    "FILLER", "BLANK", "INVISIBLE", "NULL", "SPACE", "EMPTY", "VOID", "JOINER", "SEPARATOR",
}  # fmt: skip
TANGUT_IDEOGRAPHS = [*range(0x17000, 0x187F8), *range(0x18D00, 0x18D09)]


def test_no_other_letter_or_digit_is_blank_by_unicode_data() -> None:
    # Unicode has no property for an empty glyph, so blank means Default_Ignorable_Code_Point or a
    # word of the character name. U+13443..U+13446 (LOST SIGN) are drawn as hatched areas and so
    # count as visible. The scan reads Unicode data, not fonts.
    scanned = [p for p in range(0x110000) if re.match(r"\w", chr(p))]
    assert len(scanned) > 100_000
    ignorable = {p for p in scanned if any(low <= p <= high for low, high in DEFAULT_IGNORABLE)}
    assert ignorable == {0x115F, 0x1160, 0x3164, 0xFFA0}
    names = {p: unicodedata.name(chr(p), "") for p in scanned}
    named = {p for p in scanned if BLANK_WORDS & set(re.split("[ -]", names[p]))}
    assert named == {0x115F, 0x1160, 0x3164, 0xFFA0, 0x13441, 0x13442}
    assert [p for p in scanned if not names[p]] == TANGUT_IDEOGRAPHS  # nothing else has no name
    assert sorted(ignorable | named) == [ord(letter) for letter in SNOWFLAKE_BLANK_LETTERS]
    for p in ignorable | named:
        assert _refused(f"select get_ddl{chr(p)}('a')") == _REFUSED_CHARACTER


# ------------------------------------------------------------------ Databricks (#398)
@pytest.mark.parametrize(
    "sql",
    [
        "select get_ddl(1) from t",
        "select get_presigned_url(1) from t",
        "select nextval(1) from t",
        "select getnextval(1) from t",
        "select result_scan(1) from t",
        "select last_query_id() from t",
        "select a://x",
        "select $1 from @s/a\\ 'b'",
        "select a\u3164",
        "select a\u200b from t",
        # (#425)
        "select get_ddl\U00013441('a') from t",
        "select get_ddl\U00013442('a') from t",
    ],
)
def test_databricks_reads_the_texts_of_398_as_before(sql: str) -> None:
    check_read_only(sql)
    assert statement_rules(sql) == []
