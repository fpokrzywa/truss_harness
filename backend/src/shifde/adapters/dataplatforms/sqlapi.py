"""Shared toolkit of the REST SQL-API adapters (design 2026-11-22 sections 7.1 and 7.4).

* `check_read_only`: the keyword check a `read` invoke passes before any network call. It is
  defense in depth; the read-only credential (B-1507) is the real control.
* `classify_status`: HTTP status -> `ErrorClass`, with fixed messages (never a body).
* `send`: one request through an egress-guarded client, with one `Retry-After` retry on 429.
* `run_statement`: submit, poll, cancel at the deadline.
* `cap_rows`: the row cap.

Nothing here reads settings or the environment; every request goes through `ctx.egress`.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal  # Any: decoded JSON bodies, httpx options, result rows

import httpx

from shifde.adapters.base import AdapterFailure, ErrorClass, ExecContext
from shifde.adapters.httputil import classify_exception
from shifde.core.egress import EgressViolation

JSON = dict[str, Any]  # Any: free-form JSON object

READ_KEYWORDS = frozenset({"select", "with", "show", "describe", "desc", "explain"})
_FIRST_WORD = re.compile(r"\s*([A-Za-z]+)\b")
POLL_INTERVAL_S = 0.25
# the statement loop ends this long before the guard's own deadline, so the cancel request can
# still pass the guard (which refuses every request once its deadline has passed)
CANCEL_RESERVE_S = 1.0

_NOT_READ = "only read statements are allowed (read_write access and connection:write needed)"
_ONE_STATEMENT = "exactly one statement is allowed"
_UNTERMINATED = "the statement has an unterminated quote or comment"
_NESTED = "nested block comments are not allowed"
_AMBIGUOUS_DOLLARS = "a $$ directly after a name is not allowed"

Dialect = Literal["databricks", "snowflake"]
# Snowflake dialect: a word is a maximal run of letters, digits, `_` and `$`
_WORD = re.compile(r"[\w$]+")
_SNOWFLAKE_REFUSED_WORDS = frozenset({"procedure", "call"})


def _raw_prefix(sql: str, quote: int) -> bool:
    """True when the quote at `quote` opens a raw literal: an `r`/`R` that starts a token."""
    if quote == 0 or sql[quote - 1] not in "rR":
        return False
    return quote == 1 or not (sql[quote - 2].isalnum() or sql[quote - 2] == "_")


def _code_of(sql: str, dialect: Dialect = "databricks") -> tuple[str, list[int], str]:
    """`sql` with comments replaced by spaces, the offsets of every `;` outside quotes, and the
    code alone (quoted text also replaced by a space, so a word search never sees it).

    Single- and double-quoted text honours backslash escapes (Databricks SQL reads `\\'` as a
    quote inside a string); a raw literal (`r'...'`, `R"..."`) and backtick identifiers do not.
    A doubled quote needs no special case: it
    closes the text and opens it again. A `/*` inside a block comment is refused, so a dialect
    that nests block comments and one that does not cannot read the statement differently.
    The Snowflake dialect also reads `$$...$$` as a string (no escapes) and `//` as a line
    comment, and refuses a `$$` right after a word character, where Snowflake may read it as
    part of an identifier (B-3211)."""
    snowflake = dialect == "snowflake"
    out: list[str] = []
    bare: list[str] = []
    semicolons: list[int] = []
    size = 0  # length of the text in `out` so far
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"', "`"):
            escapes = ch != "`" and not _raw_prefix(sql, i)
            j = i + 1
            while j < n and sql[j] != ch:
                j += 2 if escapes and sql[j] == "\\" else 1
            if j >= n:
                raise AdapterFailure(ErrorClass.CAPABILITY, _UNTERMINATED)
            out.append(sql[i : j + 1])
            bare.append(" ")
            size += j + 1 - i
            i = j + 1
        elif snowflake and sql.startswith("$$", i):
            if i and (sql[i - 1].isalnum() or sql[i - 1] in "_$"):
                raise AdapterFailure(ErrorClass.CAPABILITY, _AMBIGUOUS_DOLLARS)
            end = sql.find("$$", i + 2)
            if end < 0:
                raise AdapterFailure(ErrorClass.CAPABILITY, _UNTERMINATED)
            out.append(sql[i : end + 2])
            bare.append(" ")
            size += end + 2 - i
            i = end + 2
        elif sql.startswith("--", i) or (snowflake and sql.startswith("//", i)):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            bare.append(" ")
            size += 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise AdapterFailure(ErrorClass.CAPABILITY, _UNTERMINATED)
            if "/*" in sql[i + 2 : end]:
                raise AdapterFailure(ErrorClass.CAPABILITY, _NESTED)
            i = end + 2
            out.append(" ")
            bare.append(" ")
            size += 1
        else:
            if ch == ";":
                semicolons.append(size)
            out.append(ch)
            bare.append(ch)
            size += 1
            i += 1
    return "".join(out), semicolons, "".join(bare)


def check_read_only(sql: str, *, dialect: Dialect = "databricks") -> None:
    """Refuse (CAPABILITY, 422) anything but one statement whose first keyword is a read keyword;
    comments are stripped first, quoted text is never read as code. The Snowflake dialect also
    refuses a `procedure` or `call` word anywhere in the code (anonymous procedures)."""
    code, semicolons, bare = _code_of(sql, dialect)
    stripped = code.rstrip()
    if semicolons:
        if len(semicolons) > 1 or semicolons[0] != len(stripped) - 1:
            raise AdapterFailure(ErrorClass.CAPABILITY, _ONE_STATEMENT)
        stripped = stripped[:-1]
    first = _FIRST_WORD.match(stripped)
    if first is None or first.group(1).lower() not in READ_KEYWORDS:
        raise AdapterFailure(ErrorClass.CAPABILITY, _NOT_READ)
    if dialect == "snowflake" and any(
        word.lower() in _SNOWFLAKE_REFUSED_WORDS for word in _WORD.findall(bare)
    ):
        raise AdapterFailure(ErrorClass.CAPABILITY, _NOT_READ)


def classify_status(status: int, *, subject: str = "the platform") -> AdapterFailure | None:
    """None for a 2xx answer, else the classified failure (fixed text and the status only)."""
    if 200 <= status < 300:
        return None
    if 300 <= status < 400:
        return AdapterFailure(
            ErrorClass.EGRESS,
            f"{subject} answered a redirect (HTTP {status}); redirects are not followed",
        )
    if status in (401, 403):
        return AdapterFailure(
            ErrorClass.AUTH, f"{subject} rejected the credentials (HTTP {status})"
        )
    if status == 404:
        return AdapterFailure(ErrorClass.CONFIG, f"{subject} answered HTTP 404 (not found)")
    if status == 429:
        return AdapterFailure(ErrorClass.RATE_LIMIT, f"{subject} is rate limiting (HTTP 429)")
    return AdapterFailure(ErrorClass.UPSTREAM, f"{subject} answered HTTP {status}")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    return float(raw) if raw.isdigit() else None


def send(
    ctx: ExecContext,
    client: httpx.Client,
    method: str,
    url: str,
    **kwargs: Any,  # Any: httpx request options
) -> httpx.Response:
    """One request. A 429 whose numeric `Retry-After` fits the remaining deadline (or, with no
    deadline, the policy timeout) is waited out through the guard and retried exactly once;
    otherwise a 429 raises RATE_LIMIT. Transport errors are classified, never echoed."""
    for attempt in (0, 1):
        try:
            response = client.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            raise classify_exception(exc) from exc
        if response.status_code != 429:
            return response
        wait = _retry_after(response)
        left = ctx.egress.remaining()
        limit = left if left is not None else ctx.egress.timeout_seconds
        if attempt or wait is None or wait >= limit:
            break
        try:
            ctx.egress.sleep(wait)
        except EgressViolation as exc:
            raise classify_exception(exc) from exc
    raise AdapterFailure(ErrorClass.RATE_LIMIT, "the platform is rate limiting (HTTP 429)")


@dataclass(frozen=True)
class StatementCalls:
    """The platform-specific steps `run_statement` drives."""

    submit: Callable[[], JSON]
    poll: Callable[[str], JSON]
    cancel: Callable[[str], None]
    statement_id: Callable[[JSON], str | None]
    pending: Callable[[JSON], bool]


_TIMED_OUT = "the statement did not finish within its time limit and was cancelled"


def run_statement(
    ctx: ExecContext,
    calls: StatementCalls,
    *,
    timeout_seconds: float,
    poll_interval: float = POLL_INTERVAL_S,
) -> JSON:
    """Submit, then poll while pending. The deadline is the smaller of `timeout_seconds` and the
    guard's remaining budget (less `CANCEL_RESERVE_S`); when it passes, the statement is
    cancelled once (best effort) and TIMEOUT is raised."""
    budget = timeout_seconds
    left = ctx.egress.remaining()
    if left is not None:
        budget = min(budget, left - CANCEL_RESERVE_S)
    if budget <= 0:
        raise AdapterFailure(ErrorClass.TIMEOUT, "the call exceeded its deadline")
    end = time.monotonic() + budget
    doc = calls.submit()
    while calls.pending(doc):
        statement_id = calls.statement_id(doc)
        if not statement_id:
            raise AdapterFailure(ErrorClass.UPSTREAM, "the platform sent no statement id")
        now = time.monotonic()
        if now >= end:
            _cancel(calls, statement_id)
            raise AdapterFailure(ErrorClass.TIMEOUT, _TIMED_OUT)
        try:
            ctx.egress.sleep(min(poll_interval, end - now))
            doc = calls.poll(statement_id)
        except (AdapterFailure, EgressViolation) as exc:
            failure = classify_exception(exc)
            if failure.error_class is ErrorClass.TIMEOUT:
                _cancel(calls, statement_id)
            raise failure from exc
    return doc


def _cancel(calls: StatementCalls, statement_id: str) -> None:
    # best effort: a failed cancel must not hide the timeout the caller sees
    with contextlib.suppress(Exception):
        calls.cancel(statement_id)


def cap_rows(rows: Sequence[Any], max_rows: int) -> tuple[list[Any], bool]:
    """At most `max_rows` rows, and whether more came back."""
    return list(rows[:max_rows]), len(rows) > max_rows
