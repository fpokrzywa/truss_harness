"""The Databricks REST gateway and pinned CLI, with a mock transport and a fake subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any  # Any: subprocess.run keyword arguments
from uuid import uuid4

import httpx
import pytest

import shifde.adapters.native.databricks.clients as clients
from shifde.adapters.native.base import ReadContext
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.databricks import LIVE_VALIDATED, MATURITY
from shifde.adapters.native.databricks.adapter import DatabricksAdapter
from shifde.adapters.native.databricks.clients import DatabricksCLI, DatabricksWorkspaceGateway
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.adapters.native.databricks.source import CLI_VERSION

HOST = "https://workspace.example"


def _target() -> Target:
    return Target(tenant_id=uuid4(), host=HOST, bindings={"warehouse_id": "wh"})


def _gateway(handler: Any) -> DatabricksWorkspaceGateway:
    return DatabricksWorkspaceGateway(
        _target(), token_provider=lambda host: "tok", transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com",
        "http://app.databricksapps.com",
        "https://app.databricksapps.com/path",
        "https://user:pw@app.databricksapps.com",
        "https://app.databricksapps.com:8443",
        "https://app.databricksapps.com?q=1",
    ],
)
def test_health_check_refuses_unrecognized_origins_without_a_request(url: str) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"status": "ok", "warehouse": "ok"})

    with pytest.raises(ValueError, match="origin"):
        _gateway(handler).health_check(url)
    assert sent == []


def test_health_check_sends_the_bearer_token_to_the_app_origin() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"status": "ok", "warehouse": "ok"})

    assert _gateway(handler).health_check("https://app.databricksapps.com") == {
        "status": "ok",
        "warehouse": "ok",
    }
    assert str(sent[0].url) == "https://app.databricksapps.com/health"
    assert sent[0].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com",
        "http://app.databricksapps.com",
        "https://app.databricksapps.com/path",
        "https://user:pw@app.databricksapps.com",
        "https://app.databricksapps.com:8443",
        "https://app.databricksapps.com?q=1",
    ],
)
def test_deep_health_refuses_unrecognized_origins_without_a_request(url: str) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"status": "ok"})

    with pytest.raises(ValueError, match="origin"):
        _gateway(handler).deep_health(url)
    assert sent == []


@pytest.mark.parametrize("status", [200, 503])
def test_deep_health_sends_the_bearer_token_and_returns_the_answer(status: int) -> None:
    sent: list[httpx.Request] = []
    body = {"status": "ok" if status == 200 else "failed", "checks": []}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json=body)

    assert _gateway(handler).deep_health("https://app.databricksapps.com") == (status, body)
    assert str(sent[0].url) == "https://app.databricksapps.com/health?deep=1"
    assert sent[0].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"status": "ok"}),
        httpx.Response(302, headers={"location": "https://evil.example.com/health"}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["500", "redirect", "not-an-object"],
)
def test_deep_health_raises_on_another_status_or_body(answer: httpx.Response) -> None:
    with pytest.raises((httpx.HTTPError, ValueError)):
        _gateway(lambda request: answer).deep_health("https://app.databricksapps.com")


def test_get_app_maps_404_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/2.0/apps/missing"
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})

    assert _gateway(handler).get_app("missing") is None


def test_get_app_returns_the_app_and_does_not_follow_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/2.0/apps/found":
            return httpx.Response(200, json={"name": "found", "id": "1"})
        return httpx.Response(302, headers={"location": "https://elsewhere.example/"})

    gateway = _gateway(handler)
    assert gateway.get_app("found") == {"name": "found", "id": "1"}
    with pytest.raises(httpx.HTTPStatusError):
        gateway.get_app("moved")


def test_probe_reads_the_warehouse_and_one_app() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url).removeprefix(HOST))
        return httpx.Response(200, json={})

    _gateway(handler).probe("wh-1")
    assert paths == ["/api/2.0/sql/warehouses/wh-1", "/api/2.0/apps?page_size=1"]


class _Runs:
    """A stand-in for `subprocess.run` that records each call."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout, self.returncode = stdout, returncode

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"args": args, **kwargs})
        stdout = f"Databricks CLI v{CLI_VERSION}".encode() if "--version" in args else self.stdout
        return subprocess.CompletedProcess(args, self.returncode, stdout, b"secret output")


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> _Runs:
    fake = _Runs(stdout=b'{"deployment_id": "d-1"}')
    monkeypatch.setattr(clients.shutil, "which", lambda name: "/bin/databricks")
    monkeypatch.setattr(clients.subprocess, "run", fake)
    return fake


def _cli() -> DatabricksCLI:
    return DatabricksCLI(token_provider=lambda host: "tok", timeout=7)


def test_check_version_refuses_another_cli_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients.shutil, "which", lambda name: "/bin/databricks")
    monkeypatch.setattr(
        clients.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, b"Databricks CLI v0.99.0", b""),
    )
    with pytest.raises(ValueError, match=CLI_VERSION):
        _cli().check_version()


def test_check_version_accepts_the_pinned_version(runs: _Runs) -> None:
    assert _cli().check_version() == CLI_VERSION


def test_execute_refuses_unknown_actions_and_run_without_app_key(
    runs: _Runs, tmp_path: Path
) -> None:
    cli = _cli()
    with pytest.raises(ValueError, match="unsupported"):
        cli.execute("destroy", tmp_path, target="dev", host=HOST)
    with pytest.raises(ValueError, match="unsupported"):
        cli.execute("run", tmp_path, target="dev", host=HOST)
    assert runs.calls == []


def test_cli_environment_drops_variables_outside_the_allow_list(
    runs: _Runs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_TEST_LEAK", "must-not-reach-the-cli")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "someone")
    monkeypatch.setenv("PATH", "/usr/bin")
    result = _cli().execute("run", tmp_path, target="dev", host=HOST, app_key="app")
    assert result == {"deployment_id": "d-1"}
    call = runs.calls[-1]
    env = call["env"]
    assert "SHIFDE_TEST_LEAK" not in env
    assert "DATABRICKS_CONFIG_PROFILE" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["DATABRICKS_HOST"] == HOST
    assert env["DATABRICKS_TOKEN"] == "tok"
    assert env["DATABRICKS_AUTH_TYPE"] == "pat"
    assert call["args"][1:] == ["bundle", "run", "app", "--target", "dev", "--output", "json"]
    assert call["cwd"] == tmp_path
    assert call["timeout"] == 7
    assert "DATABRICKS_TOKEN" not in runs.calls[0]["env"]  # the version check gets no token


def test_cli_failure_hides_output_and_a_timeout_becomes_timeout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clients.shutil, "which", lambda name: "/bin/databricks")
    failing = _Runs(returncode=3)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, f"v{CLI_VERSION}".encode(), b"")
        return failing(args, **kwargs)

    monkeypatch.setattr(clients.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        _cli().execute("deploy", tmp_path, target="dev", host=HOST)
    assert "secret" not in str(error.value)
    assert len(failing.calls) == 1

    def hang(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, f"v{CLI_VERSION}".encode(), b"")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(clients.subprocess, "run", hang)
    with pytest.raises(TimeoutError):
        _cli().execute("deploy", tmp_path, target="dev", host=HOST)


def test_databricks_integration_is_experimental_and_not_live_validated() -> None:
    assert MATURITY == "experimental"
    assert LIVE_VALIDATED is False
    platform = FakeDatabricksPlatform(service_principal="sp", warehouses={"wh"})
    adapter = DatabricksAdapter(cli=platform, workspace_factory=platform.workspace)
    target = _target()
    report = adapter.capabilities(ReadContext(tenant_id=target.tenant_id, target=target))
    assert report.maturity == "experimental"
    assert report.supports("cli") and report.supports("workspace")
