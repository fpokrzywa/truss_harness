"""`shifde-api` shutdown grace (SP4 design 6.2): `run()` passes `SHIFDE_API_SHUTDOWN_GRACE_SECONDS`
to uvicorn as `timeout_graceful_shutdown` and keeps every other argument."""

from __future__ import annotations

import signal
import sys
from typing import Any

import pytest

from shifde import main
from shifde.core.settings import Settings

APP = object()


def _settings(**update: Any) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url="postgresql+psycopg://shifde_app@127.0.0.1:1/unused",
        **update,
    )


def _run(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(main, "settings_or_exit", lambda: settings)
    monkeypatch.setattr(main, "refuse_unsafe_role", lambda settings: None)
    monkeypatch.setattr(main, "create_app", lambda settings: APP)
    monkeypatch.setattr(main.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    main.run()
    return calls


@pytest.mark.parametrize(("update", "grace"), [({}, 30), ({"api_shutdown_grace_seconds": 7}, 7)])
def test_run_passes_the_shutdown_grace_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, Any], grace: int
) -> None:
    settings = _settings(**update)
    assert _run(monkeypatch, settings) == [
        (
            APP,
            {
                "host": settings.api_host,
                "port": settings.api_port,
                "proxy_headers": False,
                "log_config": None,
                "access_log": False,
                "timeout_graceful_shutdown": grace,
            },
        )
    ]


def test_a_shutdown_grace_below_one_second_is_refused() -> None:
    with pytest.raises(ValueError, match="api_shutdown_grace_seconds"):
        _settings(api_shutdown_grace_seconds=0)


_RERAISED = [signal.SIGTERM]
if sys.platform == "win32":
    _RERAISED.append(signal.SIGBREAK)


@pytest.mark.parametrize("sig", _RERAISED, ids=lambda sig: signal.Signals(sig).name)
def test_a_stop_signal_reraised_after_the_graceful_shutdown_exits_0(
    monkeypatch: pytest.MonkeyPatch, sig: int
) -> None:
    """uvicorn re-raises the stop signal it caught once it has drained; under the default handler
    that ends the process with 3 on Windows (SIGBREAK), the unsafe-role code (issue #185)."""
    monkeypatch.setattr(main, "settings_or_exit", lambda: _settings())
    monkeypatch.setattr(main, "refuse_unsafe_role", lambda settings: None)
    monkeypatch.setattr(main, "create_app", lambda settings: APP)
    monkeypatch.setattr(main.uvicorn, "run", lambda app, **kw: signal.raise_signal(sig))
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        main.run()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert {s: signal.getsignal(s) for s in before} == before
