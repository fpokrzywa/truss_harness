"""`shifde-executor` process control (SP4 design 6.2) against the in-process gateway and the fake
platform: a stop during an `apply` step lets the step and job finish and exits 0; a grace that
passes while a write is in flight exits 1 and posts no result; `whoami` answering 401 / 403 exits
5 without the token; a credentials file of another organization than `whoami`'s exits 2
before any lease; other `whoami` failures are retried; the health endpoints follow the
runner's ticks; malformed process-control variables exit 2.

OS signals are not sent (Windows cannot deliver SIGTERM in-process): the tests call the handler
`run()` installed for SIGTERM.
"""

from __future__ import annotations

import json
import logging
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any  # Any: JSON bodies, fake platform call arguments
from uuid import uuid4

import httpx
import pytest

from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.core.db import Database
from shifde.core.healthserver import HealthServer
from shifde.executor import main
from shifde.executor.client import GatewayClient, GatewayError, Whoami
from shifde.executor.runner import FakePlatform, Runner
from tests.delivery.routes import Flow, deployment_jobs, job_row
from tests.executor.support import (
    CREDENTIAL_TOKEN,
    ask_plan,
    credentials_file,
    deploy_actions,
    fake_platform,
    make_runner,
)

SIGNALS = [getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, n)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:  # noqa: S310 - loopback
            json.loads(r.read())
            return int(r.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def _sigterm() -> None:
    """What the OS would do on SIGTERM: call the handler `run()` installed."""
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)


@pytest.fixture(autouse=True)
def _handlers() -> Iterator[None]:
    """`run()` installs process signal handlers: the test process gets its own back."""
    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def _approved(flow: Flow, tmp_path: Path, fake: FakeDatabricksPlatform) -> tuple[str, Path]:
    """A plan sealed by a runner and approved: (deployment id, credentials file)."""
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    plan = ask_plan(flow, target["id"])
    assert make_runner(flow.client, flow.token, creds, fake).run_once()
    approved = flow.approve(flow.get_plan(plan["id"]))
    return approved["deployment_id"], creds


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    flow: Flow,
    creds: Path,
    fake: FakeDatabricksPlatform,
    token: str | None = None,
    **env: str,
) -> None:
    """`main.run(["--fake-platform"])` against the in-process gateway and `fake`."""
    monkeypatch.setenv(main.API_URL, "https://testserver")
    monkeypatch.setenv(main.TOKEN, token or flow.token)
    monkeypatch.setenv(main.CREDENTIALS_FILE, str(creds))
    monkeypatch.setenv("SHIFDE_ENV", "test")
    for name in (main.HEALTH_HOST, main.HEALTH_PORT, main.SHUTDOWN_GRACE):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        main, "GatewayClient", lambda url, token: GatewayClient(url, token, http=flow.client)
    )
    monkeypatch.setattr(main, "FakePlatform", lambda: FakePlatform(fake))


def _apply_job(owner_db: Database, deployment_id: str) -> Any:  # Any: the job row
    return next(j for j in deployment_jobs(owner_db, deployment_id) if j["kind"] == "apply")


def test_a_stop_during_an_apply_step_lets_the_job_finish_and_exits_0(
    flow: Flow, tmp_path: Path, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_platform()
    deployment_id, creds = _approved(flow, tmp_path, fake)
    port = _free_port()
    _wire(monkeypatch, flow, creds, fake, **{main.HEALTH_PORT: str(port)})
    probes: list[tuple[str, int]] = []
    execute = fake.execute

    def stop_mid_deploy(action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if action == "deploy":
            probes.extend([("live", _get(port, "/healthz")), ("ready", _get(port, "/readyz"))])
            _sigterm()
            probes.extend([("live", _get(port, "/healthz")), ("ready", _get(port, "/readyz"))])
        return execute(action, *args, **kwargs)

    monkeypatch.setattr(fake, "execute", stop_mid_deploy)
    assert main.run(["--fake-platform"]) == main.EXIT_STOPPED == 0
    # live and ready while the write runs; not ready (still live) once the stop is requested
    assert probes == [("live", 200), ("ready", 200), ("live", 200), ("ready", 503)]
    body = flow.get(deployment_id)
    assert body["status"] == "observing", body
    assert [(s["step_key"], s["state"]) for s in body["journal"]] == [
        ("bundle-deploy", "complete"),
        ("bundle-run", "complete"),
    ]
    assert (deploy_actions(fake, "deploy"), deploy_actions(fake, "run")) == (1, 1)
    assert job_row(owner_db, _apply_job(owner_db, deployment_id)["id"])["result"] is not None
    with pytest.raises(urllib.error.URLError):
        _get(port, "/healthz")  # the health port is closed once `run()` returned


def test_a_grace_passing_during_a_write_exits_1_and_posts_no_result(
    flow: Flow,
    tmp_path: Path,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = fake_platform()
    deployment_id, creds = _approved(flow, tmp_path, fake)
    _wire(monkeypatch, flow, creds, fake, **{main.SHUTDOWN_GRACE: "1"})
    release = threading.Event()
    execute = fake.execute

    def blocked_write(action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if action == "deploy":  # its `intent` was accepted
            _sigterm()
            release.wait(60)
        return execute(action, *args, **kwargs)

    monkeypatch.setattr(fake, "execute", blocked_write)
    caplog.set_level(logging.INFO, logger=main.__name__)
    try:
        started = time.monotonic()
        assert main.run(["--fake-platform"]) == main.EXIT_CRASHED == 1
        assert time.monotonic() - started < 30
        assert "(a platform write was in flight); no result is posted" in caplog.text
    finally:
        release.set()
    runner = [t for t in threading.enumerate() if t.name == main.RUNNER_THREAD]
    for thread in runner:
        thread.join(30)
        assert not thread.is_alive()
    job = job_row(owner_db, _apply_job(owner_db, deployment_id)["id"])
    assert job["result"] is None  # no result and no cancel: the server's sweep decides it
    assert deploy_actions(fake, "deploy") == 1 and deploy_actions(fake, "run") == 0


@pytest.mark.parametrize("status", [401, 403])
def test_whoami_refused_exits_5_without_the_token(
    flow: Flow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    wrong = "shx_wrong_executor-token-never-logged"  # noqa: S105 - a test value
    if status == 401:  # the gateway itself refuses an unknown token
        _wire(monkeypatch, flow, creds, fake_platform(), token=wrong)
    else:
        _wire(monkeypatch, flow, creds, fake_platform(), token=wrong)

        def forbidden(self: GatewayClient) -> Whoami:
            raise GatewayError(403, "forbidden")

        monkeypatch.setattr(GatewayClient, "whoami", forbidden)
    assert main.run(["--fake-platform"]) == main.EXIT_AUTH == 5
    out, err = capsys.readouterr()
    assert f"shifde-executor: refusing to start: the gateway answered {status} to whoami" in err
    for text in (out, err, caplog.text):
        assert wrong not in text and flow.token not in text and CREDENTIAL_TOKEN not in text


def test_a_credentials_file_of_another_organization_exits_2_before_any_lease(
    flow: Flow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    _wire(monkeypatch, flow, creds, fake_platform())
    whoami = GatewayClient.whoami
    other = uuid4()
    monkeypatch.setattr(
        GatewayClient, "whoami", lambda self: whoami(self).model_copy(update={"tenant_id": other})
    )
    leases: list[object] = []
    runners: list[object] = []

    def no_runner(self: Runner, *args: object, **kwargs: object) -> None:
        runners.append(self)
        raise RuntimeError("no runner is built for a mismatched organization")

    monkeypatch.setattr(GatewayClient, "lease", lambda self, kinds: leases.append(kinds))
    monkeypatch.setattr(Runner, "__init__", no_runner)
    assert main.run(["--fake-platform"]) == main.EXIT_CONFIG == 2
    out, err = capsys.readouterr()
    assert (
        "shifde-executor: refusing to start: the credentials file belongs to another "
        "organization than this executor"
    ) in err
    assert leases == [] and runners == []
    for text in (out, err, caplog.text):
        assert flow.token not in text and CREDENTIAL_TOKEN not in text
    assert str(other) not in err and str(flow.tenant) not in err


def test_other_whoami_failures_are_retried_until_the_executor_runs(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    port = _free_port()
    _wire(monkeypatch, flow, creds, fake_platform(), **{main.HEALTH_PORT: str(port)})
    monkeypatch.setattr(main, "POLL_SECONDS", 0.5)  # liveness before `whoami`: 3 x the retry
    caplog.set_level(logging.WARNING, logger=main.__name__)
    whoami = GatewayClient.whoami
    failures: list[Exception] = [GatewayError(503, "unavailable"), httpx.ConnectError("refused")]
    probes: list[tuple[int, int]] = []

    def flaky(self: GatewayClient) -> Whoami:
        if failures:
            probes.append((_get(port, "/healthz"), _get(port, "/readyz")))
            raise failures.pop(0)
        return whoami(self)

    leased: list[Runner] = []
    run_once = Runner.run_once

    def first_poll(self: Runner) -> bool:
        leased.append(self)
        _sigterm()
        return run_once(self)

    monkeypatch.setattr(GatewayClient, "whoami", flaky)
    monkeypatch.setattr(Runner, "run_once", first_poll)
    assert main.run(["--fake-platform"]) == main.EXIT_STOPPED
    assert failures == [] and len(leased) == 1  # the runner ran after the two failures
    assert probes == [(200, 503), (200, 503)]  # alive, never ready before `whoami` answered
    assert "whoami failed (GatewayError); retrying" in caplog.text
    assert "whoami failed (ConnectError); retrying" in caplog.text


def test_a_stop_while_whoami_is_retried_exits_0(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    _wire(monkeypatch, flow, creds, fake_platform())
    monkeypatch.setattr(main, "POLL_SECONDS", 0.01)

    def unavailable(self: GatewayClient) -> Whoami:
        _sigterm()
        raise GatewayError(503, "unavailable")

    monkeypatch.setattr(GatewayClient, "whoami", unavailable)
    assert main.run(["--fake-platform"]) == main.EXIT_STOPPED


def test_the_health_endpoints_follow_the_runner_ticks(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    runner = make_runner(flow.client, flow.token, creds, fake_platform())  # `whoami` answered
    config = main.Config(
        api_url="https://testserver",
        token=flow.token,
        broker=runner.broker,
        platform=runner.platform,
    )
    process = main.Process(config, runner.client)
    server = HealthServer("127.0.0.1", 0, live=process.live, ready=process.ready).start()
    port = server.port
    try:
        assert (_get(port, "/healthz"), _get(port, "/readyz")) == (200, 503)  # no runner yet
        process.runner = runner
        assert (_get(port, "/healthz"), _get(port, "/readyz")) == (200, 200)
        idle = 3 * runner.poll_seconds
        stale = time.monotonic() - idle - 1
        runner.last_tick = stale  # the run loop stopped passing
        assert (_get(port, "/healthz"), _get(port, "/readyz")) == (503, 200)
        runner.client.last_success = stale  # and no gateway call succeeded since
        assert _get(port, "/readyz") == 503
        # a job in flight: the window is three heartbeats, which its lease heartbeats tick
        monkeypatch.setattr(runner, "_state", object())
        assert runner.window() == 3 * runner.heartbeat_seconds > idle
        assert (_get(port, "/healthz"), _get(port, "/readyz")) == (200, 200)
        runner.last_tick = runner.client.last_success = time.monotonic() - 3 * 15.0 - 1
        assert (_get(port, "/healthz"), _get(port, "/readyz")) == (503, 503)
        runner.last_tick = runner.client.last_success = time.monotonic()
        process.request_stop()  # a stop: still live, no longer ready
        assert (_get(port, "/healthz"), _get(port, "/readyz")) == (200, 503)
        assert _get(port, "/other") == 404
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (main.HEALTH_PORT, "not-a-port"),
        (main.HEALTH_PORT, "65536"),
        (main.HEALTH_PORT, "-1"),
        (main.SHUTDOWN_GRACE, "0"),
        (main.SHUTDOWN_GRACE, "1.5"),
    ],
)
def test_bad_process_control_variables_exit_2_naming_the_variable(
    flow: Flow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    _wire(monkeypatch, flow, creds, fake_platform(), **{name: value})
    with pytest.raises(SystemExit) as exited:
        main.run(["--fake-platform"])
    assert exited.value.code == main.EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert name in err and value not in err.replace(name, "")


def test_the_process_control_variables_are_read(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    _wire(monkeypatch, flow, creds, fake_platform())
    config = main.configure(["--fake-platform"], main.os.environ)
    assert (config.health_host, config.health_port, config.shutdown_grace_seconds) == (
        "127.0.0.1",
        0,
        60,
    )
    env = {main.HEALTH_HOST: "0.0.0.0", main.HEALTH_PORT: "8899", main.SHUTDOWN_GRACE: "5"}  # noqa: S104
    _wire(monkeypatch, flow, creds, fake_platform(), **env)
    config = main.configure(["--fake-platform"], main.os.environ)
    assert (config.health_host, config.health_port, config.shutdown_grace_seconds) == (
        "0.0.0.0",  # noqa: S104 - a configured value, not a bind
        8899,
        5,
    )


def test_a_health_port_in_use_exits_2(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = credentials_file(tmp_path, "https://adb-1.cloud.databricks.com", tenant=flow.tenant)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        _wire(monkeypatch, flow, creds, fake_platform(), **{main.HEALTH_PORT: str(port)})
        assert main.run(["--fake-platform"]) == main.EXIT_CONFIG
    assert main.HEALTH_PORT in capsys.readouterr().err
