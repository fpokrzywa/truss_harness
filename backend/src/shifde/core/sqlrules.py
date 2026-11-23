"""SELECT-only rules for Databricks SQL (SP5 design 5.4): a small tokenizer and the statement,
table and parameter rules, in pure Python (S5: no parser library).

It lives in `core` (it imports nothing from `shifde`) so every layer can share it: `build` runs
`statement_rules` at tool publish on a `connection.sql` template converted to the `:name` form,
and the runtime read checks of `adapters` refuse any word of `WRITE_KEYWORDS` in a `read` invoke.
Every function takes any string and never raises; every issue is an error (callers add severity
and path).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

WRITE_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "merge", "create", "alter", "drop", "truncate", "grant",
        "revoke", "copy", "put", "get", "call", "optimize", "vacuum", "set", "use", "refresh",
        "msck", "analyze", "cache", "uncache", "restore",
    }
)  # fmt: skip
PUNCTUATION = frozenset(".,();")


@dataclass(frozen=True)
class SqlIssue:
    code: str
    message: str


@dataclass(frozen=True)
class Token:
    """`kind` is `ident` (lowercased; `quoted` for a backtick identifier), `string` (opaque),
    `param` (a `:name`, the name as written), `punct`, `comment` (`--` or `/*`) or `unterminated`
    (a quote or backtick that never closes; always the last token). A `param` also carries its
    span in the text (`start`, `end`), outside equality."""

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


def tokenize(sql: str) -> list[Token]:
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


def _bare(token: Token | None, *words: str) -> bool:
    return token is not None and token.kind == "ident" and not token.quoted and token.value in words


def _punct(token: Token | None, value: str) -> bool:
    return token is not None and token.kind == "punct" and token.value == value


def _at(tokens: list[Token], i: int) -> Token | None:
    return tokens[i] if 0 <= i < len(tokens) else None


def statement_rules(sql: str) -> list[SqlIssue]:
    tokens = tokenize(sql)
    issues: list[SqlIssue] = []
    if tokens and tokens[-1].kind == "unterminated":
        issues.append(
            SqlIssue("sql_unterminated_quote", "a quoted string or identifier is not closed")
        )
    if any(t.kind == "comment" for t in tokens):
        issues.append(SqlIssue("sql_comment", "comments (-- or /*) are not allowed"))
    if any(_punct(t, ";") for t in tokens[:-1]):
        issues.append(SqlIssue("sql_multiple_statements", "exactly one statement is allowed"))
    first = next((t for t in tokens if t.kind == "ident"), None)
    if not _bare(first, "select", "with"):
        issues.append(SqlIssue("sql_not_select", "the statement must start with SELECT or WITH"))
    seen: list[str] = []
    for t in tokens:
        if t.kind == "ident" and not t.quoted and t.value in WRITE_KEYWORDS and t.value not in seen:
            seen.append(t.value)
    for word in seen:
        issues.append(
            SqlIssue("sql_write_statement", f"{word.upper()} is not allowed in a read statement")
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


def table_rules(sql: str, tables: Iterable[str]) -> list[SqlIssue]:
    tokens = tokenize(sql)
    allowed = {str(t).lower() for t in tables}
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
            name = ".".join(parts)
            if _punct(_at(tokens, end), "("):
                issues.append(
                    SqlIssue("sql_table_function", f"table function {name} is not allowed")
                )
                end = _close(tokens, end)
            elif len(parts) >= 3:
                if name not in allowed:
                    issues.append(SqlIssue("sql_table_not_allowed", f"{name} is not allowed"))
            elif len(parts) == 2 or name not in ctes:
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

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind != "ident" or _punct(_at(tokens, i - 1), "."):
            i += 1
            continue
        parts, end = _name(tokens, i)
        if i not in referenced and len(parts) >= 3:
            name = ".".join(parts[:3])
            if name not in allowed:
                issues.append(SqlIssue("sql_table_not_allowed", f"{name} is not allowed"))
        i = max(end, i + 1)
    return issues


def parameter_rules(sql: str, declared: Iterable[str]) -> list[SqlIssue]:
    names = list(dict.fromkeys(str(d) for d in declared))
    used = list(dict.fromkeys(t.value for t in tokenize(sql) if t.kind == "param"))
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


def check(sql: str, tables: Iterable[str], declared: Iterable[str]) -> list[SqlIssue]:
    return statement_rules(sql) + table_rules(sql, tables) + parameter_rules(sql, declared)
