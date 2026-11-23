"""An in-memory Docker daemon for development and tests only (no real daemon exists).

`FakeContainerRuntime` is a `ContainerRuntime`. `build` stores an image (labels, `Dockerfile`,
`compose.yaml` and `app/` files) after the sha256 check and the bundle validation; `compose_up`
creates or replaces the project's single container from that image; `http_get_local` serves the
container by loading the staged `app/app.py` and calling its `app` WSGI callable, which is what
Flask's test client does. `prepare_app` gets the loaded module and the compose `environment` and
is where a workspace fake is injected. Fault switches are off by default.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: WSGI environ values
from uuid import uuid4

import yaml

from shifde.adapters.native.docker.runtime import ContainerState, check_local_request, check_staged
from shifde.adapters.native.docker.source import (
    CONTAINER_PORT,
    PORT_MAX,
    PORT_MIN,
    dockerfile_labels,
    validate_bundle,
)

PrepareApp = Callable[[ModuleType, Mapping[str, str]], None]
DEEP_HEALTH_BAD = (
    b'{"status": "failed", "generator_version": "fake", "release_digest": null, '
    b'"checks": [{"name": "warehouse", "status": "failed", "reason": "warehouse unreachable"}]}'
)


@dataclass
class FakeImage:
    labels: dict[str, str]
    dockerfile: bytes
    compose: bytes
    app: dict[str, bytes]


@dataclass
class FakeContainer:
    name: str
    image: str
    state: str
    health: str | None
    port: int
    labels: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)


def _staged_files(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class FakeContainerRuntime:
    def __init__(
        self,
        daemon_id: str,
        *,
        hang_timeout: float = 60.0,
        prepare_app: PrepareApp | None = None,
    ) -> None:
        self._daemon_id = daemon_id
        self.hang_timeout = hang_timeout
        self.prepare_app = prepare_app
        self.images: dict[str, FakeImage] = {}
        self.projects: dict[str, FakeContainer] = {}
        self.actions: list[tuple[str, str]] = []
        self.fail_after_accept: str | None = None  # "image-build" or "compose-up"
        self.build_failed = False
        self.unhealthy = False
        self.daemon_mismatch = False
        self.deep_health_bad = False
        self.hang = False
        self._released = threading.Event()

    def release(self) -> None:
        """Unblock a `hang`ing write (it still raises `TimeoutError`: accepted, unconfirmed)."""
        self._released.set()

    def _after_write(self, step: str) -> None:
        if self.fail_after_accept == step:
            raise TimeoutError("connection lost after the daemon accepted the request")
        if self.hang:
            self._released.wait(self.hang_timeout)
            raise TimeoutError("daemon write accepted but never confirmed")

    def daemon_id(self) -> str:
        if self.daemon_mismatch:
            return str(uuid4())
        return self._daemon_id

    def build(self, directory: Path, *, tag: str, expected_sha256: Mapping[str, str]) -> None:
        self.actions.append(("build", tag))
        check_staged(directory, expected_sha256)
        files = _staged_files(directory)
        validate_bundle(files)
        if self.build_failed:
            raise RuntimeError("Docker command failed with exit code 1")
        self.images[tag] = FakeImage(
            labels=dockerfile_labels(files["Dockerfile"].decode("utf-8")),
            dockerfile=files["Dockerfile"],
            compose=files["compose.yaml"],
            app={name: data for name, data in files.items() if name.startswith("app/")},
        )
        self._after_write("image-build")

    def image_labels(self, tag: str) -> dict[str, str] | None:
        image = self.images.get(tag)
        return dict(image.labels) if image else None

    def compose_up(self, directory: Path, *, project: str) -> None:
        self.actions.append(("compose-up", project))
        compose = (directory / "compose.yaml").read_bytes()
        bundle = validate_bundle(_staged_files(directory))
        tag = str(bundle.service.get("image"))
        image = self.images.get(tag)
        if image is None:
            raise RuntimeError("Docker command failed with exit code 1")
        if image.compose != compose:
            raise ValueError("staged compose.yaml differs from the image's")
        ports = bundle.service.get("ports") or []
        port = int(str(ports[0]).rsplit(":", 2)[1]) if ports else 0
        if not PORT_MIN <= port <= PORT_MAX:
            raise RuntimeError("Docker command failed with exit code 1")
        service = next(iter(yaml.safe_load(compose)["services"]))
        self.projects[project] = FakeContainer(
            name=f"{project}-{service}-1",
            image=tag,
            state="running",
            health="healthy",
            port=port,
            labels=dict(bundle.labels),
            environment=dict(bundle.environment),
        )
        self._after_write("compose-up")

    def compose_ps(self, project: str) -> list[ContainerState]:
        container = self.projects.get(project)
        if container is None:
            return []
        return [
            ContainerState(
                name=container.name,
                image=container.image,
                state=container.state,
                health="unhealthy" if self.unhealthy else container.health,
                labels=dict(container.labels),
            )
        ]

    def http_get_local(self, port: int, path: str) -> tuple[int, bytes]:
        check_local_request(port, path)
        container = next(
            (c for c in self.projects.values() if c.port == port and c.state == "running"), None
        )
        if container is None:
            raise ConnectionError("connection refused")
        if self.deep_health_bad and path == "/health?deep=1":
            return 503, DEEP_HEALTH_BAD
        return self._serve(self.images[container.image], container, path)

    def _serve(self, image: FakeImage, container: FakeContainer, path: str) -> tuple[int, bytes]:
        with tempfile.TemporaryDirectory(prefix="shifde-fake-app-") as temporary:
            root = Path(temporary)
            for name, data in image.app.items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            module = self._load(root / "app")
            if self.prepare_app:
                self.prepare_app(module, dict(container.environment))
            path_info, _, query = path.partition("?")
            environ: dict[str, Any] = {
                "REQUEST_METHOD": "GET",
                "SCRIPT_NAME": "",
                "PATH_INFO": path_info,
                "QUERY_STRING": query,
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": str(CONTAINER_PORT),
                "SERVER_PROTOCOL": "HTTP/1.1",
                "HTTP_HOST": f"127.0.0.1:{container.port}",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }
            status: list[int] = []

            def start_response(
                line: str, headers: list[tuple[str, str]], exc_info: object = None
            ) -> Callable[[bytes], object]:
                status.append(int(line.split(" ", 1)[0]))
                return lambda data: None

            result = module.app(environ, start_response)
            try:
                body = b"".join(result)
            finally:
                close = getattr(result, "close", None)
                if close:
                    close()
            return status[0], body

    @staticmethod
    def _load(directory: Path) -> ModuleType:
        name = f"shifde_fake_app_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, directory / "app.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("the staged app cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(directory))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(directory))
        return module
