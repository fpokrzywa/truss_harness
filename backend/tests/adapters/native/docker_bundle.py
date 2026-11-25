"""Test-only `docker.compose/v1` bundles and the Docker adapter setup over the fake runtime.

`bundle_files` builds a bundle that meets every hardening rule. Its `app/app.py` is a plain-stdlib
WSGI `app` answering `/health` and `/health?deep=1` in the SP5 6.4 shape; it reads its release
digest from the staged `app/release_digest` file and reports `MARKER` as `generator_version`.
The module holds no secrets and is not collected as tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Any: compose YAML documents
from uuid import UUID, uuid4

import yaml

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    ReadContext,
    create_archive,
)
from shifde.adapters.native.contracts import (
    BuildArtifact,
    BuildRequest,
    DeploymentPlan,
    DockerComposeSpec,
    PlanRequest,
    Target,
    canonical_bytes,
)
from shifde.adapters.native.docker.adapter import DockerComposeAdapter
from shifde.adapters.native.docker.fake import FakeContainerRuntime
from shifde.adapters.native.docker.source import (
    BASE_IMAGE,
    RELEASE_LABEL,
    TARGET_LABEL,
    compose_project,
    env_file,
    image_tag,
)

DAEMON = "0b3c7e2a-4f1d-4c5e-8a9b-6d7e8f9a0b1c"
LEGACY_DAEMON = "ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ23:4567:ABCD:EFGH:IJKL:MNOP"
RELEASE = "1" * 64
MARKER = "docker-bundle-marker"

APP = '''"""Test-only stdlib WSGI app: /health and /health?deep=1 in the SP5 6.4 shape."""

import json
from pathlib import Path

MARKER = "{marker}"
WORKSPACE = None  # `prepare_app` may set a callable returning (ok, reason)


def _release_digest():
    return (Path(__file__).parent / "release_digest").read_text(encoding="utf-8").strip()


def app(environ, start_response):
    if environ.get("PATH_INFO") != "/health":
        start_response("404 Not Found", [("Content-Type", "application/json")])
        return [b"{{}}"]
    status, body = "200 OK", {{"status": "ok"}}
    if environ.get("QUERY_STRING") == "deep=1":
        checks = [{{"name": "app", "status": "passed", "reason": None}}]
        if WORKSPACE is not None:
            ok, reason = WORKSPACE()
            checks.append(
                {{"name": "workspace", "status": "passed" if ok else "failed", "reason": reason}}
            )
        good = all(check["status"] == "passed" for check in checks)
        status = "200 OK" if good else "503 Service Unavailable"
        body = {{
            "status": "ok" if good else "failed",
            "generator_version": MARKER,
            "release_digest": _release_digest(),
            "checks": checks,
        }}
    start_response(status, [("Content-Type", "application/json")])
    return [json.dumps(body).encode()]


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    make_server("0.0.0.0", 8000, app).serve_forever()
'''


def compose_document(*, target_id: UUID, port: int, release_digest: str) -> dict[str, Any]:
    project = compose_project(target_id)
    return {
        "services": {
            "app": {
                "image": image_tag(project, release_digest),
                "env_file": [env_file(project)],
                "environment": {
                    "DATABRICKS_WAREHOUSE_ID": "warehouse-123",
                    "SERVING_ENDPOINT": "endpoint-1",
                },
                "ports": [f"127.0.0.1:{port}:8000"],
                "read_only": True,
                "tmpfs": ["/tmp"],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "restart": "unless-stopped",
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import urllib.request; "
                        "urllib.request.urlopen('http://127.0.0.1:8000/health')",
                    ],
                    "interval": "2s",
                    "retries": 15,
                },
                "labels": {TARGET_LABEL: str(target_id)},
            }
        }
    }


def dockerfile(release_digest: str) -> str:
    return (
        f"FROM {BASE_IMAGE}\n"
        f'LABEL {RELEASE_LABEL}="{release_digest}"\n'
        "WORKDIR /srv\n"
        "COPY app/ /srv/app/\n"
        "USER 10001\n"
        "EXPOSE 8000\n"
        'CMD ["python", "/srv/app/app.py"]\n'
    )


def bundle_files(
    *,
    target_id: UUID,
    port: int,
    release_digest: str = RELEASE,
    marker: str = MARKER,
) -> dict[str, bytes]:
    compose = compose_document(target_id=target_id, port=port, release_digest=release_digest)
    return {
        "Dockerfile": dockerfile(release_digest).encode(),
        "compose.yaml": yaml.safe_dump(compose, sort_keys=False).encode(),
        "release.json": canonical_bytes(
            {"manifest": {"name": "docker-test"}, "digest": release_digest}
        ),
        "app/app.py": APP.format(marker=marker).encode(),
        "app/release_digest": release_digest.encode(),
    }


def edit_compose(files: dict[str, bytes], edit: Callable[[dict[str, Any]], None]) -> None:
    """Applies `edit` to the bundle's single compose service, in place."""
    document = yaml.safe_load(files["compose.yaml"])
    edit(document["services"]["app"])
    files["compose.yaml"] = yaml.safe_dump(document, sort_keys=False).encode()


@dataclass
class DockerSetup:
    runtime: FakeContainerRuntime
    adapter: DockerComposeAdapter
    build: BuildContext
    read: ReadContext

    @property
    def target(self) -> Target:
        return self.read.target

    @property
    def project(self) -> str:
        return compose_project(self.target.id)

    @property
    def image(self) -> str:
        return image_tag(self.project, RELEASE)

    @property
    def port(self) -> int:
        return int(self.target.bindings["port"])

    def files(self, release_digest: str = RELEASE, marker: str = MARKER) -> dict[str, bytes]:
        return bundle_files(
            target_id=self.target.id,
            port=self.port,
            release_digest=release_digest,
            marker=marker,
        )

    def artifact(
        self,
        files: dict[str, bytes] | None = None,
        *,
        application_id: UUID | None = None,
        release_digest: str = RELEASE,
        marker: str = MARKER,
    ) -> BuildArtifact:
        source = self.build.artifacts.put(
            self.build.tenant_id,
            create_archive(files or self.files(release_digest, marker)),
        )
        return self.adapter.build(
            BuildRequest(
                spec=DockerComposeSpec(),
                source=source,
                application_id=application_id or uuid4(),
            ),
            self.build,
        )

    def deploy_context(self, journal: InMemoryJournal | None = None) -> DeployContext:
        operation_id = journal.operation_id if journal else uuid4()
        return DeployContext(
            tenant_id=self.read.tenant_id,
            target=self.target,
            operation_id=operation_id,
            artifacts=self.build.artifacts,
            journal=journal or InMemoryJournal(self.read.tenant_id, operation_id),
            toolchain_digest=self.build.toolchain_digest,
        )

    def prepare(
        self, release_digest: str = RELEASE, marker: str = MARKER
    ) -> tuple[BuildArtifact, DeploymentPlan, DeployContext]:
        artifact = self.artifact(release_digest=release_digest, marker=marker)
        plan = self.adapter.plan(PlanRequest(artifact=artifact), self.read)
        return artifact, plan, self.deploy_context()


def make_docker_setup(
    runtime: FakeContainerRuntime,
    *,
    project: str = "shop",  # a display name only: the adapter never reads it
    port: int = 8701,
    daemon: str = DAEMON,
) -> DockerSetup:
    tenant = uuid4()
    target = Target(
        tenant_id=tenant,
        host=f"docker://{daemon}",
        bindings={"project": project, "port": str(port)},
    )
    artifacts = InMemoryArtifactStore()
    build = BuildContext(tenant_id=tenant, artifacts=artifacts, toolchain_digest="a" * 64)
    adapter = DockerComposeAdapter(runtime, artifacts=artifacts, authorize=lambda: None)
    return DockerSetup(runtime, adapter, build, ReadContext(tenant_id=tenant, target=target))
