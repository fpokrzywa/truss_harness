"""Issue #384: a log line of a database error holds its type, SQLSTATE, constraint, table and
statement, and nothing the server said (B-4098, B-4099). Everything is asserted on the rendered
standard output of the product's logging setup, for structlog's and for stdlib loggers."""

from __future__ import annotations

import json
import logging
import re
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any  # Any: parsed log lines, structlog's configuration

import psycopg
import pytest
import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import DBAPIError

from shifde.core.redaction import REDACTED, register_secret
from shifde.observability import dberrors
from shifde.observability.logging import configure_logging, redact_processor
from tests._support.dberrors import (
    BAD_UUID,
    CHECK_VIOLATION,
    ERROR,
    UNIQUE_VIOLATION,
    Failure,
    fail_naming,
    new_marker,
)

log = structlog.get_logger("tests.database_error_values")
each_format = pytest.mark.parametrize("fmt", ["json", "console"])
each_failure = pytest.mark.parametrize(
    "failure", [BAD_UUID, UNIQUE_VIOLATION], ids=["uuid", "unique"]
)
RAISED = "raised by the server"


@contextmanager
def _logging(capsys: pytest.CaptureFixture[str], fmt: str) -> Iterator[Callable[[], str]]:
    """The product's logging setup in `fmt`; what it printed since on standard output."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    config: dict[str, Any] = structlog.get_config()
    configure_logging("INFO", fmt)
    capsys.readouterr()
    try:
        yield lambda: capsys.readouterr().out
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        structlog.configure(**config)


def _caught(action: Callable[[], Any]) -> Exception:
    try:
        action()
    except Exception as exc:
        return exc
    raise AssertionError("nothing was raised")


def _error(engine: Engine, failure: Failure, marker: str) -> DBAPIError:
    named: list[str] = []
    with engine.connect() as c:
        exc = _caught(lambda: fail_naming(c, marker, failure, named))
    assert isinstance(exc, DBAPIError) and any(marker in said for said in named)
    return exc


def _raised(engine: Engine, sqlstate: str, message: str, marker: str) -> DBAPIError:
    """A PL/pgSQL error of `sqlstate` with `message`, and `marker` in its DETAIL and HINT."""
    body = (
        "CREATE FUNCTION pg_temp.refuse(m text, d text) RETURNS void LANGUAGE plpgsql AS $$ "
        f"BEGIN RAISE EXCEPTION '%%', m USING ERRCODE = '{sqlstate}', DETAIL = d, HINT = d; END $$"
    )
    with engine.connect() as c:
        c.exec_driver_sql(body)
        exc = _caught(
            lambda: c.exec_driver_sql("SELECT pg_temp.refuse(%(m)s, %(d)s)",
                                      {"m": message, "d": marker})
        )  # fmt: skip
    assert isinstance(exc, DBAPIError) and isinstance(exc.orig, psycopg.Error)
    diag = exc.orig.diag
    assert exc.orig.sqlstate == sqlstate and diag.message_primary == message
    assert diag.message_detail == marker and diag.message_hint == marker
    return exc


def _shown(capsys: pytest.CaptureFixture[str], exc: BaseException) -> str:
    """The `exception` field of a structlog line; the stdlib line shows the same text, and the
    summary of `exc` as a field and as a `%s` argument is of one line."""
    with _logging(capsys, "json") as out:
        log.error("it.failed", exc_info=exc)
        line = json.loads(out())
        logging.getLogger("uvicorn.error").error("it failed", exc_info=exc)
        foreign = json.loads(out())
        log.error("it.failed", error=exc)
        field = json.loads(out())["error"]
        logging.getLogger("uvicorn.error").error("it failed: %s", exc)
        argument = json.loads(out())["event"]
    assert line["event"] == "it.failed" and foreign["event"] == "it failed"
    assert "exc_info" not in line and "exc_info" not in foreign
    assert foreign["exception"] == line["exception"]
    assert argument == f"it failed: {field}" and "\n" not in field
    shown: str = line["exception"]
    for part in re.findall(r"sqlstate=\w+|connection_status=\w+|constraint=\w+|message=\w+", shown):
        assert part in field
    return f"{shown}\n{field}"


# ------------------------------------------------------------------ the stdlib path (criterion 4)
@each_format
@each_failure
def test_uvicorns_line_of_a_database_error_names_no_value(
    app_engine: Engine, capsys: pytest.CaptureFixture[str], failure: Failure, fmt: str
) -> None:
    marker = new_marker()
    exc = _error(app_engine, failure, marker)
    with _logging(capsys, fmt) as out:
        logging.getLogger("uvicorn.error").error(
            "Exception in ASGI application\n", exc_info=exc
        )  # as uvicorn logs it
        logged = out()
        logging.getLogger("psycopg.pool").warning("error ignored in %s: %s", "rollback", exc)
        argument = out()
        logging.getLogger("psycopg.pool").warning("%(error)s", {"error": exc})
        mapping = out()
        logging.getLogger("psycopg.pool").warning(exc)
        message = out()
    assert "Exception in ASGI application" in logged
    failure.check_log(logged, marker)
    assert "in fail_naming" in logged and "Traceback (most recent call last):" in logged
    assert "error ignored in rollback: " in argument
    for summary in (argument, mapping, message):
        failure.check_log(summary, marker)
        assert "Traceback" not in summary


@each_format
@each_failure
def test_a_structlog_line_of_a_database_error_names_no_value(
    app_engine: Engine, capsys: pytest.CaptureFixture[str], failure: Failure, fmt: str
) -> None:
    marker = new_marker()
    exc = _error(app_engine, failure, marker)
    with _logging(capsys, fmt) as out:
        log.error("it.failed", exc_info=exc)
        logged = out()
        log.error("it.failed", errors={"all": [("first", exc)]})
        nested = out()
    failure.check_log(logged, marker)
    assert "in fail_naming" in logged
    failure.check_log(nested, marker)


def test_the_record_and_the_exception_are_not_changed(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    exc = _error(app_engine, BAD_UUID, marker)
    said, args = str(exc), exc.args
    seen: list[logging.LogRecord] = []

    class Other(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    with _logging(capsys, "json") as out:
        logging.getLogger().addHandler(Other())
        logging.getLogger("uvicorn.error").error("it failed: %s", exc, exc_info=exc)
        assert marker not in out()
    [record] = seen
    assert record.args == (exc,) and record.exc_info is not None and record.exc_info[1] is exc
    assert (str(exc), exc.args) == (said, args) and marker in said


# ------------------------------------------------------------------ chains (criterion 5)
def _cause(exc: Exception) -> None:
    raise RuntimeError(str(exc)) from exc


def _context(exc: Exception) -> None:
    try:
        raise exc
    except type(exc):
        raise RuntimeError(str(exc))  # noqa: B904  # the context, not the cause


def _suppressed(exc: Exception) -> None:
    try:
        raise exc
    except type(exc):
        raise RuntimeError(str(exc)) from None


def _group(exc: Exception) -> None:
    raise ExceptionGroup(str(exc), [ValueError(str(exc)), exc])


def _noted(exc: Exception) -> None:
    exc.add_note(str(exc))
    raise exc


def _orig(exc: Exception) -> None:
    wrapper = RuntimeError(str(exc))
    wrapper.orig = exc  # type: ignore[attr-defined]
    raise wrapper


def _deep(exc: Exception) -> None:
    try:
        raise ExceptionGroup(str(exc), [ExceptionGroup(str(exc), [_caught(lambda: _cause(exc))])])
    except ExceptionGroup as group:
        raise LookupError(str(exc)) from group


@pytest.mark.parametrize(
    "wrap", [_cause, _context, _suppressed, _group, _noted, _orig, _deep], ids=lambda f: f.__name__
)
@pytest.mark.parametrize("raw", [False, True], ids=["sqlalchemy", "psycopg"])
def test_a_wrapped_database_error_names_no_value(
    app_engine: Engine,
    capsys: pytest.CaptureFixture[str],
    wrap: Callable[[Exception], None],
    raw: bool,
) -> None:
    marker = new_marker()
    error: Exception = _error(app_engine, BAD_UUID, marker)
    if raw:
        orig = error.orig  # type: ignore[attr-defined]
        assert isinstance(orig, psycopg.errors.InvalidTextRepresentation)
        error = orig
    exc = _caught(lambda: wrap(error))
    notes = getattr(exc, "__notes__", [])
    assert marker in str(exc) or any(marker in n for n in notes)
    shown = _shown(capsys, exc)
    assert marker not in shown and "invalid input syntax" not in shown
    assert f"in {wrap.__name__}" in shown  # the frames of the outermost exception
    assert type(exc).__qualname__ in shown
    if wrap is not _suppressed:  # a suppressed context is not shown
        rendered = shown.rsplit("\n", 1)[0]
        assert "sqlstate=22P02" in rendered
        assert ("in do_execute" if raw else "in fail_naming") in rendered  # its own frames
        assert "psycopg.errors.InvalidTextRepresentation" in rendered
        assert (BAD_UUID.statements[0] in rendered) is not raw


@each_failure
def test_each_form_of_exc_info_names_no_value(
    app_engine: Engine, capsys: pytest.CaptureFixture[str], failure: Failure
) -> None:
    marker = new_marker()
    with _logging(capsys, "json") as out:
        try:
            raise _error(app_engine, failure, marker)
        except DBAPIError as exc:
            log.error("a", exc_info=True)
            log.error("b", exc_info=exc)
            log.error("c", exc_info=(type(exc), exc, exc.__traceback__))
            logging.getLogger("x").error("d", exc_info=True)
            logging.getLogger("x").error("e", exc_info=exc)
            logging.getLogger("x").error("f", exc_info=(type(exc), exc, exc.__traceback__))
            logging.getLogger("x").exception("g")
            log.exception("h")
        lines = [json.loads(line) for line in out().splitlines()]
    assert [line["event"] for line in lines] == list("abcdefgh")
    assert len({line["exception"] for line in lines}) <= 2  # (line numbers never differ)
    for line in lines:
        failure.check_log(line["exception"], marker)
        assert "exc_info" not in line


def test_a_cyclic_chain_ends(app_engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    marker = new_marker()
    inner = _error(app_engine, BAD_UUID, marker)
    outer = _caught(lambda: _cause(inner))
    assert inner.orig is not None
    inner.orig.__cause__ = outer
    inner.orig.__context__ = outer
    shown = _shown(capsys, outer)
    assert "sqlstate=22P02" in shown and marker not in shown
    cycle = ValueError("visible")
    other = KeyError("other")
    cycle.__context__, other.__context__ = other, cycle
    with _logging(capsys, "json") as out:
        log.error("it.failed", exc_info=cycle)
        assert "visible" in out()


def test_a_long_chain_and_a_cyclic_container_do_not_fail_the_log_call(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    exc: Exception = _error(app_engine, BAD_UUID, marker)
    for _ in range(3000):
        exc = _caught(lambda inner=exc: _cause_named(inner, marker))  # type: ignore[misc]
    cyclic: list[Any] = [exc]
    cyclic.append(cyclic)
    with _logging(capsys, "json") as out:
        log.error("it.failed", exc_info=exc)
        assert "sqlstate=22P02" in (logged := out()) and marker not in logged
        logging.getLogger("x").error("%s", cyclic)
        assert "sqlstate=22P02" in out()


def _cause_named(exc: Exception, marker: str) -> None:
    raise RuntimeError(marker) from exc


# ------------------------------------------------------------------ other codes (criterion 5a)
def test_a_check_violation_names_no_value(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    shown = _shown(capsys, _error(app_engine, CHECK_VIOLATION, marker))
    CHECK_VIOLATION.check_log(shown, marker)


def test_a_raised_exception_names_no_value(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    shown = _shown(capsys, _raised(app_engine, "P0001", f"{RAISED} {marker}", marker))
    assert "sqlstate=P0001" in shown and "pg_temp.refuse(%(m)s, %(d)s)" in shown
    assert marker not in shown and RAISED not in shown


# ------------------------------------------------------------------ the allowlist (criterion 5b)
def test_the_allowlist_holds_one_constant_text() -> None:
    assert dberrors.SAFE_MESSAGES == {"22012": "division by zero"}


def test_a_division_by_zero_is_named(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    with app_engine.connect() as c:
        exc = _caught(lambda: c.execute(text("SELECT CAST(:m AS text), 1 / 0"), {"m": marker}))
    shown = _shown(capsys, exc)
    assert f"{ERROR}: sqlstate=22012, message=division by zero" in shown and marker not in shown


def test_another_text_of_the_allowed_code_is_not_logged(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    shown = _shown(capsys, _raised(app_engine, "22012", f"{RAISED} {marker}", marker))
    assert "sqlstate=22012" in shown and "message=" not in shown
    assert marker not in shown and RAISED not in shown


@pytest.mark.parametrize("around", ["{} now", "now {}", "{} ", " {}", "upper", "{}\n", "{}."])
def test_nearly_the_allowed_text_is_not_logged(
    app_engine: Engine, capsys: pytest.CaptureFixture[str], around: str
) -> None:
    safe = dberrors.SAFE_MESSAGES["22012"]
    message = safe.upper() if around == "upper" else around.format(safe)
    shown = _shown(capsys, _raised(app_engine, "22012", message, new_marker()))
    assert "sqlstate=22012" in shown and "message=" not in shown
    assert safe not in shown.lower()


def test_exactly_the_allowed_text_is_the_constant_without_detail_and_hint(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    exc = _raised(app_engine, "22012", dberrors.SAFE_MESSAGES["22012"], marker)
    shown = _shown(capsys, exc)
    assert "sqlstate=22012, message=division by zero" in shown and marker not in shown


def test_the_allowed_text_under_another_code_is_not_logged(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    safe = dberrors.SAFE_MESSAGES["22012"]
    shown = _shown(capsys, _raised(app_engine, "P0001", safe, new_marker()))
    assert "sqlstate=P0001" in shown and safe not in shown


# ------------------------------------------------------------------ no SQLSTATE (criterion 5c)
def test_a_refused_connection_names_no_host_or_user(capsys: pytest.CaptureFixture[str]) -> None:
    marker = new_marker()
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    info = psycopg.conninfo.make_conninfo(
        host="127.0.0.1", port=port, user=marker, dbname=marker, connect_timeout=2
    )
    raw = _caught(lambda: psycopg.connect(info))
    url = URL.create(
        "postgresql+psycopg", marker, host="127.0.0.1", port=port, database=marker
    ).update_query_dict({"connect_timeout": "2"})
    engine = create_engine(url)
    wrapped = _caught(engine.connect)
    engine.dispose()
    # Windows may drop the attempt instead of refusing it: psycopg's timeout, without a connection
    timeout = isinstance(raw, psycopg.errors.ConnectionTimeout)
    assert isinstance(raw, psycopg.OperationalError) and (raw.pgconn is None) is timeout
    assert timeout or str(port) in str(raw)
    driver = "psycopg.errors.ConnectionTimeout" if timeout else "psycopg.OperationalError"
    for exc, names in ((raw, [driver]), (wrapped, [driver, "sqlalchemy.exc.OperationalError"])):
        assert str(raw) in str(exc)
        shown = _shown(capsys, exc)
        assert all(name in shown for name in names) and "sqlstate=" not in shown
        assert ("connection_status=BAD" in shown) is not timeout
        for absent in (marker, "127.0.0.1", str(port), str(raw), "refused", "expired"):
            assert absent not in shown, absent


def test_a_connection_the_server_refuses_shows_its_status(
    app_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    url = make_url(app_url)
    info = psycopg.conninfo.make_conninfo(
        host=url.host, port=url.port, user=url.username, password=url.password, dbname=marker
    )
    exc = _caught(lambda: psycopg.connect(info))
    assert isinstance(exc, psycopg.OperationalError) and marker in str(exc)
    assert exc.sqlstate is None and exc.pgconn is not None
    assert exc.pgconn.status == psycopg.pq.ConnStatus.BAD
    shown = _shown(capsys, exc)
    assert "psycopg.OperationalError: connection_status=BAD" in shown
    for absent in (marker, str(url.host), str(url.port), str(url.username), "does not exist"):
        assert absent not in shown, absent


def test_a_connection_timeout_shows_its_type_and_no_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = new_marker()
    with socket.create_server(("127.0.0.1", 0), backlog=8) as silent:
        port = silent.getsockname()[1]
        info = psycopg.conninfo.make_conninfo(
            host="127.0.0.1", port=port, user=marker, dbname=marker, connect_timeout=2
        )
        exc = _caught(lambda: psycopg.connect(info))
    assert isinstance(exc, psycopg.errors.ConnectionTimeout) and exc.pgconn is None
    shown = _shown(capsys, exc)
    assert "psycopg.errors.ConnectionTimeout" in shown and "connection_status" not in shown
    for absent in (marker, "127.0.0.1", str(port), "expired"):
        assert absent not in shown, absent


def test_a_client_side_error_names_no_value(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = new_marker()
    with app_engine.connect() as c:
        exc = _caught(lambda: c.execute(text("SELECT CAST(:m AS text)"), {"m": f"{marker}\x00"}))
    assert "NUL" in str(exc)
    shown = _shown(capsys, exc)
    assert ERROR in shown and "psycopg.DataError" in shown
    assert "sqlstate=" not in shown and "connection_status" not in shown
    assert marker not in shown and "NUL" not in shown


class _Unreadable(psycopg.errors.UniqueViolation):
    @property
    def diag(self) -> Any:
        raise RuntimeError("no diag")


class _Unconnected(psycopg.OperationalError):
    @property
    def pgconn(self) -> Any:
        raise RuntimeError("no pgconn")


class _Closed:
    @property
    def status(self) -> Any:
        raise RuntimeError("no status")


def test_a_database_error_that_cannot_be_read_is_still_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = new_marker()
    shown = _shown(capsys, _Unreadable(marker))
    assert "_Unreadable: sqlstate=23505" in shown and "constraint=" not in shown
    closed: Any = _Closed()
    for exc in (_Unconnected(marker), psycopg.OperationalError(marker, pgconn=closed)):
        shown = _shown(capsys, exc)
        assert type(exc).__qualname__ in shown and "connection_status" not in shown
        assert marker not in shown


# ------------------------------------------------------------------ controls (criterion 6)
def _plain(fmt: str) -> logging.Formatter:
    """The stdlib formatter without the database error handling."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.add_log_level],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_processor,
            renderer,
        ],
    )


@each_format
def test_an_exception_without_a_database_error_is_rendered_as_before(
    capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    def fail() -> None:
        try:
            raise KeyError("inner")
        except KeyError as exc:
            raise ValueError("visible") from exc

    exc = _caught(fail)
    # (a structlog line) exactly what `format_exc_info` makes of it
    expected = structlog.processors.format_exc_info(None, "", {"exc_info": exc})["exception"]
    with _logging(capsys, fmt) as out:
        log.error("it.failed", exc_info=exc, error=exc)
        logged = out()
        if fmt == "json":
            line = json.loads(logged)
            assert line["exception"] == expected and line["error"] == "ValueError('visible')"
        else:
            assert expected in logged and "visible" in logged
        # (a stdlib record) the same event as without the change
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "it failed: %s", (exc,), (type(exc), exc, None)
        )
        [handler] = logging.getLogger().handlers
        assert handler.formatter is not None
        now = handler.formatter.format(record)
        now = re.sub(r'("timestamp": "[^"]*", |, "timestamp": "[^"]*")|^\S+Z ', "", now)
        assert now == _plain(fmt).format(record)
        assert "it failed: visible" in now


def test_a_registered_secret_in_the_statement_is_masked(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    marker, secret = new_marker(), new_marker()
    two_lines = f"{new_marker()}\n{new_marker()}"
    register_secret(secret)
    register_secret(two_lines)
    statement = f"SELECT '{secret}', '{two_lines}', CAST(CAST(:m AS text) AS uuid)"
    with app_engine.connect() as c:
        exc = _caught(lambda: c.execute(text(statement), {"m": marker}))
    shown = _shown(capsys, exc)
    assert "sqlstate=22P02" in shown and REDACTED in shown and "AS uuid)" in shown
    for absent in (marker, secret, *two_lines.split("\n")):
        assert absent not in shown


def test_no_exception_of_a_chain_is_turned_into_text(
    app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    class Loud(Exception):
        def __str__(self) -> str:
            raise AssertionError("str() was called")

        __repr__ = __str__

    marker = new_marker()
    error = _error(app_engine, BAD_UUID, marker)

    def fail() -> None:
        raise Loud(marker) from error

    exc = _caught(fail)
    shown = _shown(capsys, exc)
    assert "Loud" in shown and "sqlstate=22P02" in shown and marker not in shown
