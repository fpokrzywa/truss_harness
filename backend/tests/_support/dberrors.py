"""A statement that fails in the database with a bound marker whose server message does not hold
it, and the log output as the product renders it (issue #373)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any  # Any: structlog's configuration

import pytest
import structlog
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.observability.logging import configure_logging

ERROR = "sqlalchemy.exc.DataError"  # as a rendered traceback names the exception below


def new_marker() -> str:
    """A value that stands in no source line (tracebacks print source lines)."""
    return f"ocelot-{uuid7().hex}"


def fail_with(target: Session | Connection, marker: str) -> None:
    """Run a statement with `marker` bound; PostgreSQL refuses it with `division by zero`."""
    target.execute(text("SELECT CAST(:marker AS text), 1 / 0"), {"marker": marker})


@contextmanager
def rendered_logs(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    settings: Settings | None = None,
) -> Iterator[Callable[[], str]]:
    """Everything logged since, as text. `settings` applies the product's logging setup first
    (None: the caller's app has applied it). What the setup changes is restored afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    config: dict[str, Any] = structlog.get_config()
    if settings is not None:
        configure_logging(settings.log_level, settings.log_format)
    if caplog.handler not in root.handlers:
        root.addHandler(caplog.handler)  # the setup replaces the root handlers
    caplog.set_level(logging.DEBUG)
    capsys.readouterr()
    caplog.clear()

    def shown() -> str:
        captured = capsys.readouterr()
        records = "".join(str(r.__dict__) for r in caplog.records)
        return captured.out + captured.err + caplog.text + records

    try:
        yield shown
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        structlog.configure(**config)


# ------------------------------------------------------- issue #384: errors that name the marker
@dataclass(frozen=True)
class Failure:
    """Statements of which PostgreSQL refuses the last with a text that names the bound marker."""

    statements: tuple[str, ...]  # a temporary table goes with the rollback
    error: str  # as a rendered traceback names the exception
    sqlstate: str
    names: tuple[str, ...]  # the constraint and the table
    server_text: str  # of the message or the DETAIL

    def check_log(self, logged: str, marker: str) -> None:
        assert self.error in logged and self.sqlstate in logged
        for shown in (self.statements[-1], *self.names):
            assert shown in logged, shown
        assert marker not in logged and self.server_text not in logged


BAD_UUID = Failure(
    ("SELECT CAST(CAST(%(marker)s AS text) AS uuid)",),
    ERROR,
    "22P02",
    (),
    "invalid input syntax",
)
UNIQUE_VIOLATION = Failure(
    (
        "CREATE TEMPORARY TABLE named_values (v text, CONSTRAINT uq_named_values__v UNIQUE (v))",
        "INSERT INTO named_values VALUES (%(marker)s), (%(marker)s)",
    ),
    "sqlalchemy.exc.IntegrityError",
    "23505",
    ("constraint=uq_named_values__v", "table=named_values"),
    "already exists",
)
CHECK_VIOLATION = Failure(
    (
        "CREATE TEMPORARY TABLE checked_values (v text, "
        "CONSTRAINT ck_checked_values__short CHECK (length(v) < 3))",
        "INSERT INTO checked_values VALUES (%(marker)s)",
    ),
    "sqlalchemy.exc.IntegrityError",
    "23514",
    ("constraint=ck_checked_values__short", "table=checked_values"),
    "violates check constraint",
)


def fail_naming(
    target: Session | Connection, marker: str, failure: Failure, named: list[str]
) -> None:
    """Run `failure` with `marker` bound. What the server said goes to `named`, so that the caller
    can assert that the server named the marker."""
    connection = target.connection() if isinstance(target, Session) else target
    for statement in failure.statements[:-1]:
        connection.exec_driver_sql(statement)
    try:
        connection.exec_driver_sql(failure.statements[-1], {"marker": marker})
    except DBAPIError as exc:
        diag: Any = getattr(exc.orig, "diag", None)  # Any: a psycopg Diagnostic
        named += [str(exc), str(diag.message_primary), str(diag.message_detail)]
        raise
