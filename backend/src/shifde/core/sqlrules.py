"""SELECT-only rules for Databricks SQL (SP5 design 5.4) and Snowflake SQL (SP6 design 6.1, call
S6-D): a small tokenizer and the statement, table and parameter rules, in pure Python (S5: no
parser library).

It lives in `core` (it imports nothing from `shifde`) so every layer can share it: `build` runs
`statement_rules` at tool publish on a `connection.sql` template converted to the `:name` form,
with the dialect of the tool's connection, and the runtime read checks of `adapters` refuse any
word of `WRITE_KEYWORDS` in a `read` invoke (the Snowflake one also shares `FLOW_OPERATOR`,
`SYSTEM_PREFIX` and `SNOWFLAKE_REFUSED_WORDS`). Every function takes any string and never raises;
every issue is an error (callers add severity and path).

`dialect="snowflake"` reads text as `adapters/dataplatforms/sqlapi._code_of` does for Snowflake:
`'...'` takes backslash escapes, `$$...$$` is an opaque string, `"..."` is an identifier, `//` is
a comment and a backtick is no quote (`sql_syntax`). A quoted identifier is accepted only when it
equals the unquoted, uppercased name (`sql_quoted_identifier_unsupported` otherwise), a `?` is a
positional parameter (`sql_positional_parameter`), a `:` directly followed by a digit is a
positional bind reference (`sql_positional_bind`, as the runtime check refuses it, `x::1` and
`col:1` included), `SNOWFLAKE_FIRST_WORDS` are refused as the first word, and table names are
compared and reported uppercased. `table_name_supported` is the check behind the request-table
422 `table_name_unsupported`.

The Snowflake statement rules also refuse what the runtime check refuses (#358): the `->>` flow
operator in code (`sql_flow_operator`), a word or a whole `"..."` identifier starting with
`SYSTEM$` (`sql_system_function`), the word `procedure` (`sql_write_statement`) and an
`IDENTIFIER(...)` whose argument is not one plain literal or name (`sql_dynamic_name`). Here they
are stricter in two places: a bound `IDENTIFIER(:name)` is refused (no value is known yet) and
`TABLE(...)` keeps the rule of `IDENTIFIER(...)`. A word directly after a single `:` is a
parameter name, never one of these words. The Snowflake table rules (drafts and render) are
stricter again: they accept neither call, and nothing in a FROM clause they cannot read as a
table, a CTE or a subquery.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

WRITE_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "merge", "create", "alter", "drop", "truncate", "grant",
        "revoke", "copy", "put", "get", "call", "optimize", "vacuum", "set", "use", "refresh",
        "msck", "analyze", "cache", "uncache", "restore",
    }
)  # fmt: skip
# Snowflake-only statements refused when they are the first word (elsewhere they may be names)
SNOWFLAKE_FIRST_WORDS = frozenset(
    {"list", "comment", "remove", "unset", "execute", "undrop", "begin", "commit", "rollback"}
)
# the Snowflake rules `adapters/dataplatforms/sqlapi.check_read_only` shares (#358): `->>` chains a
# second statement, `SYSTEM$...` functions (`SYSTEM$CANCEL_ALL_QUERIES`) can act on the account
# (#285), `procedure` starts an anonymous procedure
FLOW_OPERATOR = "->>"
FLOW_OPERATOR_MESSAGE = "the ->> flow operator is not allowed"
SYSTEM_PREFIX = "system$"
SNOWFLAKE_REFUSED_WORDS = frozenset({"procedure"})
PUNCTUATION = frozenset(".,();")
Dialect = Literal["databricks", "snowflake"]
# the content of a Snowflake quoted identifier that names exactly what the unquoted name does
_SNOWFLAKE_PLAIN = re.compile(r"[A-Z_][A-Z0-9_$]*")
_SNOWFLAKE_TABLE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# a word as the runtime check reads it: a maximal run of letters, digits, `_` and `$`
_SNOWFLAKE_WORD = re.compile(r"[\w$]+")
_SNOWFLAKE_WORD_CHAR = re.compile(r"[\w$]")
# what a word and its `(` may have between them and still be a call: no visible ASCII character
# (whitespace, or a character the platform may skip and `\s` does not match)
CALL_GAP = re.compile(r"[^!-~]*")
# the longest SQL a drafted tool may have, and a Snowflake render accepts
MAX_TOOL_SQL_CHARS = 8000
_SNOWFLAKE_PLAIN_GAP = re.compile(r"[\s!-~]*", re.ASCII)
# a number with the dots and name characters that follow it (they can be an `ident` token, as in
# `1.e5from`), and the only run that is a number alone
_DIGIT = re.compile(r"[0-9]")
_NUMBER_RUN = re.compile(r"[0-9][A-Za-z0-9_$.]*")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]*)?(?:[eE][0-9]*)?")
# the one argument `IDENTIFIER(...)` and `TABLE(...)` may hold: a literal without a backslash or a
# doubled quote, or a plain name
_SNOWFLAKE_ARGUMENT = re.compile(r"'[^'\\]*'|[A-Za-z_][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class SqlIssue:
    code: str
    message: str


@dataclass(frozen=True)
class Token:
    """`kind` is `ident` (lowercased; `quoted` for a backtick identifier), `string` (opaque),
    `param` (a `:name`, the name as written), `punct`, `comment` (`--` or `/*`) or `unterminated`
    (a quote or backtick that never closes; always the last token). The Snowflake dialect adds
    `//` comments, `quoted_unsupported` (a `"..."` identifier that is not a plain uppercase
    name, its content as written), `positional` (a `?`), `positional_bind` (a `:` directly
    followed by a digit, the second `:` of `::` included), `flow` (the `->>` operator) and
    `syntax` (a backtick, or a `$$` right after a name character). A `param`, and in the
    Snowflake dialect every token, also carries its span in the text (`start`, `end`), outside
    equality; operators, numbers and whitespace are the text between two spans."""

    kind: str
    value: str
    quoted: bool = False
    start: int = field(default=0, compare=False, repr=False)
    end: int = field(default=0, compare=False, repr=False)


def _is_name_start(ch: str) -> bool:
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_name_char(ch: str) -> bool:
    return _is_name_start(ch) or ("0" <= ch <= "9")


def _raw_prefix(sql: str, quote: int) -> bool:
    """True when the quote at `quote` opens a raw literal: an `r`/`R` that starts a token (as
    `adapters/dataplatforms/sqlapi._code_of` reads it)."""
    if quote == 0 or sql[quote - 1] not in "rR":
        return False
    return quote == 1 or not (sql[quote - 2].isalnum() or sql[quote - 2] == "_")


def _quoted(sql: str, start: int, quote: str, escapes: bool) -> int:
    """Index just past the closing `quote` of the quoted text opened at `start`, or -1 when it
    never closes. With `escapes` a backslash takes the next character with it (Databricks reads
    `\\'` as a quote inside a string). A doubled quote closes the text and opens it again, so it
    stays inside one token."""
    i = start + 1
    while i < len(sql):
        if escapes and sql[i] == "\\":
            i += 2
            continue
        if sql[i] == quote:
            if i + 1 < len(sql) and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def tokenize(sql: str, *, dialect: Dialect = "databricks") -> list[Token]:
    if dialect == "snowflake":
        return _tokenize_snowflake(sql)
    tokens: list[Token] = []
    n, i = len(sql), 0
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in ("'", '"', "`"):
            raw = ch != "`" and _raw_prefix(sql, i)
            if raw and tokens and tokens[-1] == Token("ident", "r"):
                tokens.pop()  # the `r` of a raw literal is part of the string
            end = _quoted(sql, i, ch, escapes=ch != "`" and not raw)
            if end < 0:
                tokens.append(Token("unterminated", ch))
                return tokens
            inner = sql[i + 1 : end - 1]
            if ch == "`":
                tokens.append(Token("ident", inner.replace("``", "`").lower(), quoted=True))
            else:
                tokens.append(Token("string", inner))
            i = end
        elif ch == "-" and nxt == "-":
            tokens.append(Token("comment", "--"))
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif ch == "/" and nxt == "*":
            tokens.append(Token("comment", "/*"))
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch == ":":
            if nxt == ":":  # a cast (`x::int`), never a parameter
                i += 2
            elif _is_name_start(nxt):
                j = i + 1
                while j < n and _is_name_char(sql[j]):
                    j += 1
                tokens.append(Token("param", sql[i + 1 : j], start=i, end=j))
                i = j
            else:
                i += 1
        elif _is_name_start(ch):
            j = i
            while j < n and _is_name_char(sql[j]):
                j += 1
            tokens.append(Token("ident", sql[i:j].lower()))
            i = j
        elif "0" <= ch <= "9":  # a number (`1e5` included) is skipped as a whole
            j = i
            while j < n and _is_name_char(sql[j]):
                j += 1
            i = j
        elif ch in PUNCTUATION:
            tokens.append(Token("punct", ch))
            i += 1
        else:
            i += 1
    return tokens


def _snowflake_name_end(sql: str, j: int) -> int:
    """Index after the name characters (`$` included) from `j`; a `$$` ends the name, so the
    caller sees it right after a name character (`sql_syntax`), as the runtime check does."""
    while j < len(sql) and (_is_name_char(sql[j]) or sql[j] == "$") and not sql.startswith("$$", j):
        j += 1
    return j


def _tokenize_snowflake(sql: str) -> list[Token]:
    """The Snowflake reading of `sql` (as `sqlapi._code_of(..., "snowflake")` reads it)."""
    tokens: list[Token] = []
    n, i = len(sql), 0
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in ("'", '"'):
            end = _quoted(sql, i, ch, escapes=ch == "'")
            if end < 0:
                tokens.append(Token("unterminated", ch, start=i, end=n))
                return tokens
            inner = sql[i + 1 : end - 1]
            if ch == "'":
                tokens.append(Token("string", inner, start=i, end=end))
            else:
                name = inner.replace('""', '"')
                if _SNOWFLAKE_PLAIN.fullmatch(name):  # the same name as unquoted, never a keyword
                    tokens.append(Token("ident", name.lower(), quoted=True, start=i, end=end))
                else:
                    tokens.append(Token("quoted_unsupported", name, start=i, end=end))
            i = end
        elif ch == "$" and nxt == "$":
            if i and (sql[i - 1].isalnum() or sql[i - 1] in "_$"):
                tokens.append(Token("syntax", "$$", start=i, end=i + 2))
                i += 2
                continue
            end = sql.find("$$", i + 2)
            if end < 0:
                tokens.append(Token("unterminated", "$$", start=i, end=n))
                return tokens
            tokens.append(Token("string", sql[i + 2 : end], start=i, end=end + 2))
            i = end + 2
        elif (ch == "-" and nxt == "-") or (ch == "/" and nxt == "/"):
            ends = [e for e in (sql.find("\n", i), sql.find("\r", i)) if e >= 0]
            end = min(ends) + 1 if ends else n
            tokens.append(Token("comment", ch + nxt, start=i, end=end))
            i = end
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            tokens.append(Token("comment", "/*", start=i, end=end))
            i = end
        elif sql.startswith(FLOW_OPERATOR, i):
            end = i + len(FLOW_OPERATOR)
            tokens.append(Token("flow", FLOW_OPERATOR, start=i, end=end))
            i = end
        elif ch == "`":
            tokens.append(Token("syntax", "`", start=i, end=i + 1))
            i += 1
        elif ch == "?":
            tokens.append(Token("positional", "?", start=i, end=i + 1))
            i += 1
        elif ch == ":":
            if nxt == ":":  # a cast (`x::int`), never a parameter
                if "0" <= sql[i + 2 : i + 3] <= "9":  # `x::1`, refused as the runtime check does
                    tokens.append(Token("positional_bind", ":", start=i, end=i + 2))
                i += 2
            elif "0" <= nxt <= "9":
                tokens.append(Token("positional_bind", ":", start=i, end=i + 1))
                i += 1
            elif _is_name_start(nxt):
                j = _snowflake_name_end(sql, i + 1)
                tokens.append(Token("param", sql[i + 1 : j], start=i, end=j))
                i = j
            else:
                i += 1
        elif _is_name_start(ch):
            j = _snowflake_name_end(sql, i)
            tokens.append(Token("ident", sql[i:j].lower(), start=i, end=j))
            i = j
        elif "0" <= ch <= "9":  # a number (`1e5` included) is skipped as a whole
            i = _snowflake_name_end(sql, i)
        elif ch in PUNCTUATION:
            tokens.append(Token("punct", ch, start=i, end=i + 1))
            i += 1
        else:
            i += 1
    return tokens


def _bare(token: Token | None, *words: str) -> bool:
    return token is not None and token.kind == "ident" and not token.quoted and token.value in words


def _punct(token: Token | None, value: str) -> bool:
    return token is not None and token.kind == "punct" and token.value == value


def _at(tokens: list[Token], i: int) -> Token | None:
    return tokens[i] if 0 <= i < len(tokens) else None


def statement_rules(sql: str, *, dialect: Dialect = "databricks") -> list[SqlIssue]:
    tokens = tokenize(sql, dialect=dialect)
    issues: list[SqlIssue] = []
    if tokens and tokens[-1].kind == "unterminated":
        issues.append(
            SqlIssue("sql_unterminated_quote", "a quoted string or identifier is not closed")
        )
    snowflake = dialect == "snowflake"
    if any(t.kind == "comment" for t in tokens):
        comments = "--, // or /*" if snowflake else "-- or /*"
        issues.append(SqlIssue("sql_comment", f"comments ({comments}) are not allowed"))
    several = any(_punct(t, ";") for t in tokens[:-1])
    if dialect == "snowflake" and tokens and _punct(tokens[-1], ";"):
        several = several or sql[tokens[-1].end :].strip() != ""  # text that makes no token
    if several:
        issues.append(SqlIssue("sql_multiple_statements", "exactly one statement is allowed"))
    first = next((t for t in tokens if t.kind == "ident"), None)
    if not _bare(first, "select", "with"):
        issues.append(SqlIssue("sql_not_select", "the statement must start with SELECT or WITH"))
    seen: list[str] = []
    if snowflake:
        issues += _snowflake_rules(sql, tokens)
        if first is not None and _bare(first, *SNOWFLAKE_FIRST_WORDS):
            seen.append(first.value)
    for i, t in enumerate(tokens):
        refused = t.kind == "ident" and not t.quoted and t.value in WRITE_KEYWORDS
        if snowflake and _snowflake_word(sql, tokens, i) in SNOWFLAKE_REFUSED_WORDS:
            refused = True
        if refused and t.value not in seen:
            seen.append(t.value)
    for word in seen:
        issues.append(
            SqlIssue("sql_write_statement", f"{word.upper()} is not allowed in a read statement")
        )
    return issues


def _snowflake_word(sql: str, tokens: list[Token], i: int) -> str:
    """The lowercased word of code that starts at the unquoted `ident` at `i`, split as the
    runtime check splits words; empty when none starts there (`system$x` in `$system$x`)."""
    t = tokens[i]
    if t.kind != "ident" or t.quoted:
        return ""
    before = _at(tokens, i - 1)
    after_string = before is not None and before.kind == "string" and before.end == t.start
    if t.start and not after_string and _SNOWFLAKE_WORD_CHAR.match(sql, t.start - 1):
        return ""
    word = _SNOWFLAKE_WORD.match(sql, t.start)
    return word.group().lower() if word else ""


def _system_name(sql: str, tokens: list[Token], i: int) -> bool:
    t = tokens[i]
    quoted = t.kind == "quoted_unsupported" or t.quoted
    name = t.value.lower() if quoted else _snowflake_word(sql, tokens, i)
    return name.startswith(SYSTEM_PREFIX)


def _called(sql: str, word: Token, after: Token | None) -> bool:
    """Whether `after` is the `(` of a call of `word`."""
    if after is None or not _punct(after, "("):
        return False
    return CALL_GAP.fullmatch(sql, word.end, after.start) is not None


def _dynamic_name(sql: str, tokens: list[Token], i: int) -> bool:
    """Whether the token at `i` is the word `identifier` or `table`, called with anything but one
    plain literal (without `SYSTEM_PREFIX`) or one plain name. It reads the text between the
    parentheses: operators and a lone `$` are no tokens, and a `$$...$$` is a `string` too."""
    if _snowflake_word(sql, tokens, i) not in ("identifier", "table"):
        return False
    opened, closed = _at(tokens, i + 1), _at(tokens, i + 3)  # a plain argument is one token
    if opened is None or not _called(sql, tokens[i], opened):
        return False
    if sql[tokens[i].end : opened.start].strip():
        return True  # neither whitespace nor anything visible
    if closed is None or not _punct(closed, ")"):
        return True
    argument = sql[opened.end : closed.start].strip()
    if not _SNOWFLAKE_ARGUMENT.fullmatch(argument):
        return True
    return argument.startswith("'") and SYSTEM_PREFIX in argument.lower()


def _snowflake_rules(sql: str, tokens: list[Token]) -> list[SqlIssue]:
    issues: list[SqlIssue] = []
    if any(t == Token("syntax", "`") for t in tokens):
        issues.append(SqlIssue("sql_syntax", "backticks are not Snowflake SQL; use plain names"))
    if any(t == Token("syntax", "$$") for t in tokens):
        issues.append(SqlIssue("sql_syntax", "a $$ directly after a name is not allowed"))
    if any(t.kind == "quoted_unsupported" for t in tokens):
        issues.append(
            SqlIssue(
                "sql_quoted_identifier_unsupported",
                'a "quoted" identifier must be an uppercase name of letters, digits, _ and $',
            )
        )
    if any(t.kind == "positional" for t in tokens):
        issues.append(
            SqlIssue(
                "sql_positional_parameter",
                "positional ? parameters are not allowed; use named parameters",
            )
        )
    if any(t.kind == "positional_bind" for t in tokens):
        # the text of `adapters/dataplatforms/sqlapi._POSITIONAL_BIND` (`core` imports no layer)
        issues.append(
            SqlIssue(
                "sql_positional_bind",
                "positional :n bind references are not allowed; use named parameters",
            )
        )
    if any(t.kind == "flow" for t in tokens):
        issues.append(SqlIssue("sql_flow_operator", FLOW_OPERATOR_MESSAGE))
    if any(_system_name(sql, tokens, i) for i in range(len(tokens))):
        issues.append(SqlIssue("sql_system_function", "system functions are not allowed"))
    if any(_dynamic_name(sql, tokens, i) for i in range(len(tokens))):
        issues.append(
            SqlIssue(
                "sql_dynamic_name",
                "IDENTIFIER(...) and TABLE(...) must hold one plain string literal or name",
            )
        )
    return issues


def _name(tokens: list[Token], i: int) -> tuple[list[str], int]:
    """The dotted name starting at `i` (its parts) and the index after it."""
    parts: list[str] = []
    while True:
        t = _at(tokens, i)
        if t is None or t.kind != "ident":
            return parts, i
        parts.append(t.value)
        if not (_punct(_at(tokens, i + 1), ".") and _at(tokens, i + 2) is not None):
            return parts, i + 1
        if tokens[i + 2].kind != "ident":
            return parts, i + 1
        i += 2


def _close(tokens: list[Token], i: int) -> int:
    """Index after the `)` matching the `(` at `i` (the end when it never closes)."""
    depth = 0
    for j in range(i, len(tokens)):
        if _punct(tokens[j], "("):
            depth += 1
        elif _punct(tokens[j], ")"):
            depth -= 1
            if depth == 0:
                return j + 1
    return len(tokens)


def _ctes(tokens: list[Token]) -> set[str]:
    """Names a leading `with` defines: `name as (` or `name (cols) as (` at the top level."""
    if not _bare(next((t for t in tokens if t.kind == "ident"), None), "with"):
        return set()
    names: set[str] = set()
    depth = 0
    for i, t in enumerate(tokens):
        if _punct(t, "("):
            depth += 1
        elif _punct(t, ")"):
            depth -= 1
        elif depth == 0 and t.kind == "ident":
            after = i + 1
            if _punct(_at(tokens, after), "("):
                after = _close(tokens, after)
            if _bare(_at(tokens, after), "as") and _punct(_at(tokens, after + 1), "("):
                names.add(t.value)
    return names


def _snowflake_ctes(tokens: list[Token]) -> dict[str, int]:
    """The names the statement's leading `with` list defines, each with the index of the first
    token after its definition: before it, the name is whatever the schema holds."""
    names: dict[str, int] = {}
    if not _bare(_at(tokens, 0), "with"):
        return names
    i = 2 if _bare(_at(tokens, 1), "recursive") else 1
    while (t := _at(tokens, i)) is not None and t.kind == "ident":
        i += 1
        if _punct(_at(tokens, i), "("):
            i = _close(tokens, i)
        if not (_bare(_at(tokens, i), "as") and _punct(_at(tokens, i + 1), "(")):
            break
        i = _close(tokens, i + 1)
        names.setdefault(t.value, i)
        if not _punct(_at(tokens, i), ","):
            break
        i += 1
    return names


def _glued_number(sql: str, spans: list[tuple[int, int]]) -> bool:
    """Whether a number in the code text between tokens (`spans`) has a name directly after it,
    its decimal point and exponent included (`1from`, `1.e5from`). It looks past the span because
    the `e5from` is an `ident` token; one pass, as no run is read twice."""
    at = 0
    for start, end in spans:
        at = max(at, start)
        while (digit := _DIGIT.search(sql, at, end)) is not None:
            run = _NUMBER_RUN.match(sql, digit.start())
            if run is None or not _NUMBER.fullmatch(run.group()):
                return True
            at = run.end()
    return False


def _ends_from(sql: str, tokens: list[Token], i: int) -> bool:
    """Whether the token at `i` (never the first) starts a clause after the FROM clause. Only words
    Snowflake reserves count, SELECT only after a set operator, and none directly before a comma or
    after an operator, `::` or `$`, so no alias, type or variable ends the list of tables; a comma
    after any other word (`limit`, `offset`) is still read as one between tables."""
    t, last = tokens[i], tokens[i - 1]
    if _punct(last, ".") or _bare(last, "as"):
        return False
    before = sql[last.end : t.start].rstrip()[-1:]
    if before and not (before.isalnum() or before in "]}"):  # no end of a number, `x[0]` or `{}`
        return False
    if _bare(t, "where", "having", "qualify"):  # no comma is valid in these, after a number too
        return not _punct(_at(tokens, i + 1), ",")
    if _bare(t, "select"):
        if not _bare(last, "union", "except", "intersect", "minus", "all", "distinct", "name"):
            return False
    elif _bare(t, "group", "order", "connect", "start"):
        i += 1
        if not _bare(_at(tokens, i), "with" if t.value == "start" else "by"):
            return False
    else:
        return False
    after = _at(tokens, i + 1)
    if after is None or not _punct(after, ","):
        return True
    return sql[tokens[i].end : after.start].strip() != ""  # `order by 1, 2`


def _snowflake_references(
    sql: str, tokens: list[Token], allowed: set[str]
) -> tuple[list[SqlIssue], set[int]]:
    """The issues of a Snowflake statement's table references, and the token index of each
    reference's first part. One pass that fails closed (#358): after FROM, JOIN and every comma of
    a FROM clause (a join's and a parenthesized list's included) only an allowed name, a CTE
    defined before it or a subquery may stand, with nothing but whitespace around it."""
    issues: list[SqlIssue] = []
    referenced: set[int] = set()
    ctes = _snowflake_ctes(tokens)
    n = len(tokens)
    # `gaps[i]` is the text before token `i`; the last one is the text after all of them
    ends = [0, *(t.end for t in tokens)]
    starts = [*(t.start for t in tokens), len(sql)]
    spans = list(zip(ends, starts, strict=True))
    gaps = [sql[end:start] for end, start in spans]
    blank = [not gap.strip() for gap in gaps]
    source = SqlIssue(
        "sql_table_not_allowed",
        "only a table name or a subquery is allowed after FROM, JOIN or a comma between tables",
    )
    if not all(_SNOWFLAKE_PLAIN_GAP.fullmatch(gap) for gap in gaps):
        issues.append(
            SqlIssue("sql_syntax", "only ASCII characters are allowed outside quoted text")
        )
    if _glued_number(sql, spans):
        issues.append(SqlIssue("sql_syntax", "a name directly after a number is not allowed"))

    def function(name: str) -> SqlIssue:
        return SqlIssue("sql_table_function", f"table function {name} is not allowed")

    def unqualified(name: str) -> SqlIssue:
        return SqlIssue("sql_table_not_qualified", f"{name} must be named as database.schema.table")

    def entry(i: int) -> int:
        """Check the name at `i`, where a table is expected; the index after it."""
        parts, end = _name(tokens, i)
        referenced.add(i)
        name = ".".join(parts).upper()
        after = _at(tokens, end)
        follows = after is None or (after.kind in ("ident", "punct") and not _punct(after, "."))
        if not follows or not all(blank[i : end + 1]):
            issues.append(source)
        elif _punct(after, "("):
            issues.append(function(name))
        elif len(parts) >= 3:
            if name not in allowed:
                issues.append(SqlIssue("sql_table_not_allowed", f"{name} is not allowed"))
        elif len(parts) == 2 or _bare(tokens[i], "lateral") or ctes.get(parts[0], n) > i:
            issues.append(unqualified(name))
        return end

    # one entry per open parenthesis: "table" (a table is expected), "from" (inside a FROM clause)
    # or "" (anything else)
    stack = [""]
    i = 0
    while i < n:
        t, after = tokens[i], _at(tokens, i + 1)
        if stack[-1] == "table":
            stack[-1] = "from"
            if blank[i] and _punct(t, "("):
                query = _bare(after, "select", "with") and blank[i + 1]
                stack.append("" if query else "table")
                i += 1
                continue
            if blank[i] and t.kind == "ident":
                i = entry(i)
                continue
            issues.append(source)
        if _punct(t, "("):
            stack.append("")
        elif _punct(t, ")"):
            if len(stack) > 1:
                stack.pop()
        elif _bare(t, "from", "join") or (_punct(t, ",") and stack[-1] == "from"):
            stack[-1] = "table"
        elif _bare(t, "identifier", "table") and _called(sql, t, after):
            issues.append(function(t.value.upper()))
        elif _punct(t, ".") and _punct(after, ".") and blank[i + 1]:
            issues.append(
                SqlIssue("sql_table_not_qualified", "a name with an empty part is not allowed")
            )
        elif stack[-1] == "from" and _bare(t, "lateral"):
            issues.append(unqualified("LATERAL"))
        elif stack[-1] == "from" and _ends_from(sql, tokens, i):
            stack[-1] = ""
        i += 1
    if "table" in stack:
        issues.append(source)
    return issues, referenced


def _references(tokens: list[Token], allowed: set[str]) -> tuple[list[SqlIssue], set[int]]:
    """The issues of a Databricks statement's table references, and the token index of each
    reference's first part."""
    ctes = _ctes(tokens)
    issues: list[SqlIssue] = []
    referenced: set[int] = set()

    def reference(i: int) -> int:
        """Check the table entry at `i`; the index after it and its alias."""
        if _punct(_at(tokens, i), "("):  # a subquery: its own references are found on their own
            i = _close(tokens, i)
        else:
            parts, end = _name(tokens, i)
            if not parts:
                return i
            referenced.add(i)
            name = ".".join(parts).lower()
            if _punct(_at(tokens, end), "("):
                issues.append(
                    SqlIssue("sql_table_function", f"table function {name} is not allowed")
                )
                end = _close(tokens, end)
            elif len(parts) >= 3:
                if name not in allowed:
                    issues.append(SqlIssue("sql_table_not_allowed", f"{name} is not allowed"))
            elif len(parts) == 2 or parts[0] not in ctes:
                issues.append(
                    SqlIssue(
                        "sql_table_not_qualified", f"{name} must be named as catalog.schema.table"
                    )
                )
            i = end
        if _bare(_at(tokens, i), "as"):
            i += 1
        t = _at(tokens, i)
        if t is not None and t.kind == "ident":
            i += 1
        return i

    for i, t in enumerate(tokens):
        if _bare(t, "join"):
            reference(i + 1)
        elif _bare(t, "from"):
            j = reference(i + 1)
            while _punct(_at(tokens, j), ","):
                j = reference(j + 1)
    return issues, referenced


def table_rules(
    sql: str, tables: Iterable[str], *, dialect: Dialect = "databricks"
) -> list[SqlIssue]:
    """Snowflake names are compared and reported uppercased (Databricks: lowercased). The Snowflake
    rules (`_snowflake_references`) also refuse every `IDENTIFIER(...)` and `TABLE(...)`, a plain
    literal included. Neither dialect reads what a function names by a string or a one- or two-part
    name outside a FROM clause (`get_ddl`, `seq.nextval`): the platform's grants limit those."""
    tokens = tokenize(sql, dialect=dialect)
    snowflake = dialect == "snowflake"
    norm = str.upper if snowflake else str.lower
    allowed = {norm(str(t)) for t in tables}
    if snowflake:
        issues, referenced = _snowflake_references(sql, tokens, allowed)
    else:
        issues, referenced = _references(tokens, allowed)

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind != "ident" or _punct(_at(tokens, i - 1), "."):
            i += 1
            continue
        parts, end = _name(tokens, i)
        if i not in referenced and len(parts) >= 3:
            name = norm(".".join(parts[:3]))
            if name not in allowed:
                issues.append(SqlIssue("sql_table_not_allowed", f"{name} is not allowed"))
        i = max(end, i + 1)
    return issues


def parameter_rules(
    sql: str, declared: Iterable[str], *, dialect: Dialect = "databricks"
) -> list[SqlIssue]:
    names = list(dict.fromkeys(str(d) for d in declared))
    used = list(dict.fromkeys(t.value for t in tokenize(sql, dialect=dialect) if t.kind == "param"))
    issues = [
        SqlIssue("sql_parameter_undeclared", f":{name} is not a declared parameter")
        for name in used
        if name not in names
    ]
    issues += [
        SqlIssue("sql_parameter_unused", f"parameter {name} is not used in the statement")
        for name in names
        if name not in used
    ]
    return issues


def check(
    sql: str, tables: Iterable[str], declared: Iterable[str], *, dialect: Dialect = "databricks"
) -> list[SqlIssue]:
    return (
        statement_rules(sql, dialect=dialect)
        + table_rules(sql, tables, dialect=dialect)
        + parameter_rules(sql, declared, dialect=dialect)
    )


def table_name_supported(name: str, dialect: Dialect) -> bool:
    """Whether a requested table name can be used: on Snowflake every dot-separated part must be
    a plain name (letters, digits, `_` and `$`, not starting with a digit or `$`)."""
    if dialect != "snowflake":
        return True
    return all(_SNOWFLAKE_TABLE_PART.fullmatch(part) for part in name.split("."))
