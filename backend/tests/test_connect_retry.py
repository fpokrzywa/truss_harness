"""The test harness's connect retry (issue #238, B-3231): only WinError 10055/10048 at connect,
at most 3 attempts with 0.2 s and 0.5 s backoff, one WARNING per retry. No database, no sleep."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from tests._support import connect_retry

E10055 = (
    'connection is bad: connection to server at "127.0.0.1", port 55450 failed: '
    "No buffer space available (0x00002747/10055)"
)
E10048 = (
    'connection is bad: connection to server at "127.0.0.1", port 55450 failed: '
    "Only one usage of each socket address is normally permitted (0x00002740/10048)"
)
SENTINEL = object()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(connect_retry, "_sleep", slept.append)
    return slept


def _fake(outcomes: list[BaseException | object]) -> tuple[Callable[..., Any], list[Any]]:
    calls: list[Any] = []

    def connect(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return connect, calls


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "tests.connect_retry" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(("message", "code"), [(E10055, "10055"), (E10048, "10048")])
def test_a_socket_error_at_connect_is_retried_and_then_succeeds(
    message: str, code: str, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    connect, calls = _fake([psycopg.OperationalError(message), SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    result = connect_retry.retrying(connect)("postgresql://x", autocommit=True)

    assert result is SENTINEL
    assert len(calls) == 2
    assert calls[0] == calls[1] == (("postgresql://x",), {"autocommit": True})
    assert sleeps == [0.2]
    records = _warnings(caplog)
    assert len(records) == 1
    assert code in records[0].getMessage()
    assert "attempt 1 of 3" in records[0].getMessage()


@pytest.mark.parametrize(
    "message",
    [
        'connection to server at "127.0.0.1" failed: connection refused (0x0000274D/10061)',
        'FATAL:  password authentication failed for user "shifde_app"',
    ],
)
def test_another_operational_error_is_not_retried(
    message: str, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    error = psycopg.OperationalError(message)
    connect, calls = _fake([error, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    with pytest.raises(psycopg.OperationalError) as raised:
        connect_retry.retrying(connect)()

    assert raised.value is error
    assert len(calls) == 1
    assert sleeps == []
    assert _warnings(caplog) == []


def test_another_exception_type_is_not_retried(sleeps: list[float]) -> None:
    error = ValueError(E10055)
    connect, calls = _fake([error, SENTINEL])

    with pytest.raises(ValueError) as raised:
        connect_retry.retrying(connect)()

    assert raised.value is error
    assert len(calls) == 1
    assert sleeps == []


def test_three_socket_errors_re_raise_the_third_after_three_calls(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    errors = [psycopg.OperationalError(E10055) for _ in range(3)]
    connect, calls = _fake([*errors, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    with pytest.raises(psycopg.OperationalError) as raised:
        connect_retry.retrying(connect)()

    assert raised.value is errors[2]
    assert len(calls) == 3
    assert sleeps == [0.2, 0.5]
    records = _warnings(caplog)
    assert len(records) == 2
    assert "attempt 1 of 3" in records[0].getMessage()
    assert "attempt 2 of 3" in records[1].getMessage()


def test_the_wrapper_is_installed_once_for_the_session() -> None:
    installed = psycopg.connect
    assert getattr(installed, "_connect_retry", False) is True
    assert installed.__wrapped__ == psycopg.Connection.connect  # type: ignore[attr-defined]

    connect_retry.install()

    assert psycopg.connect is installed
