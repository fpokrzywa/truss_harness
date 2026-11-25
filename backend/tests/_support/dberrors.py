"""A statement that fails in the database with a bound marker whose server message does not hold
it, and the log output as the product renders it (issue #373)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any  # Any: structlog's configuration

import pytest
import structlog
from sqlalchemy import Connection, text
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
