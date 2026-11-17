"""`shifde-admin dev-rest-sink` (SETH 3ba0e06 `cli/main.py:780-811`, ported unchanged, B-2443).

SETH has no test for it. Behaviour: development only (anything else exits 1 and starts no
server); in development it serves `RestTestServer` with the dev bearer token on 127.0.0.1, echoes
each request's method and path (never a header or a body) and stops the server on Ctrl-C."""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from shifde.adapters import testservers
from shifde.cli import main as cli_main
from shifde.core.settings import Settings

TOKEN = "sink-test-token"  # a test value, not the dev token (which is never read here)


def test_dev_rest_sink_refuses_outside_development(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = settings.model_copy(update={"env": "production"})
    monkeypatch.setattr(cli_main, "_settings", lambda: production)
    started: list[str] = []
    monkeypatch.setattr(testservers.RestTestServer, "start", lambda self: started.append("x"))
    monkeypatch.setattr("shifde.cli.sample_pipeline.dev_rest_token", lambda: TOKEN)
    result = CliRunner().invoke(cli_main.app, ["dev-rest-sink"])
    assert result.exit_code == 1
    assert "dev-rest-sink refuses to run unless SHIFDE_ENV=development" in result.output
    assert started == []


def test_dev_rest_sink_echoes_method_and_path_and_stops_on_ctrl_c(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    development = settings.model_copy(update={"env": "development"})
    assert development.is_development
    monkeypatch.setattr(cli_main, "_settings", lambda: development)
    monkeypatch.setattr("shifde.cli.sample_pipeline.dev_rest_token", lambda: TOKEN)
    servers: list[testservers.RestTestServer] = []
    real_start = testservers.RestTestServer.start

    def start(self: testservers.RestTestServer) -> testservers.RestTestServer:
        servers.append(self)
        return real_start(self)

    monkeypatch.setattr(testservers.RestTestServer, "start", start)
    ticks = iter([False, True])

    def sleep(_seconds: float) -> None:
        if next(ticks):
            raise KeyboardInterrupt
        # the first tick: one authorized request reaches the sink before the echo loop reads it
        (server,) = servers
        r = httpx.post(
            f"{server.base_url}/items?q=secret-value",
            json={"body": "secret-body"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code != 401, r.text  # authorized (an unknown path still is recorded)

    monkeypatch.setattr("time.sleep", sleep)
    result = CliRunner().invoke(cli_main.app, ["dev-rest-sink", "--port", "0"])
    assert result.exit_code == 0, result.output
    assert "REST sink listening on http://127.0.0.1:0 (Ctrl-C to stop)" in result.output
    assert "  POST /items" in result.output
    assert TOKEN not in result.output and "secret-body" not in result.output
    assert "secret-value" not in result.output
    (server,) = servers
    with pytest.raises(httpx.HTTPError):  # stopped
        httpx.get(f"{server.base_url}/items", timeout=1)
