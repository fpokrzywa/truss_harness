"""The deterministic `docker.compose/v1` bundle of a sealed release (SP8 design section 6).

`render_docker(release_digest, resolved, databricks, values) -> dict[str, bytes]` is pure, like
`appgen.render` (which dispatches a `docker.compose/v1` release here after checking its generator
version): it reads only the Docker lock under `shifde.appgen/templates` (the "appgen.render is
pure" import contract). The bundle is `app/**` and `release.json` of the Databricks render of the
same inputs, byte for byte (`databricks.yml` left out), `app/requirements-docker.txt` (SP5's lock
plus gunicorn), a `Dockerfile` and, only for a target's `DockerTargetValues`, its `compose.yaml`.
The base image, the labels, the project, image and env file names and the generator version are
those of `adapters.native.docker.source`, whose `validate_bundle` and `validate_for_target` a
bundle with target values passes. It holds no credential: the service principal's variables are in
the env file on the host, which `compose.yaml` only names.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from shifde.adapters.native.docker.source import (
    BASE_IMAGE,
    CONTAINER_PORT,
    GENERATOR_VERSION,
    RELEASE_LABEL,
    TARGET_LABEL,
    TMPFS,
    compose_project,
    env_file,
    image_tag,
)

if TYPE_CHECKING:
    from shifde.appgen.render import Resolved

__all__ = ["GENERATOR_VERSION", "TARGET_KIND", "DockerTargetValues", "render_docker"]

TARGET_KIND = "docker.compose/v1"
WORKDIR = "/srv/app"
THREADS = 8
# added to the app's `deadline_s`: a model or SQL call already running may end after it
TIMEOUT_MARGIN_S = 120
HEALTH_PROBE = (
    "import urllib.request; "
    f"urllib.request.urlopen('http://127.0.0.1:{CONTAINER_PORT}/health', timeout=20)"
)


class DockerTargetValues(BaseModel):
    """What `compose.yaml` takes from the target."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: UUID
    port: int
    warehouse_id: str
    serving_endpoint: str


def _dockerfile(release_digest: str, deadline_s: int) -> bytes:
    command = [
        "gunicorn",
        "--bind",
        f"0.0.0.0:{CONTAINER_PORT}",
        "--worker-class",
        "gthread",
        "--threads",
        str(THREADS),
        "--timeout",
        str(deadline_s + TIMEOUT_MARGIN_S),
        "--worker-tmp-dir",
        TMPFS,
        "app:create_app()",
    ]
    lines = [
        f"FROM {BASE_IMAGE}",
        f'LABEL {RELEASE_LABEL}="{release_digest}"',
        f"WORKDIR {WORKDIR}",
        f"COPY app/ {WORKDIR}/",
        "RUN pip install --no-cache-dir --require-hashes -r requirements-docker.txt",
        "USER 10001",
        f"EXPOSE {CONTAINER_PORT}",
        f"CMD {json.dumps(command)}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _compose(release_digest: str, values: DockerTargetValues) -> bytes:
    """Every string value is double-quoted, so Compose never reads one as a number. The healthcheck
    probes often only in the start period: each `/health` runs `SELECT 1` on the client's
    warehouse, which frequent probes would hold awake."""
    q = json.dumps  # an ASCII JSON string is a YAML double-quoted scalar
    project = compose_project(values.target_id)
    lines = [
        "services:",
        "  app:",
        f"    image: {q(image_tag(project, release_digest))}",
        f"    env_file: [{q(env_file(project))}]",
        "    environment:",
        f"      DATABRICKS_WAREHOUSE_ID: {q(values.warehouse_id)}",
        f"      SERVING_ENDPOINT: {q(values.serving_endpoint)}",
        f"    ports: [{q(f'127.0.0.1:{values.port}:{CONTAINER_PORT}')}]",
        "    read_only: true",
        f"    tmpfs: [{q(TMPFS)}]",
        '    cap_drop: ["ALL"]',
        '    security_opt: ["no-new-privileges:true"]',
        '    restart: "unless-stopped"',
        "    healthcheck:",
        f"      test: {q(['CMD', 'python', '-c', HEALTH_PROBE])}",
        '      interval: "1h"',
        '      timeout: "30s"',
        "      retries: 1",
        '      start_period: "5m"',
        '      start_interval: "5s"',
        "    labels:",
        f"      {TARGET_LABEL}: {q(str(values.target_id))}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_docker(
    release_digest: str,
    resolved: Resolved,
    databricks: Mapping[str, bytes],
    values: DockerTargetValues | None,
) -> dict[str, bytes]:
    """The bundle files, by path, of a `docker.compose/v1` release whose Databricks render is
    `databricks`."""
    lock = files("shifde.appgen").joinpath("templates", "requirements-docker.txt")
    result = {
        name: content
        for name, content in databricks.items()
        if name.startswith("app/") or name == "release.json"
    }
    result["app/requirements-docker.txt"] = lock.read_bytes()
    result["Dockerfile"] = _dockerfile(release_digest, resolved.agent.limits.deadline_s)
    if values is not None:
        result["compose.yaml"] = _compose(release_digest, values)
    return dict(sorted(result.items()))
