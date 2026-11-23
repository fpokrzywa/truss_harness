"""B-3901: httpx logs every request's URL, query values included, at INFO; the product's logging
setup raises the HTTP clients' loggers to WARNING, so no outbound request URL reaches the logs."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest

from shifde.core.httplogs import HTTP_CLIENT_LOGGERS
from shifde.core.ids import uuid7
from shifde.observability.logging import configure_logging


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def records() -> Iterator[_Records]:
    """Every record reaching the root logger after `configure_logging`; the root handlers and
    levels and the HTTP client loggers' levels are restored afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS}
    configure_logging("INFO", "json")
    out = _Records()
    root.addHandler(out)
    try:
        yield out
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, value in levels.items():
            logging.getLogger(name).setLevel(value)


def _get(url: str) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        assert c.get(url).status_code == 200


def test_after_the_logging_setup_no_request_url_is_logged_at_info(records: _Records) -> None:
    marker = f"marker-{uuid7().hex}"
    _get(f"https://upstream.example/echo?q={marker}")
    assert not any(marker in m for m in records.messages)
    for name in HTTP_CLIENT_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    # not vacuous: with the httpx logger left at the root's INFO, the URL is logged
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    _get(f"https://upstream.example/echo?q={marker}")
    assert any(marker in m for m in records.messages)
