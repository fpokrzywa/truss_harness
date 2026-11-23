"""The opt-in local Docker daemon harness for `DockerCliRuntime` (design SP8 section 6).

It runs only with `SHIFDE_TEST_DOCKER=1` (`-m docker` selects it); otherwise it is skipped, so it
never runs in CI, `factory/checks.sh` or a default pytest run. Against the local daemon it checks
the versions, reads the daemon id, builds a test bundle whose app runs on the stdlib, brings it up
on a loopback port and reads `/health`. Its teardown removes its own project with a direct
`docker compose ... down`, never through the runtime. It proves nothing about a client's host
and never marks anything validated.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from shifde.adapters.native.docker.runtime import DockerCliRuntime, docker_host
from shifde.adapters.native.docker.source import RELEASE_LABEL
from tests.adapters.native.docker_bundle import bundle_files

OPT_IN = "local Docker harness is opt-in: set SHIFDE_TEST_DOCKER=1 and run with -m docker"
PORT = 18765

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("SHIFDE_TEST_DOCKER") != "1", reason=OPT_IN),
]


@pytest.fixture
def project() -> Iterator[str]:
    name = f"shifde-harness-{uuid4().hex[:8]}"
    yield name
    docker = shutil.which("docker")
    if docker:
        subprocess.run(  # direct teardown, never through the runtime
            [docker, "compose", "-p", name, "down", "--rmi", "all"],
            env={**os.environ, "DOCKER_HOST": docker_host()},
            capture_output=True,
            timeout=300,
            check=False,
        )


def test_docker_local_daemon_builds_and_serves_health(project: str) -> None:
    release = hashlib.sha256(project.encode()).hexdigest()
    files = bundle_files(target_id=uuid4(), project=project, port=PORT, release_digest=release)
    # The harness host has no `/etc/shifde-apps/<project>.env`; the runtime does not need one.
    document = yaml.safe_load(files["compose.yaml"])
    del document["services"]["app"]["env_file"]
    files["compose.yaml"] = yaml.safe_dump(document, sort_keys=False).encode()
    tag = document["services"]["app"]["image"]
    runtime = DockerCliRuntime()
    runtime.check_version()
    assert runtime.daemon_id()
    directory = Path(tempfile.mkdtemp(prefix="shifde-docker-"))
    try:
        for name, data in files.items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
        runtime.build(
            directory,
            tag=tag,
            expected_sha256={
                name: hashlib.sha256(files[name]).hexdigest()
                for name in ("Dockerfile", "compose.yaml")
            },
        )
        assert (runtime.image_labels(tag) or {}).get(RELEASE_LABEL) == release
        runtime.compose_up(directory, project=project)
    finally:
        shutil.rmtree(directory)
    deadline = time.monotonic() + 90
    status, body = 0, b""
    while time.monotonic() < deadline:
        try:
            status, body = runtime.http_get_local(PORT, "/health")
        except Exception:
            status = 0  # the container is not listening yet
        if status == 200:
            break
        time.sleep(1)
    assert status == 200
    assert b'"ok"' in body
    [container] = runtime.compose_ps(project)
    assert container.image == tag
    assert container.state == "running"
