"""structlog configuration: JSON (or console) logs with request/tenant/principal correlation and
secret redaction. Nothing from request bodies, cookies or headers is ever logged, nor the
database server's own messages (`dberrors`)."""

from __future__ import annotations

import logging
import sys
from typing import Any  # Any: structlog event dicts are heterogeneous by design

import structlog
from structlog.typing import EventDict, WrappedLogger

from shifde.core.context import current_log_context
from shifde.core.httplogs import quiet_http_client_logs
from shifde.core.redaction import REDACTED, is_sensitive_key, scrub_text
from shifde.observability.dberrors import database_errors, without_database_errors


def add_request_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Merge request_id / tenant_id / principal_id bound for the current request."""
    for key, value in current_log_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def redact(value: Any) -> Any:  # Any: recursively walks arbitrary log payloads
    if isinstance(value, str):
        return scrub_text(value)  # registered secret values + token/key/PEM patterns
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def redact_processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    return {  # top-level keys; nested payloads handled by redact()
        k: (REDACTED if is_sensitive_key(k) else redact(v)) for k, v in event_dict.items()
    }


def _stdout_logger_factory(*_: object) -> structlog.PrintLogger:
    # Resolve sys.stdout per call so swapped/closed streams (pytest capture) never go stale.
    return structlog.PrintLogger(file=sys.stdout)


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """StreamHandler that always writes to the *current* sys.stdout (survives stream swaps)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:  # Any: matches the typeshed `SupportsWrite[str]` attribute
        return sys.stdout

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


class _Formatter(structlog.stdlib.ProcessorFormatter):
    """ProcessorFormatter that also keeps database errors out of a record's message."""

    def format(self, record: logging.LogRecord) -> str:
        msg, args = without_database_errors(record.msg), without_database_errors(record.args)
        if msg is not record.msg or args is not record.args:
            record = logging.makeLogRecord(record.__dict__)  # other handlers get the original
            record.msg, record.args = msg, args
        return super().format(record)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    shared: list[Any] = [  # Any: structlog processor callables
        add_request_context,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        database_errors,
        structlog.processors.format_exc_info,
        redact_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=_stdout_logger_factory,
        cache_logger_on_first_use=False,
    )
    # Route stdlib logging (uvicorn, sqlalchemy, alembic) through the same pipeline.
    handler = _StdoutHandler()
    handler.setFormatter(
        _Formatter(
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                add_request_context,
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                database_errors,
                redact_processor,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(numeric)
    quiet_http_client_logs()  # no outbound request URL (query values) at INFO (B-3901)
