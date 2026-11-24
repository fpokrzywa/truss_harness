"""`DockerCliRuntime` with a fake `subprocess.run`/`shutil.which` and an httpx mock transport:
no Docker daemon is needed."""

from __future__ import annotations

import hashlib
import json
import os
import shutil as stdlib_shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any  # Any: subprocess.run keyword arguments

import httpx
import pytest

import shifde.adapters.native.docker.runtime as runtime_module
from shifde.adapters.native.docker.runtime import (
    FORBIDDEN_FLAGS,
    MAX_BODY,
    DockerCliRuntime,
    DockerRefused,
)
from tests.adapters.native.docker_bundle import DAEMON

PROJECT = f"shifde-{'a' * 32}"
TAG = f"{PROJECT}:111111111111"
PUBLISHED = (
    f'{{"Name": "{PROJECT}-app-1", "Image": "{TAG}", "State": "running", "Health": "healthy", '
    '"Publishers": [{"URL": "127.0.0.1", "TargetPort": 8000, "PublishedPort": 8701, '
    '"Protocol": "tcp"}, {"URL": "0.0.0.0", "TargetPort": 8000, "PublishedPort": 8702, '
    '"Protocol": "tcp"}]}\n'
).encode()


class _Runs:
    """A stand-in for `subprocess.run` that records each call and answers the version checks."""

    def __init__(
        self,
        stdout: bytes = b"",
        returncode: int = 0,
        client: bytes = b"27.3.1\n",
        compose: bytes = b"2.29.7\n",
        server: bytes = b"28.5.1\n",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.configs: list[dict[str, bytes]] = []  # the DOCKER_CONFIG files seen by each call
        self.stdout, self.returncode = stdout, returncode
        self.client, self.compose, self.server = client, compose, server

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"args": args, **kwargs})
        config = Path(kwargs["env"]["DOCKER_CONFIG"])
        self.configs.append({path.name: path.read_bytes() for path in config.iterdir()})
        if args[1:3] == ["version", "--format"]:
            version = self.server if "Server" in args[3] else self.client
            return subprocess.CompletedProcess(args, 0, version, b"")
        if args[1:3] == ["compose", "version"]:
            return subprocess.CompletedProcess(args, 0, self.compose, b"")
        if args[1] == "info":
            return subprocess.CompletedProcess(args, 0, f"{DAEMON}\n".encode(), b"")
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, b"secret output")

    @property
    def commands(self) -> list[list[str]]:
        """The recorded argv without the executable and the version checks."""
        return [
            call["args"][1:]
            for call in self.calls
            if call["args"][1:3] not in (["version", "--format"], ["compose", "version"])
        ]


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> _Runs:
    fake = _Runs()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_module.subprocess, "run", fake)
    return fake


@pytest.fixture
def staged() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="shifde-docker-"))
    (directory / "Dockerfile").write_bytes(b"FROM x\n")
    (directory / "compose.yaml").write_bytes(b"services: {}\n")
    yield directory
    stdlib_shutil.rmtree(directory)


def _digests(directory: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("Dockerfile", "compose.yaml")
    }


@pytest.mark.parametrize(
    "host", ["tcp://10.0.0.1:2375", "ssh://user@host", "fd://", "", "unix://relative.sock"]
)
def test_docker_host_not_local_is_refused_before_any_subprocess(
    runs: _Runs, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("DOCKER_HOST", host)
    with pytest.raises(DockerRefused) as refused:
        DockerCliRuntime().daemon_id()
    assert refused.value.code == "docker_host_not_local"
    assert runs.calls == []


@pytest.mark.parametrize(
    "host", ["unix:///run/user/1000/docker.sock", "npipe:////./pipe/dockerDesktopLinuxEngine"]
)
def test_docker_host_local_forms_are_passed_explicitly(
    runs: _Runs, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("DOCKER_HOST", host)
    assert DockerCliRuntime().daemon_id() == DAEMON
    assert all(call["env"]["DOCKER_HOST"] == host for call in runs.calls)


def test_docker_cli_environment_holds_only_the_allow_list(
    runs: _Runs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_TEST_LEAK", "must-not-reach-docker")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    monkeypatch.setenv("COMPOSE_FILE", "elsewhere.yaml")
    monkeypatch.setenv("BUILDX_BUILDER", "remote")
    monkeypatch.setenv("PATH", "/usr/bin")
    assert DockerCliRuntime().daemon_id() == DAEMON
    for call in runs.calls:
        env = call["env"]
        assert set(env) <= runtime_module.ALLOWED_ENV | {"DOCKER_HOST", "DOCKER_CONFIG"}
        assert "SHIFDE_TEST_LEAK" not in env
        assert "DOCKER_CONTEXT" not in env
        assert env["DOCKER_HOST"] in (
            "unix:///var/run/docker.sock",
            "npipe:////./pipe/docker_engine",
        )
        assert env["PATH"] == "/usr/bin"
    assert runs.commands == [["info", "--format", "{{.ID}}"]]


def test_docker_cli_config_is_a_fresh_empty_directory(
    runs: _Runs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A user config with a proxy must never reach the CLI.
    (tmp_path / "config.json").write_text(
        json.dumps({"proxies": {"default": {"httpProxy": "http://proxy.example:3128"}}})
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    DockerCliRuntime().daemon_id()
    configs = [call["env"]["DOCKER_CONFIG"] for call in runs.calls]
    assert len(set(configs)) == len(configs) == 4  # one per command
    assert str(tmp_path) not in configs
    assert all(Path(config).name.startswith("shifde-docker-config-") for config in configs)
    assert runs.configs == [{}, {}, {}, {}]
    assert not any(os.path.exists(config) for config in configs)  # removed after each command


def test_docker_cli_config_names_only_the_plugin_dirs(runs: _Runs, tmp_path: Path) -> None:
    plugins = tmp_path / "cli-plugins"
    DockerCliRuntime(plugin_dirs=[plugins]).daemon_id()
    assert runs.configs
    for files in runs.configs:
        assert set(files) == {"config.json"}
        assert json.loads(files["config.json"]) == {"cliPluginsExtraDirs": [str(plugins)]}


@pytest.mark.parametrize("flag", sorted(FORBIDDEN_FLAGS))
def test_docker_run_guard_refuses_forbidden_flags(runs: _Runs, flag: str) -> None:
    cli = DockerCliRuntime()
    for argv in (["run", flag, "x"], ["run", f"{flag}=x"]):
        with pytest.raises(DockerRefused) as refused:
            cli._run(argv)
        assert refused.value.code == "forbidden_flag"
    assert runs.calls == []


@pytest.mark.parametrize(
    ("client", "compose"),
    [(b"23.0.6", b"2.29.7"), (b"27.3.1", b"1.29.2"), (b"garbage", b"2.29.7"), (b"27.3.1", b"")],
)
def test_docker_version_check_refuses_old_docker_or_compose(
    monkeypatch: pytest.MonkeyPatch, client: bytes, compose: bytes
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/docker")
    fake = _Runs(client=client, compose=compose)
    monkeypatch.setattr(runtime_module.subprocess, "run", fake)
    with pytest.raises(ValueError, match="required"):
        DockerCliRuntime().daemon_id()
    assert fake.commands == []


@pytest.mark.parametrize("compose", [b"v2.20.2", b"2.29.7", b"5.6.1"])
def test_docker_version_check_accepts_compose_v2_and_later(
    monkeypatch: pytest.MonkeyPatch, compose: bytes
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_module.subprocess, "run", _Runs(compose=compose))
    DockerCliRuntime().check_version()


@pytest.mark.parametrize("server", [b"27.5.1", b"27.99.0", b"20.10.24", b"garbage", b""])
def test_docker_engine_older_than_28_is_refused_before_any_command(
    monkeypatch: pytest.MonkeyPatch, staged: Path, server: bytes
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/docker")
    fake = _Runs(server=server)
    monkeypatch.setattr(runtime_module.subprocess, "run", fake)
    cli = DockerCliRuntime()
    for call in (
        cli.daemon_id,
        lambda: cli.build(staged, tag=TAG, expected_sha256=_digests(staged)),
        lambda: cli.compose_up(staged, project=PROJECT),
        lambda: cli.compose_ps(PROJECT),
        lambda: cli.image_labels(TAG),
    ):
        with pytest.raises(DockerRefused) as refused:
            call()
        assert refused.value.code == "docker_engine_too_old"
    assert fake.commands == []
    assert ["version", "--format", "{{.Server.Version}}"] in [c["args"][1:] for c in fake.calls]


@pytest.mark.parametrize("server", [b"28.0.0", b"28.0.4-rc.1", b"28.5.1", b"29.9.2"])
def test_docker_engine_28_or_later_is_accepted(
    monkeypatch: pytest.MonkeyPatch, server: bytes
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_module.subprocess, "run", _Runs(server=server))
    assert DockerCliRuntime().daemon_id() == DAEMON


def test_docker_compose_up_argv(runs: _Runs, staged: Path) -> None:
    DockerCliRuntime().compose_up(staged, project=PROJECT)
    assert runs.commands == [
        [
            "compose",
            "-p",
            PROJECT,
            "-f",
            "compose.yaml",
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
        ]
    ]
    assert runs.calls[-1]["cwd"] == staged


def test_docker_build_argv_after_the_sha256_check(runs: _Runs, staged: Path) -> None:
    cli = DockerCliRuntime()
    with pytest.raises(ValueError, match="differ"):
        cli.build(staged, tag=TAG, expected_sha256={**_digests(staged), "Dockerfile": "0" * 64})
    assert runs.calls == []
    cli.build(staged, tag=TAG, expected_sha256=_digests(staged))
    assert runs.commands == [
        ["build", "--tag", TAG, "--file", str(staged / "Dockerfile"), str(staged)]
    ]


def test_docker_build_refuses_a_directory_outside_the_staging_area(
    runs: _Runs, tmp_path: Path
) -> None:
    unprefixed = Path(tempfile.mkdtemp(prefix="other-"))
    nested = tmp_path / "shifde-docker-nested"
    nested.mkdir()
    try:
        for directory in (tmp_path, nested, unprefixed):
            (directory / "Dockerfile").write_bytes(b"FROM x\n")
            (directory / "compose.yaml").write_bytes(b"services: {}\n")
            with pytest.raises(ValueError, match="staged"):
                DockerCliRuntime().build(directory, tag=TAG, expected_sha256=_digests(directory))
            with pytest.raises(ValueError, match="staged"):
                DockerCliRuntime().compose_up(directory, project=PROJECT)
    finally:
        stdlib_shutil.rmtree(unprefixed)
    assert runs.calls == []


def test_docker_failure_hides_output_and_a_timeout_becomes_timeout_error(
    monkeypatch: pytest.MonkeyPatch, staged: Path
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(runtime_module.subprocess, "run", _Runs(returncode=3))
    with pytest.raises(RuntimeError) as error:
        DockerCliRuntime().compose_up(staged, project=PROJECT)
    assert "secret" not in str(error.value)
    assert "3" in str(error.value)

    def hang(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(runtime_module.subprocess, "run", hang)
    with pytest.raises(TimeoutError):
        DockerCliRuntime().daemon_id()


def test_docker_image_labels_and_compose_ps(runs: _Runs) -> None:
    cli = DockerCliRuntime()
    runs.stdout = b""
    assert cli.image_labels(TAG) is None
    runs.stdout = b'{"org.shifde.release_digest": "abc"}'
    assert cli.image_labels(TAG) == {"org.shifde.release_digest": "abc"}
    runs.stdout = (
        f'{{"Name": "{PROJECT}-app-1", "Image": "{TAG}", "State": "running", '
        '"Health": "healthy", "Labels": "org.shifde.target_id=t-1,other=x"}\n'
    ).encode()
    [container] = cli.compose_ps(PROJECT)
    assert (container.name, container.state, container.health) == (
        f"{PROJECT}-app-1",
        "running",
        "healthy",
    )
    assert container.labels == {"org.shifde.target_id": "t-1", "other": "x"}
    assert container.ports == ()
    runs.stdout = PUBLISHED
    assert cli.compose_ps(PROJECT)[0].ports == (8701,)  # loopback publishers only
    runs.stdout = b'[{"Name": "x-app-1", "Image": "i", "State": "exited", "Health": ""}]'
    assert cli.compose_ps(PROJECT)[0].health is None
    runs.stdout = b""
    assert cli.compose_ps(PROJECT) == []
    assert ["compose", "-p", PROJECT, "ps", "--all", "--format", "json"] in runs.commands
    with pytest.raises(ValueError, match="project"):
        cli.compose_ps("shop")  # only a target-derived project name


def test_docker_http_get_local_reads_loopback_health_without_redirects(runs: _Runs) -> None:
    runs.stdout = PUBLISHED
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.query:
            return httpx.Response(302, headers={"location": "https://elsewhere.example/"})
        return httpx.Response(200, json={"status": "ok"})

    cli = DockerCliRuntime(transport=httpx.MockTransport(handler))
    status, body = cli.http_get_local(PROJECT, 8701, "/health")
    assert (status, body) == (200, b'{"status":"ok"}')
    assert str(sent[0].url) == "http://127.0.0.1:8701/health"
    assert cli.http_get_local(PROJECT, 8701, "/health?deep=1")[0] == 302
    assert len(sent) == 2
    assert ["compose", "-p", PROJECT, "ps", "--all", "--format", "json"] in runs.commands


@pytest.mark.parametrize("port", [8702, 8703])
def test_docker_http_get_local_refuses_a_port_the_project_does_not_publish(
    runs: _Runs, port: int
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    runs.stdout = PUBLISHED  # 8701 on 127.0.0.1; 8702 only on 0.0.0.0
    cli = DockerCliRuntime(transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectionError):
        cli.http_get_local(PROJECT, port, "/health")
    assert sent == []


def test_docker_http_get_local_discards_the_answer_when_the_port_changed_hands(
    runs: _Runs,
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        runs.stdout = b""  # the project's container is gone by the time the answer arrives
        return httpx.Response(200, json={"status": "ok"})

    runs.stdout = PUBLISHED
    cli = DockerCliRuntime(transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectionError, match="no longer"):
        cli.http_get_local(PROJECT, 8701, "/health")
    assert len(sent) == 1
    ps = ["compose", "-p", PROJECT, "ps", "--all", "--format", "json"]
    assert runs.commands.count(ps) == 2


@pytest.mark.parametrize(
    ("port", "path"),
    [(8701, "/"), (8701, "/admin"), (8701, "/health?deep=2"), (80, "/health"), (70000, "/health")],
)
def test_docker_http_get_local_refuses_other_paths_and_ports(
    runs: _Runs, port: int, path: str
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    with pytest.raises(ValueError):
        DockerCliRuntime(transport=httpx.MockTransport(handler)).http_get_local(PROJECT, port, path)
    assert sent == []
    assert runs.calls == []


def test_docker_http_get_local_caps_the_body(runs: _Runs) -> None:
    runs.stdout = PUBLISHED
    cli = DockerCliRuntime(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (MAX_BODY + 1)))
    )
    with pytest.raises(ValueError, match="256 KiB"):
        cli.http_get_local(PROJECT, 8701, "/health")


def test_docker_cli_runtime_has_no_destructive_operation() -> None:
    for name in ("down", "rm", "prune", "pull"):
        assert not hasattr(DockerCliRuntime, name)
