"""Connect retry on WinError 10055/10048 for the test process (issue #238, DECISIONS B-3231).

`install()` replaces the module attribute `psycopg.connect`, which direct callers and every
SQLAlchemy psycopg engine look up at connect time. Only the connect call is retried, and only on
an `OperationalError` whose message carries one of the two socket errors; each retry is logged at
WARNING on `tests.connect_retry` so the rate stays visible in pytest output.

`install_testclient()` does the same for `starlette.testclient.TestClient.__enter__` (issue #253,
DECISIONS B-3321), where the event loop's socket pair can fail with the same two errors; only an
`OSError` whose `winerror` is 10055 or 10048 is retried.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import psycopg
import starlette.testclient

logger = logging.getLogger("tests.connect_retry")

# libpq prints the Windows socket error as `(0x00002747/10055)` or `(0x00002740/10048)`.
_WINERROR = re.compile(r"/(10055|10048)\)")
_WINERROR_CODES = (10055, 10048)
BACKOFF_SECONDS = (0.2, 0.5)  # before the second and the third attempt
ATTEMPTS = len(BACKOFF_SECONDS) + 1
_sleep = time.sleep


def retrying(connect: Callable[..., Any]) -> Callable[..., Any]:
    """`connect` with up to `ATTEMPTS` tries on a WinError 10055/10048 connect failure."""

    def connect_with_retry(*args: Any, **kwargs: Any) -> Any:
        attempt = 1
        while True:
            try:
                return connect(*args, **kwargs)
            except psycopg.OperationalError as error:
                match = _WINERROR.search(str(error))
                if match is None or attempt >= ATTEMPTS:
                    raise
                delay = BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "database connect failed with WinError %s (attempt %d of %d), retrying in %ss",
                    match.group(1),
                    attempt,
                    ATTEMPTS,
                    delay,
                )
                _sleep(delay)
                attempt += 1

    connect_with_retry.__wrapped__ = connect  # type: ignore[attr-defined]
    connect_with_retry._connect_retry = True  # type: ignore[attr-defined]
    return connect_with_retry


def install() -> None:
    """Point `psycopg.connect` at the retrying wrapper, once."""
    if getattr(psycopg.connect, "_connect_retry", False):
        return
    psycopg.connect = retrying(psycopg.connect)


def retrying_enter(enter: Callable[..., Any]) -> Callable[..., Any]:
    """`enter` with up to `ATTEMPTS` tries on an `OSError` with winerror 10055/10048."""

    def enter_with_retry(*args: Any, **kwargs: Any) -> Any:
        attempt = 1
        while True:
            try:
                return enter(*args, **kwargs)
            except OSError as error:
                code = getattr(error, "winerror", None)
                if code not in _WINERROR_CODES or attempt >= ATTEMPTS:
                    raise
                delay = BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "TestClient start failed with WinError %s (attempt %d of %d), retrying in %ss",
                    code,
                    attempt,
                    ATTEMPTS,
                    delay,
                )
                _sleep(delay)
                attempt += 1

    enter_with_retry.__wrapped__ = enter  # type: ignore[attr-defined]
    enter_with_retry._connect_retry = True  # type: ignore[attr-defined]
    return enter_with_retry


def install_testclient() -> None:
    """Point `TestClient.__enter__` (starlette's, which FastAPI re-exports) at the wrapper, once."""
    client = starlette.testclient.TestClient
    if getattr(client.__enter__, "_connect_retry", False):
        return
    client.__enter__ = retrying_enter(client.__enter__)  # type: ignore[method-assign]
