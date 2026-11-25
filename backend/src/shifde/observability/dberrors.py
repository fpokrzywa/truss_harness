"""Database errors in log lines (issue #384, B-4098, B-4099).

PostgreSQL names values in its own messages (`invalid input syntax for type uuid: "x"`, the DETAIL
of a constraint). An exception that holds a database error is therefore logged as type names,
traceback frames and, per database error, the SQLSTATE, the constraint and table name and the
statement text (placeholders, no values). No message, `args`, `repr` or note of any exception of
such a chain is read: a wrapper often repeats the server's text. An exception without a database
error is left to the rest of the pipeline.
"""

from __future__ import annotations

import sys
import traceback
from types import TracebackType
from typing import Any, Final  # Any: log payloads and attributes of arbitrary exceptions

import psycopg
from sqlalchemy.exc import StatementError
from structlog.typing import EventDict, WrappedLogger

from shifde.core.redaction import scrub_text

# The only server texts ever logged: shown when the whole message equals the constant (B-4098).
SAFE_MESSAGES: Final[dict[str, str]] = {"22012": "division by zero"}

_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"


def _read(obj: object, name: str) -> Any:
    """The attribute; None when it is missing or cannot be read (logging never fails on it)."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _exception(obj: object, name: str) -> BaseException | None:
    value = _read(obj, name)
    return value if isinstance(value, BaseException) else None


def _members(exc: BaseException) -> tuple[BaseException, ...]:
    return exc.exceptions if isinstance(exc, BaseExceptionGroup) else ()


def _database_error(exc: BaseException) -> BaseException | None:
    """The nearest database error `exc` is or reaches (cause, context, `.orig`, group members)."""
    seen = {id(exc)}
    queue = [exc]
    for found in queue:  # grows below
        if isinstance(found, StatementError | psycopg.Error):
            return found
        linked = [_exception(found, n) for n in ("__cause__", "__context__", "orig")]
        for other in (*linked, *_members(found)):
            if other is not None and id(other) not in seen:
                seen.add(id(other))
                queue.append(other)
    return None


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _facts(exc: BaseException) -> list[str]:
    """What is logged of a database error beside its type and statement."""
    error = _exception(exc, "orig") if isinstance(exc, StatementError) else exc
    if not isinstance(error, psycopg.Error):
        return []
    sqlstate = _read(error, "sqlstate")
    if not isinstance(sqlstate, str):  # a failed connection or an error of the client library
        status = _read(_read(error, "pgconn"), "status")  # a plain int of a finished connection
        if not isinstance(status, int) or status not in set(psycopg.pq.ConnStatus):
            return []
        return [f"connection_status={psycopg.pq.ConnStatus(status).name}"]
    out = [f"sqlstate={sqlstate}"]
    diag = _read(error, "diag")
    safe = SAFE_MESSAGES.get(sqlstate)
    if safe is not None and _read(diag, "message_primary") == safe:
        out.append(f"message={safe}")
    for label, name in (("constraint", "constraint_name"), ("table", "table_name")):
        value = _read(diag, name)
        if isinstance(value, str) and value:
            out.append(f"{label}={_one_line(value)}")
    return out


def _statement(exc: BaseException) -> str | None:
    value = _read(exc, "statement") if isinstance(exc, StatementError) else None
    # masked here: a secret of several lines does not match once lines are joined or indented
    return scrub_text(value) if isinstance(value, str) and value else None


def _title(exc: BaseException) -> str:
    facts = _facts(exc)
    return f"{_type_name(exc)}: {', '.join(facts)}" if facts else _type_name(exc)


def summary(exc: BaseException) -> str | None:
    """`exc` in one line; None when it holds no database error."""
    found = _database_error(exc)
    if found is None:
        return None
    out = _title(found) if found is exc else f"{_type_name(exc)} with {_title(found)}"
    statement = _statement(found)
    return f"{out} [SQL: {_one_line(statement)}]" if statement else out


def _block(exc: BaseException, tb: TracebackType | None) -> list[str]:
    """One exception without its chain."""
    out: list[str] = []
    if tb is not None:
        out.append("Traceback (most recent call last):")
        out += "".join(traceback.format_tb(tb)).splitlines()
    out.append(_title(exc))
    statement = _statement(exc)
    if statement:
        out += f"[SQL: {statement}]".splitlines()
    return out


def render(exc: BaseException, tb: TracebackType | None) -> str | None:
    """`exc` with its causes, contexts and group members, oldest first as Python prints them; None
    without a database error. Each exception is shown once and nothing recurses, so a cyclic or
    very long chain ends."""
    if _database_error(exc) is None:
        return None
    lines: list[str] = []
    seen = {id(exc)}
    # a stack: a line of text, an exception with its chain, or the block of one exception
    chain, block = "chain", "block"
    todo: list[tuple[str, str, BaseException | str, TracebackType | None]]
    todo = [(chain, "", exc, tb)]
    while todo:
        step, indent, item, frames = todo.pop()
        if isinstance(item, str):
            lines.append(f"{indent}{item}")
        elif step == block:
            lines += [f"{indent}{line}" for line in _block(item, frames)]
            for number, member in reversed(list(enumerate(_members(item), start=1))):
                if id(member) not in seen:
                    seen.add(id(member))
                    todo.append((chain, f"{indent}  ", member, member.__traceback__))
                    todo.append(("", f"{indent}  ", f"---- exception {number} ----", None))
        else:
            todo.append((block, indent, item, frames))
            while True:
                inner, label = _exception(item, "__cause__") or _exception(item, "orig"), _CAUSE
                if inner is None and not _read(item, "__suppress_context__"):
                    inner, label = _exception(item, "__context__"), _CONTEXT
                if inner is None or id(inner) in seen:
                    break
                seen.add(id(inner))
                todo += [("", indent, text, None) for text in ("", label, "")]
                todo.append((block, indent, inner, inner.__traceback__))
                item = inner
    return "\n".join(lines)


def without_database_errors(
    value: Any,  # Any: recursively walks arbitrary log payloads
    inside: frozenset[int] = frozenset(),
) -> Any:
    """`value` with each exception that holds a database error as its summary, at any depth
    `logging.redact` walks; `value` itself when there is none. `inside`: the containers around."""
    if isinstance(value, BaseException):
        return summary(value) or value
    if not isinstance(value, dict | list | tuple) or id(value) in inside:
        return value
    inside = inside | {id(value)}
    if isinstance(value, dict):
        items = {k: without_database_errors(v, inside) for k, v in value.items()}
        return items if any(items[k] is not v for k, v in value.items()) else value
    walked = [without_database_errors(v, inside) for v in value]
    if all(new is old for new, old in zip(walked, value, strict=True)):
        return value
    return walked if isinstance(value, list) else tuple(walked)


def _exc_info(value: Any) -> tuple[BaseException | None, TracebackType | None]:  # Any: log field
    """The exception of an `exc_info` value, found as `format_exc_info` finds it."""
    if isinstance(value, BaseException):
        return value, value.__traceback__
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[1], BaseException):
        return value[1], value[2] if isinstance(value[2], TracebackType) else None
    return sys.exc_info()[1:] if value else (None, None)


def database_errors(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Log processor: runs before `format_exc_info`, and redaction runs after it."""
    exc, tb = _exc_info(event_dict.get("exc_info"))
    shown = render(exc, tb) if exc is not None else None
    if shown is not None:
        event_dict.pop("exc_info")
        event_dict["exception"] = shown
    out: EventDict = without_database_errors(event_dict)
    return out
