"""The `ContainerRuntime` protocol and `DockerCliRuntime`, the host `docker` CLI (local daemon).

The CLI runs in a minimal environment with `DOCKER_HOST` always set to the local socket or named
pipe and `DOCKER_CONFIG` set to a fresh directory of its own (so `~/.docker/config.json` proxies,
credentials and contexts never apply), every argv is built here, and one guard refuses privileged,
network, volume, mount, secret and capability flags. There is no `down`, `rm`, `prune` or `pull`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol  # Any: decoded `docker compose ps` JSON

import httpx

from shifde.adapters.native.contracts import DAEMON_ID
from shifde.adapters.native.docker.source import PORT_MAX, PORT_MIN, PROJECT

# Before Engine 28.0 a container with NET_RAW on the bridge can reach ports another project
# publishes on 127.0.0.1 (`route_localnet`).
MIN_ENGINE = (28, 0)
HEALTH_PATHS = frozenset({"/health", "/health?deep=1"})
MAX_BODY = 256 * 1024
STAGING_PREFIX = "shifde-docker-"
BUILD_FILES = frozenset({"Dockerfile", "compose.yaml"})
FORBIDDEN_FLAGS = frozenset(
    {
        "--privileged",
        "--network",
        "--net",
        "-v",
        "--volume",
        "--mount",
        "--build-arg",
        "--secret",
        "--ssh",
        "--add-host",
        "--cap-add",
        "--device",
        "--security-opt",
    }
)
# The Databricks CLI allow-list (`DatabricksCLI._base_env`); `DOCKER_HOST` is set separately.
ALLOWED_ENV = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
_DAEMON_ID = re.compile(DAEMON_ID)
_UNIX_HOST = re.compile(r"unix:///\S+")
_NPIPE_HOST = re.compile(r"npipe:////\./pipe/[A-Za-z0-9._-]+")
_TAG = re.compile(r"shifde-[0-9a-f]{32}:[a-f0-9]{12}")


class DockerRefused(PermissionError):
    """A Docker refusal; `code` is `docker_host_mismatch`, `foreign_project`,
    `docker_host_not_local`, `docker_engine_too_old` or `forbidden_flag`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContainerState:
    name: str
    image: str
    state: str
    health: str | None  # healthy, starting, unhealthy or None
    labels: dict[str, str] = field(default_factory=dict)
    ports: tuple[int, ...] = ()  # host ports published on 127.0.0.1


class ContainerRuntime(Protocol):
    def daemon_id(self) -> str: ...
    def build(self, directory: Path, *, tag: str, expected_sha256: Mapping[str, str]) -> None: ...
    def image_labels(self, tag: str) -> dict[str, str] | None: ...
    def compose_up(self, directory: Path, *, project: str) -> None: ...
    def compose_ps(self, project: str) -> list[ContainerState]: ...
    def http_get_local(self, project: str, port: int, path: str) -> tuple[int, bytes]: ...


def check_local_request(port: int, path: str) -> None:
    if path not in HEALTH_PATHS:
        raise ValueError("only the health paths may be requested")
    if not PORT_MIN <= port <= PORT_MAX:
        raise ValueError("the port is outside 1024..65535")


def check_staged(directory: Path, expected_sha256: Mapping[str, str]) -> None:
    """Refuses staged `Dockerfile`/`compose.yaml` bytes whose sha256 differ from the expected."""
    if set(expected_sha256) != BUILD_FILES:
        raise ValueError("expected digests must name the Dockerfile and compose.yaml")
    for name in sorted(BUILD_FILES):
        staged = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if staged != expected_sha256[name]:
            raise ValueError("staged build files differ from the artifact")


def docker_host() -> str:
    """The explicit local `DOCKER_HOST`; anything but a unix socket or named pipe is refused."""
    value = os.environ.get("DOCKER_HOST")
    if value is None:
        return (
            "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
        )
    if _UNIX_HOST.fullmatch(value) or _NPIPE_HOST.fullmatch(value):
        return value
    raise DockerRefused("docker_host_not_local")


def _loopback_ports(value: Any) -> tuple[int, ...]:
    """The host ports of `docker compose ps` `Publishers` bound to 127.0.0.1."""
    if not isinstance(value, list):
        return ()
    return tuple(
        item["PublishedPort"]
        for item in value
        if isinstance(item, dict)
        and item.get("URL") == "127.0.0.1"
        and isinstance(item.get("PublishedPort"), int)
    )


def _labels(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, str) and value:
        return dict(item.partition("=")[::2] for item in value.split(","))
    return {}


class DockerCliRuntime:
    """The host `docker` CLI (24 or later, compose v2 or later) against the local daemon (Docker
    Engine 28.0 or later)."""

    def __init__(
        self,
        executable: str = "docker",
        *,
        plugin_dirs: Sequence[Path] = (),
        timeout: float = 900,
        http_timeout: float = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.executable, self.timeout = executable, timeout
        # CLI plugin directories (compose, buildx) outside the system ones, for example Docker
        # Desktop's per-user `~/.docker/cli-plugins`; the only setting the CLI config carries.
        self.plugin_dirs = tuple(plugin_dirs)
        self.http_timeout, self.transport = http_timeout, transport

    def _env(self, config: Path) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key.upper() in ALLOWED_ENV}
        env["DOCKER_HOST"] = docker_host()
        env["DOCKER_CONFIG"] = str(config)
        return env

    def _run(self, args: list[str], *, cwd: Path | None = None) -> bytes:
        for arg in args:
            if arg in FORBIDDEN_FLAGS or arg.split("=", 1)[0] in FORBIDDEN_FLAGS:
                raise DockerRefused("forbidden_flag")
        executable = shutil.which(self.executable)
        if not executable:
            raise FileNotFoundError("install Docker 28 or later or provide its executable path")
        with tempfile.TemporaryDirectory(prefix="shifde-docker-config-") as config:
            env = self._env(Path(config))
            if self.plugin_dirs:
                (Path(config) / "config.json").write_text(
                    json.dumps({"cliPluginsExtraDirs": [str(d) for d in self.plugin_dirs]}),
                    encoding="utf-8",
                )
            try:
                process = subprocess.run(
                    [executable, *args],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError("Docker command timed out") from None
        if process.returncode:
            # Raw Docker output may hold host details; never return it to an agent.
            raise RuntimeError(f"Docker command failed with exit code {process.returncode}")
        return process.stdout

    def check_version(self) -> None:
        client = self._run(["version", "--format", "{{.Client.Version}}"]).decode().strip()
        match = re.match(r"(\d+)\.", client)
        if not match or int(match.group(1)) < 24:
            raise ValueError("Docker 24 or later is required")
        server = self._run(["version", "--format", "{{.Server.Version}}"]).decode().strip()
        match = re.match(r"(\d+)\.(\d+)", server)
        # An unreadable version is refused too.
        if not match or (int(match.group(1)), int(match.group(2))) < MIN_ENGINE:
            raise DockerRefused("docker_engine_too_old")
        compose = self._run(["compose", "version", "--short"]).decode().strip()
        match = re.match(r"v?(\d+)\.", compose)
        # Compose v2's successors kept its CLI and numbered on from 5; only v1 is refused.
        if not match or int(match.group(1)) < 2:
            raise ValueError("Docker Compose v2 or later is required")

    def daemon_id(self) -> str:
        self.check_version()
        value = self._run(["info", "--format", "{{.ID}}"]).decode().strip()
        if not _DAEMON_ID.fullmatch(value):
            raise ValueError("the Docker daemon id is not recognized")
        return value

    @staticmethod
    def _check_directory(directory: Path) -> None:
        if (
            not directory.is_dir()
            or directory.resolve().parent != Path(tempfile.gettempdir()).resolve()
            or not directory.name.startswith(STAGING_PREFIX)
        ):
            raise ValueError("Docker runs only from a staged bundle directory")

    def build(self, directory: Path, *, tag: str, expected_sha256: Mapping[str, str]) -> None:
        self._check_directory(directory)
        if not _TAG.fullmatch(tag):
            raise ValueError("the image tag is not a derived tag")
        check_staged(directory, expected_sha256)
        self.check_version()
        self._run(["build", "--tag", tag, "--file", str(directory / "Dockerfile"), str(directory)])

    def image_labels(self, tag: str) -> dict[str, str] | None:
        if not _TAG.fullmatch(tag):
            raise ValueError("the image tag is not a derived tag")
        self.check_version()
        if not self._run(["image", "ls", "--quiet", tag]).strip():
            return None
        output = self._run(["image", "inspect", "--format", "{{json .Config.Labels}}", tag])
        return _labels(json.loads(output))

    def compose_up(self, directory: Path, *, project: str) -> None:
        self._check_directory(directory)
        if not PROJECT.fullmatch(project):
            raise ValueError("the project name is invalid")
        self.check_version()
        self._run(
            [
                "compose",
                "-p",
                project,
                "-f",
                "compose.yaml",
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
            ],
            cwd=directory,
        )

    def compose_ps(self, project: str) -> list[ContainerState]:
        if not PROJECT.fullmatch(project):
            raise ValueError("the project name is invalid")
        self.check_version()
        output = self._run(["compose", "-p", project, "ps", "--all", "--format", "json"])
        text = output.decode().strip()
        if not text:
            return []
        # Compose prints a JSON array (older v2) or one JSON object per line (newer v2).
        rows = (
            json.loads(text)
            if text.startswith("[")
            else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        return [
            ContainerState(
                name=str(row.get("Name", "")),
                image=str(row.get("Image", "")),
                state=str(row.get("State", "")),
                health=row.get("Health") or None,
                labels=_labels(row.get("Labels")),
                ports=_loopback_ports(row.get("Publishers")),
            )
            for row in rows
            if isinstance(row, dict)
        ]

    def http_get_local(self, project: str, port: int, path: str) -> tuple[int, bytes]:
        check_local_request(port, path)
        if not any(port in container.ports for container in self.compose_ps(project)):
            raise ConnectionError("the project publishes no container on that loopback port")
        body = b""
        with (
            httpx.Client(
                timeout=self.http_timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client,
            client.stream("GET", f"http://127.0.0.1:{port}{path}") as response,
        ):
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BODY:
                    raise ValueError("the health response exceeds 256 KiB")
            status = response.status_code
        # The port may have changed hands during the request; then the answer is not this project's.
        if not any(port in container.ports for container in self.compose_ps(project)):
            raise ConnectionError("the project no longer publishes that loopback port")
        return status, body
