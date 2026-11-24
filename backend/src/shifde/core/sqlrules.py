"""SELECT-only rules for Databricks SQL (SP5 design 5.4) and Snowflake SQL (SP6 design 6.1, call
S6-D): a small tokenizer and the statement, table and parameter rules, in pure Python (S5: no
parser library).

It lives in `core` (it imports nothing from `shifde`) so every layer can share it: `build` runs
`statement_rules` at tool publish on a `connection.sql` template converted to the `:name` form,
with the dialect of the tool's connection, and the runtime read checks of `adapters` refuse any
word of `WRITE_KEYWORDS` in a `read` invoke. Every function takes any string and never raises;
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
PUNCTUATION = frozenset(".,();")
Dialect = Literal["databricks", "snowflake"]
# the content of a Snowflake quoted identifier that names exactly what the unquoted name does
_SNOWFLAKE_PLAIN = re.compile(r"[A-Z_][A-Z0-9_$]*")
_SNOWFLAKE_TABLE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


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
    followed by a digit, the second `:` of `::` included) and `syntax` (a backtick, or a `$$`
    right after a name character). A `param` also carries its span in the text (`start`, `end`),
    outside equality."""

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
                tokens.append(Token("unterminated", ch))
                return tokens
            inner = sql[i + 1 : end - 1]
            if ch == "'":
                tokens.append(Token("string", inner))
            else:
                name = inner.replace('""', '"')
                if _SNOWFLAKE_PLAIN.fullmatch(name):  # the same name as unquoted, never a keyword
                    tokens.append(Token("ident", name.lower(), quoted=True))
                else:
                    tokens.append(Token("quoted_unsupported", name))
            i = end
        elif ch == "$" and nxt == "$":
            if i and (sql[i - 1].isalnum() or sql[i - 1] in "_$"):
                tokens.append(Token("syntax", "$$"))
                i += 2
                continue
            end = sql.find("$$", i + 2)
            if end < 0:
                tokens.append(Token("unterminated", "$$"))
                return tokens
            tokens.append(Token("string", sql[i + 2 : end]))
            i = end + 2
        elif (ch == "-" and nxt == "-") or (ch == "/" and nxt == "/"):
            tokens.append(Token("comment", ch + nxt))
            ends = [e for e in (sql.find("\n", i), sql.find("\r", i)) if e >= 0]
            i = min(ends) + 1 if ends else n
        elif ch == "/" and nxt == "*":
            tokens.append(Token("comment", "/*"))
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch == "`":
            tokens.append(Token("syntax", "`"))
            i += 1
        elif ch == "?":
            tokens.append(Token("positional", "?"))
            i += 1
        elif ch == ":":
            if nxt == ":":  # a cast (`x::int`), never a parameter
                if "0" <= sql[i + 2 : i + 3] <= "9":  # `x::1`, refused as the runtime check does
                    tokens.append(Token("positional_bind", ":"))
                i += 2
            elif "0" <= nxt <= "9":
                tokens.append(Token("positional_bind", ":"))
                i += 1
            elif _is_name_start(nxt):
                j = _snowflake_name_end(sql, i + 1)
                tokens.append(Token("param", sql[i + 1 : j], start=i, end=j))
                i = j
            else:
                i += 1
        elif _is_name_start(ch):
            j = _snowflake_name_end(sql, i)
            tokens.append(Token("ident", sql[i:j].lower()))
            i = j
        elif "0" <= ch <= "9":  # a number (`1e5` included) is skipped as a whole
            i = _snowflake_name_end(sql, i)
        elif ch in PUNCTUATION:
            tokens.append(Token("punct", ch))
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
    if any(_punct(t, ";") for t in tokens[:-1]):
        issues.append(SqlIssue("sql_multiple_statements", "exactly one statement is allowed"))
    first = next((t for t in tokens if t.kind == "ident"), None)
    if not _bare(first, "select", "with"):
        issues.append(SqlIssue("sql_not_select", "the statement must start with SELECT or WITH"))
    seen: list[str] = []
    if snowflake:
        issues += _snowflake_rules(tokens)
        if first is not None and _bare(first, *SNOWFLAKE_FIRST_WORDS):
            seen.append(first.value)
    for t in tokens:
        if t.kind == "ident" and not t.quoted and t.value in WRITE_KEYWORDS and t.value not in seen:
            seen.append(t.value)
    for word in seen:
        issues.append(
            SqlIssue("sql_write_statement", f"{word.upper()} is not allowed in a read statement")
        )
    return issues


def _snowflake_rules(tokens: list[Token]) -> list[SqlIssue]:
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


def table_rules(
    sql: str, tables: Iterable[str], *, dialect: Dialect = "databricks"
) -> list[SqlIssue]:
    """Snowflake names are compared and reported uppercased (Databricks: lowercased)."""
    tokens = tokenize(sql, dialect=dialect)
    snowflake = dialect == "snowflake"
    norm = str.upper if snowflake else str.lower
    allowed = {norm(str(t)) for t in tables}
    qualified = "database.schema.table" if snowflake else "catalog.schema.table"
    ctes = _ctes(tokens)
    issues: list[SqlIssue] = []
    referenced: set[int] = set()  # token index of each table reference's first part

    def reference(i: int) -> int:
        """Check the table entry at `i`; the index after it and its alias."""
        if _punct(_at(tokens, i), "("):  # a subquery: its own references are found on their own
            i = _close(tokens, i)
        else:
            parts, end = _name(tokens, i)
            if not parts:
                return i
            referenced.add(i)
            name = norm(".".join(parts))
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
                    SqlIssue("sql_table_not_qualified", f"{name} must be named as {qualified}")
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
