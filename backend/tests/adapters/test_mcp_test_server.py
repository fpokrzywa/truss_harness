"""McpTestServer leaves the process-wide uvicorn loggers as it found them (issue #384): under the
product's logging setup a `uvicorn.error` line still goes through the product's filter. And no
module of the API or the worker starts a local test server (B-4098)."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any  # Any: the ASGI messages, structlog's configuration

import pytest
import structlog
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

import shifde
from shifde.adapters.conformance import connection, exec_ctx, loopback_guard
from shifde.adapters.mcp_http import McpHttpAdapter
from shifde.adapters.testservers import McpTestServer
from shifde.observability.logging import configure_logging
from tests._support.dberrors import UNIQUE_VIOLATION, fail_naming, new_marker

TOKEN = "mcp-logger-token-0004"
NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
SRC = Path(shifde.__file__).parent
BACKEND = SRC.parents[1]

type State = list[tuple[str, bool, list[logging.Handler], int]]


def _state() -> State:
    loggers = [logging.getLogger(name) for name in NAMES]
    return [(x.name, x.propagate, x.handlers[:], x.level) for x in loggers]


def _put(state: State) -> None:
    for name, propagate, handlers, level in state:
        logger = logging.getLogger(name)
        logger.propagate, logger.handlers[:] = propagate, handlers
        logger.setLevel(level)


@pytest.fixture
def uvicorn_loggers() -> Iterator[None]:
    """The uvicorn loggers as Python makes them, put back afterwards."""
    before = _state()
    _put([(name, True, [], logging.NOTSET) for name in NAMES])
    try:
        yield
    finally:
        _put(before)


@contextmanager
def _product_logging(capsys: pytest.CaptureFixture[str]) -> Iterator[Callable[[], str]]:
    """The product's logging setup; what was printed since, on both streams."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    config: dict[str, Any] = structlog.get_config()
    configure_logging("INFO", "json")
    capsys.readouterr()

    def printed() -> str:
        captured = capsys.readouterr()
        return captured.out + captured.err

    try:
        yield printed
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        structlog.configure(**config)


def _use(server: McpTestServer) -> None:
    ctx = exec_ctx(loopback_guard(timeout_seconds=15.0))
    target = connection("mcp_http", {"url": server.url}, auth_method="bearer", secret=TOKEN)
    assert McpHttpAdapter().test(ctx, target).passed


# ------------------------------------------------------------------------------- the loggers
@pytest.mark.parametrize("kept", ["default", "configured"])
def test_the_server_changes_no_uvicorn_logger(uvicorn_loggers: None, kept: str) -> None:
    if kept == "configured":  # no value is a default or one that uvicorn sets
        _put([(n, n == "uvicorn.access", [logging.NullHandler()], 17) for n in NAMES])
    before = _state()
    with McpTestServer(token=TOKEN) as server:
        assert _state() == before
        _use(server)
        assert _state() == before
    assert _state() == before


def test_the_server_is_quiet_in_a_process_without_a_logging_setup(
    uvicorn_loggers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """As in `shifde-admin adapters verify`: a logger at its default level prints no INFO line."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers[:] = []
    root.setLevel(logging.WARNING)  # Python's default
    try:
        with McpTestServer(token=TOKEN) as server:
            _use(server)
        captured = capsys.readouterr()
        assert root.level == logging.WARNING
        assert "INFO" not in captured.out + captured.err
        logging.getLogger("uvicorn.error").warning("it is shown")
        assert "it is shown" in capsys.readouterr().err  # not vacuous
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.mark.parametrize("when", ["running", "stopped"])
def test_a_uvicorn_error_line_still_goes_through_the_products_filter(
    uvicorn_loggers: None, app_engine: Engine, capsys: pytest.CaptureFixture[str], when: str
) -> None:
    marker = new_marker()
    with app_engine.connect() as c, pytest.raises(DBAPIError) as caught:
        fail_naming(c, marker, UNIQUE_VIOLATION, [])
    exc = caught.value
    assert marker in str(exc)
    with _product_logging(capsys) as printed:
        server = McpTestServer(token=TOKEN).start()
        try:
            _use(server)
            if when == "running":
                logging.getLogger("uvicorn.error").error("it failed", exc_info=exc)
        finally:
            server.stop()
        if when == "stopped":
            logging.getLogger("uvicorn.error").error("it failed", exc_info=exc)
        shown = printed()
    lines = [json.loads(line) for line in shown.splitlines()]
    (line,) = [x for x in lines if x["event"] == "it failed"]
    assert "sqlstate=23505" in line["exception"]
    assert marker not in shown


def test_a_request_that_fails_in_the_server_names_no_value(
    uvicorn_loggers: None, app_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """uvicorn's own line of a request whose application raises a database error."""
    marker = new_marker()

    class Failing(McpTestServer):
        def _build_app(self) -> Callable[..., Any]:
            app = super()._build_app()

            async def failing(scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] == "http":
                    with app_engine.connect() as c:
                        fail_naming(c, marker, UNIQUE_VIOLATION, [])
                await app(scope, receive, send)

            return failing

    with _product_logging(capsys) as printed:
        with Failing() as server:
            ctx = exec_ctx(loopback_guard(timeout_seconds=15.0))
            target = connection("mcp_http", {"url": server.url})
            assert not McpHttpAdapter().test(ctx, target).passed
        shown = printed()
    failed = [x for x in map(json.loads, shown.splitlines()) if "ASGI application" in x["event"]]
    assert failed and all("sqlstate=23505" in x["exception"] for x in failed)
    assert marker not in shown


# ---------------------------------------------------------------- who may start such a server
STARTERS = {  # in `src`, the only modules that name what starts a local test server
    "testservers": {"adapters/suites.py", "cli/main.py", "cli/sample_pipeline.py"},
    "McpTestServer": {"adapters/suites.py", "adapters/testservers.py"},
    "run_suite": {"adapters/conformance.py", "cli/main.py"},
    "suite_for": {"adapters/registry.py", "cli/main.py"},
    "SUITES": {"adapters/registry.py", "adapters/suites.py"},
}
WORDS = {*STARTERS, "shifde.adapters.suites", "shifde.cli"}


def _words(node: ast.AST) -> set[str]:
    """The names a node uses: identifiers, attributes, imports and text that is only a path."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.FunctionDef | ast.ClassDef):
        return {node.name}
    paths = []
    if isinstance(node, ast.Import):
        paths = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        assert node.level == 0, "relative imports are not resolved here"
        paths = [f"{node.module}.{alias.name}" for alias in node.names]
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        paths = [node.value.replace(":", ".")]
    parts = [path.split(".") for path in paths]
    prefixes = {".".join(p[:n]) for p in parts for n in range(1, len(p) + 1)}
    return prefixes | {part for p in parts for part in p}


def _users(root: Path) -> dict[str, set[str]]:
    users: dict[str, set[str]] = {word: set() for word in WORDS}
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            for word in _words(node) & WORDS:
                users[word].add(path.relative_to(root).as_posix())
    return users


def test_only_the_admin_command_starts_a_local_test_server() -> None:
    users = _users(SRC)
    for word, named_by in STARTERS.items():
        assert users[word] == named_by, word
    assert users["shifde.adapters.suites"] == {"adapters/registry.py"}
    assert users["shifde.cli"]  # not vacuous
    assert all(path.startswith("cli/") for path in users["shifde.cli"])


def test_every_uvicorn_server_of_the_repository_is_given_no_logging_setup() -> None:
    """Each `uvicorn.run` and `uvicorn.Config` in Python code passes `log_config=None`."""
    found: set[str] = set()
    for root in (BACKEND / "src", BACKEND / "tests", BACKEND.parent / "scripts"):
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "uvicorn" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] != "uvicorn", path
                if isinstance(node, ast.Call) and ast.unparse(node.func) in (
                    "uvicorn.run",
                    "uvicorn.Config",
                ):
                    given = [ast.unparse(k.value) for k in node.keywords if k.arg == "log_config"]
                    assert given == ["None"], path
                    found.add(path.relative_to(BACKEND.parent).as_posix())
    assert found == {"backend/src/shifde/main.py", "backend/src/shifde/adapters/testservers.py"}
