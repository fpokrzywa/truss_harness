"""The test harness's TestClient entry retry (issue #253, B-3321): only an `OSError` with winerror
10055/10048, at most 3 attempts with 0.2 s and 0.5 s backoff, one WARNING per retry. No database,
no sleep."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import starlette.testclient
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._support import connect_retry

SENTINEL = object()


def _winerror(code: int | None) -> OSError:
    # Set as an attribute so the error is built the same way on Linux and on Windows.
    error = OSError("socket pair failed")
    if code is not None:
        error.winerror = code  # type: ignore[attr-defined,unused-ignore]
    return error


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(connect_retry, "_sleep", slept.append)
    return slept


def _fake(outcomes: list[BaseException | object]) -> tuple[Callable[..., Any], list[Any]]:
    calls: list[Any] = []

    def enter(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return enter, calls


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "tests.connect_retry" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize("code", [10055, 10048])
def test_a_socket_error_at_entry_is_retried_and_then_succeeds(
    code: int, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    enter, calls = _fake([_winerror(code), SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")
    client = object()

    result = connect_retry.retrying_enter(enter)(client)

    assert result is SENTINEL
    assert len(calls) == 2
    assert calls[0] == calls[1] == ((client,), {})
    assert sleeps == [0.2]
    records = _warnings(caplog)
    assert len(records) == 1
    assert str(code) in records[0].getMessage()
    assert "attempt 1 of 3" in records[0].getMessage()


@pytest.mark.parametrize("code", [10061, None])
def test_another_os_error_is_not_retried(
    code: int | None, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    error = _winerror(code)
    enter, calls = _fake([error, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    with pytest.raises(OSError) as raised:
        connect_retry.retrying_enter(enter)(object())

    assert raised.value is error
    assert len(calls) == 1
    assert sleeps == []
    assert _warnings(caplog) == []


def test_three_socket_errors_re_raise_the_third_after_three_calls(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    errors = [_winerror(10055) for _ in range(3)]
    enter, calls = _fake([*errors, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    with pytest.raises(OSError) as raised:
        connect_retry.retrying_enter(enter)(object())

    assert raised.value is errors[2]
    assert len(calls) == 3
    assert sleeps == [0.2, 0.5]
    records = _warnings(caplog)
    assert len(records) == 2
    assert "attempt 1 of 3" in records[0].getMessage()
    assert "attempt 2 of 3" in records[1].getMessage()


def test_the_entry_wrapper_is_installed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = starlette.testclient.TestClient
    # Restores the class attribute after the test, whatever install does to it.
    monkeypatch.setattr(client, "__enter__", client.__enter__)

    connect_retry.install_testclient()
    installed = client.__enter__
    assert getattr(installed, "_connect_retry", False) is True
    assert TestClient.__enter__ is installed

    connect_retry.install_testclient()

    assert client.__enter__ is installed


def test_a_real_client_enters_serves_and_exits_with_the_wrapper() -> None:
    connect_retry.install_testclient()
    assert getattr(TestClient.__enter__, "_connect_retry", False) is True
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ping": "pong"}

    with TestClient(app) as client:
        response = client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"ping": "pong"}


class _Client:
    portal: Any = None


def _fake_portal(
    outcomes: list[BaseException | object],
) -> tuple[Callable[[Any], contextlib.AbstractContextManager[Any]], list[Any], list[Any]]:
    calls: list[Any] = []
    exits: list[Any] = []

    @contextlib.contextmanager
    def portal_factory(client: Any) -> Iterator[Any]:
        calls.append(client)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        try:
            yield outcome
        finally:
            exits.append(outcome)

    return portal_factory, calls, exits


@pytest.mark.parametrize("code", [10055, 10048])
def test_a_socket_error_at_the_portal_start_is_retried_and_then_succeeds(
    code: int, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    factory, calls, exits = _fake_portal([_winerror(code), SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")
    client = _Client()

    with connect_retry.retrying_portal(factory)(client) as portal:
        assert portal is SENTINEL

    assert calls == [client, client]
    assert exits == [SENTINEL]
    assert sleeps == [0.2]
    records = _warnings(caplog)
    assert len(records) == 1
    assert str(code) in records[0].getMessage()
    assert "attempt 1 of 3" in records[0].getMessage()


@pytest.mark.parametrize("code", [10061, None])
def test_another_os_error_at_the_portal_start_is_not_retried(
    code: int | None, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    error = _winerror(code)
    factory, calls, _ = _fake_portal([error, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    with pytest.raises(OSError) as raised, connect_retry.retrying_portal(factory)(_Client()):
        pass

    assert raised.value is error
    assert len(calls) == 1
    assert sleeps == []
    assert _warnings(caplog) == []


def test_three_socket_errors_at_the_portal_start_re_raise_the_third(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    errors = [_winerror(10048) for _ in range(3)]
    factory, calls, _ = _fake_portal([*errors, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")

    with pytest.raises(OSError) as raised, connect_retry.retrying_portal(factory)(_Client()):
        pass

    assert raised.value is errors[2]
    assert len(calls) == 3
    assert sleeps == [0.2, 0.5]
    records = _warnings(caplog)
    assert len(records) == 2
    assert "attempt 1 of 3" in records[0].getMessage()
    assert "attempt 2 of 3" in records[1].getMessage()


def test_an_error_inside_the_request_is_not_retried(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    factory, calls, exits = _fake_portal([SENTINEL, SENTINEL])
    caplog.set_level(logging.WARNING, logger="tests.connect_retry")
    error = _winerror(10055)

    with pytest.raises(OSError) as raised, connect_retry.retrying_portal(factory)(_Client()):
        raise error

    assert raised.value is error
    assert len(calls) == 1
    assert exits == [SENTINEL]
    assert sleeps == []
    assert _warnings(caplog) == []


def test_an_entered_client_reuses_its_portal() -> None:
    factory, calls, _ = _fake_portal([SENTINEL])
    client = _Client()
    client.portal = entered = object()

    with connect_retry.retrying_portal(factory)(client) as portal:
        assert portal is entered

    assert calls == []


def test_the_portal_wrapper_is_installed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = starlette.testclient.TestClient
    # Restores the class attribute after the test, whatever install does to it.
    monkeypatch.setattr(client, "_portal_factory", client._portal_factory)

    connect_retry.install_testclient_portal()
    installed = client._portal_factory
    assert getattr(installed, "_connect_retry", False) is True
    assert TestClient._portal_factory is installed

    connect_retry.install_testclient_portal()

    assert client._portal_factory is installed


def test_a_real_unentered_client_serves_a_request_with_the_wrapper() -> None:
    connect_retry.install_testclient_portal()
    assert getattr(TestClient._portal_factory, "_connect_retry", False) is True
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ping": "pong"}

    response = TestClient(app).get("/ping")

    assert response.status_code == 200
    assert response.json() == {"ping": "pong"}
