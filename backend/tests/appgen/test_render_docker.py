"""SP8-9: `appgen.render` gives the Docker bundle of a correctly sealed `docker.compose/v1` release
and refuses every other seal, an unknown kind and a kind with a generator version but no renderer.
The bundle is the Databricks render's `app/**` byte for byte, the Docker lock, a `Dockerfile`, the
release record and, for a target, a `compose.yaml` that passes the adapter's bundle rules; its
golden digest is pinned, and the Databricks and Snowflake versions and renders are the ones from
before."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any  # Any: JSON documents
from uuid import UUID

import pytest
import yaml

from shifde.adapters.native.base import create_archive
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.docker import source as docker_source
from shifde.adapters.native.docker.source import (
    BASE_IMAGE,
    RELEASE_LABEL,
    dockerfile_labels,
    validate_bundle,
    validate_for_target,
)
from shifde.appgen import render as render_module
from shifde.appgen.render import GENERATOR_VERSIONS, GeneratorVersionUnsupported, render
from shifde.appgen.render_docker import DockerTargetValues
from shifde.packaging.canonical import canonical, digest
from tests.appgen.rendered import (
    DOCKER_SEALS,
    ENDPOINT,
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    SAMPLE_RESOLVED,
    SNOWFLAKE_DIGEST,
    SNOWFLAKE_MANIFEST,
    SNOWFLAKE_RESOLVED,
    on_docker,
)
from tests.appgen.test_render import FORBIDDEN, GOLDEN_DATABRICKS

# the Snowflake render of the SP6 fixture before SP8 (DECISIONS B-4071)
GOLDEN_SNOWFLAKE = "e9c9bbeb3f957bbbf96d09fb16169c3a5f3e850f3dd18f9dbfd7b96a2a05a2fd"
# the Docker render of the SP5 fixture for `VALUES` (DECISIONS B-4125)
GOLDEN_DOCKER = "14f465d44270e9fe09f9ee82b8babf7c6f7b33482f7400d9ff276e5efef60e2f"
REFUSED = "the release was sealed for another app generator version; seal it again"
TARGET_ID = UUID("0a1b2c3d-0000-4000-8000-00000000d0c5")
VALUES = DockerTargetValues(
    target_id=TARGET_ID, port=8501, warehouse_id="abc123def456", serving_endpoint=ENDPOINT
)
TEMPLATES = files("shifde.appgen").joinpath("templates")


def _golden(rendered: dict[str, bytes]) -> str:
    """The sha256 of the sorted names and contents, each after its length (DECISIONS B-3999)."""
    golden = hashlib.sha256()
    for name, content in sorted(rendered.items()):
        if not name.startswith("app/static/"):
            for part in (name.encode("utf-8"), content):
                golden.update(len(part).to_bytes(8, "big") + part)
    return golden.hexdigest()


def _manifest(app: Any, base: dict[str, Any] = SAMPLE_MANIFEST) -> dict[str, Any]:
    manifest = {**base}
    on_docker(app)(manifest)
    return manifest


DOCKER_MANIFEST = _manifest(DOCKER_SEALS["docker"])
DOCKER_DIGEST = digest(DOCKER_MANIFEST)


def _render(values: DockerTargetValues | None = VALUES) -> dict[str, bytes]:
    return render(DOCKER_MANIFEST, DOCKER_DIGEST, SAMPLE_RESOLVED, values)


# ------------------------------------------------------------------ dispatch
@pytest.mark.parametrize("app", DOCKER_SEALS.values(), ids=DOCKER_SEALS)
@pytest.mark.parametrize("base", [SAMPLE_MANIFEST, SNOWFLAKE_MANIFEST], ids=["dbx", "sf"])
def test_only_a_correctly_sealed_docker_release_renders(base: dict[str, Any], app: Any) -> None:
    manifest = _manifest(app, base)
    if app == DOCKER_SEALS["docker"]:
        assert "Dockerfile" in render(manifest, digest(manifest), SAMPLE_RESOLVED)
        return
    with pytest.raises(GeneratorVersionUnsupported) as caught:
        render(manifest, digest(manifest), SAMPLE_RESOLVED)
    assert caught.value.status == 409 and caught.value.code == "generator_version_unsupported"
    assert str(caught.value) == REFUSED


@pytest.mark.parametrize("version", GENERATOR_VERSIONS.values())
@pytest.mark.parametrize(
    "kind", ["fabric.solution/v1", "palantir.marketplace/v1", "databricks.job/v1", "", None, ["x"]]
)
def test_an_unknown_kind_is_refused_whatever_version_it_carries(kind: Any, version: str) -> None:
    manifest = {
        **SAMPLE_MANIFEST,
        "target_kind": kind,
        "app": {"generator_version": version},
    }
    for values in (None, VALUES):
        with pytest.raises(GeneratorVersionUnsupported) as caught:
            render(manifest, SAMPLE_DIGEST, SAMPLE_RESOLVED, values)
        assert str(caught.value) == REFUSED


@pytest.mark.parametrize("kind", ["test.kind/v1", "docker.compose/v1", "databricks.app/v1"])
def test_a_kind_with_a_version_but_no_renderer_is_refused(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setitem(GENERATOR_VERSIONS, kind, "shifde.appgen.test/1.0")
    monkeypatch.delitem(render_module.RENDERERS, kind, raising=False)
    manifest = {
        **SAMPLE_MANIFEST,
        "target_kind": kind,
        "app": {"generator_version": "shifde.appgen.test/1.0"},
    }
    with pytest.raises(GeneratorVersionUnsupported) as caught:
        render(manifest, SAMPLE_DIGEST, SAMPLE_RESOLVED)
    assert str(caught.value) == REFUSED


def test_docker_adds_a_version_and_changes_no_other_version_or_render() -> None:
    assert GENERATOR_VERSIONS == {
        "databricks.app/v1": "shifde.appgen/1.0",
        "snowflake.native-app/v1": "shifde.appgen.snowflake/1.0",
        "docker.compose/v1": "shifde.appgen.docker/1.0",
    }
    databricks = render(SAMPLE_MANIFEST, SAMPLE_DIGEST, SAMPLE_RESOLVED)
    assert _golden(databricks) == GOLDEN_DATABRICKS
    snowflake = render(SNOWFLAKE_MANIFEST, SNOWFLAKE_DIGEST, SNOWFLAKE_RESOLVED)
    assert _golden(snowflake) == GOLDEN_SNOWFLAKE


def test_the_renderers_are_exactly_the_kinds_with_a_generator() -> None:
    assert set(render_module.RENDERERS) == {
        "databricks.app/v1",
        "snowflake.native-app/v1",
        "docker.compose/v1",
    }
    # the executor, which never imports appgen, checks a plan against this constant
    assert GENERATOR_VERSIONS["docker.compose/v1"] == docker_source.GENERATOR_VERSION


# ------------------------------------------------------------------ bundle
def test_the_docker_render_is_pinned_and_deterministic() -> None:
    """Golden digest (Call S8-K) in the B-3999 form, for the fixed `VALUES`; change it only with a
    logged decision in `docs/build/DECISIONS.md`."""
    first, second = _render(), _render()
    assert first == second
    assert create_archive(first) == create_archive(second)
    assert _golden(first) == GOLDEN_DOCKER


def test_the_app_is_the_databricks_renders_plus_the_docker_lock() -> None:
    bundle = _render()
    # the Databricks app of the same release: its digest is in `agent.json`
    databricks = render(SAMPLE_MANIFEST, DOCKER_DIGEST, SAMPLE_RESOLVED)
    app = {name: content for name, content in databricks.items() if name.startswith("app/")}
    assert "app/app.yaml" in app and "app/static/index.html" in app
    lock = TEMPLATES.joinpath("requirements-docker.txt").read_bytes()
    assert {n: c for n, c in bundle.items() if n.startswith("app/")} == {
        **app,
        "app/requirements-docker.txt": lock,
    }
    assert set(bundle) - {n for n in bundle if n.startswith("app/")} == {
        "Dockerfile",
        "compose.yaml",
        "release.json",
    }
    assert bundle["release.json"] == canonical(
        {"manifest": DOCKER_MANIFEST, "digest": DOCKER_DIGEST}
    )
    assert json.loads(bundle["app/config/agent.json"])["release_digest"] == DOCKER_DIGEST


def test_without_target_values_the_render_has_no_compose_file() -> None:
    """A preview: the bundle's other files are the same, and its own rules refuse it."""
    preview = _render(None)
    bundle = _render()
    assert "compose.yaml" not in preview
    assert preview == {n: c for n, c in bundle.items() if n != "compose.yaml"}
    with pytest.raises(docker_source.BundleInvalid) as caught:
        validate_bundle(preview)
    assert caught.value.code == "bundle_missing"


def test_the_bundle_passes_the_adapters_rules_for_its_own_target_only() -> None:
    bundle = validate_bundle(_render())
    assert bundle.release_digest == DOCKER_DIGEST
    target = Target(
        tenant_id=UUID(int=1),
        id=TARGET_ID,
        host=f"docker://{TARGET_ID}",
        bindings={"port": "8501"},
    )
    assert validate_for_target(bundle, target) == (f"shifde-{TARGET_ID.hex}", 8501)
    with pytest.raises(PermissionError):
        validate_for_target(bundle, target.model_copy(update={"id": UUID(int=2)}))
    with pytest.raises(docker_source.BundleInvalid):
        validate_for_target(bundle, target.model_copy(update={"bindings": {"port": "8502"}}))


def test_the_compose_service_is_hardened_and_names_the_target() -> None:
    document = yaml.safe_load(_render()["compose.yaml"])
    assert set(document) == {"services"} and len(document["services"]) == 1
    service = document["services"]["app"]
    health = service.pop("healthcheck")
    project = f"shifde-{TARGET_ID.hex}"
    assert service == {
        "image": f"{project}:{DOCKER_DIGEST[:12]}",
        "env_file": [f"/etc/shifde-apps/{project}.env"],
        "environment": {"DATABRICKS_WAREHOUSE_ID": "abc123def456", "SERVING_ENDPOINT": ENDPOINT},
        "ports": ["127.0.0.1:8501:8000"],
        "read_only": True,
        "tmpfs": ["/tmp"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "labels": {"org.shifde.target_id": str(TARGET_ID)},
    }
    assert health["test"][:3] == ["CMD", "python", "-c"]
    assert "http://127.0.0.1:8000/health'" in health["test"][3]
    compile(health["test"][3], "healthcheck", "exec")
    assert (health["start_period"], health["start_interval"], health["interval"]) == (
        "5m",
        "5s",
        "1h",
    )


def test_every_string_value_is_double_quoted_so_compose_reads_it_as_text() -> None:
    """`123e4` unquoted is a float to Compose (YAML 1.2)."""
    values = VALUES.model_copy(update={"warehouse_id": "123e4", "serving_endpoint": "0777"})
    raw = _render(values)["compose.yaml"]
    assert b'DATABRICKS_WAREHOUSE_ID: "123e4"\n' in raw
    assert b'SERVING_ENDPOINT: "0777"\n' in raw
    environment = validate_bundle(_render(values)).service["environment"]
    assert environment == {"DATABRICKS_WAREHOUSE_ID": "123e4", "SERVING_ENDPOINT": "0777"}

    def strings(node: yaml.Node) -> list[yaml.ScalarNode]:
        if isinstance(node, yaml.MappingNode):
            return [s for _, value in node.value for s in strings(value)]
        if isinstance(node, yaml.SequenceNode):
            return [s for value in node.value for s in strings(value)]
        assert isinstance(node, yaml.ScalarNode)
        return [node]

    scalars = strings(yaml.compose(raw.decode("utf-8")))
    plain = {s.value for s in scalars if s.style != '"'}
    assert plain == {"true", "1"}  # `read_only` and the healthcheck's `retries`


def test_the_dockerfile_pins_the_base_image_and_runs_gunicorn_unprivileged() -> None:
    assert BASE_IMAGE == (
        "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
    )
    text = _render()["Dockerfile"].decode("utf-8")
    lines = text.splitlines()
    assert [line for line in lines if line.startswith("FROM")] == [f"FROM {BASE_IMAGE}"]
    assert dockerfile_labels(text) == {RELEASE_LABEL: DOCKER_DIGEST}
    assert any(
        line.startswith("RUN pip install ")
        and "--require-hashes" in line
        and line.endswith("-r requirements-docker.txt")
        for line in lines
    )
    assert "USER 10001" in lines and "EXPOSE 8000" in lines
    assert lines.index("USER 10001") > next(i for i, x in enumerate(lines) if x.startswith("RUN"))
    command = json.loads(lines[-1].removeprefix("CMD "))
    assert command[0] == "gunicorn" and command[-1] == "app:create_app()"
    options = dict(zip(command[1:-1:2], command[2:-1:2], strict=True))
    assert options["--bind"] == "0.0.0.0:8000"
    assert options["--worker-tmp-dir"] == "/tmp"
    # a sync worker is killed at the timeout; a gthread worker's requests are not, and the timeout
    # is above the app's deadline anyway
    assert options["--worker-class"] == "gthread"
    deadline = SAMPLE_RESOLVED["agent"]["limits"]["deadline_s"]
    assert int(options["--timeout"]) > deadline


@pytest.mark.parametrize("deadline", [60, 300])
def test_the_gunicorn_timeout_follows_the_releases_deadline(deadline: int) -> None:
    agent = SAMPLE_RESOLVED["agent"]
    resolved = {
        **SAMPLE_RESOLVED,
        "agent": {**agent, "limits": {**agent["limits"], "deadline_s": deadline}},
    }
    text = render(DOCKER_MANIFEST, DOCKER_DIGEST, resolved, VALUES)["Dockerfile"].decode("utf-8")
    command = json.loads(text.splitlines()[-1].removeprefix("CMD "))
    assert int(command[command.index("--timeout") + 1]) > deadline


def test_the_bundle_has_no_seth_origin_or_credential_pattern() -> None:
    loopback = re.escape("127.0.0.1")
    for name, content in _render().items():
        text = name + "\n" + content.decode("utf-8")
        # only compose.yaml names the loopback address: its port and its health probe
        hits = [
            rx.pattern
            for rx in FORBIDDEN
            if rx.search(text) and not (name == "compose.yaml" and rx.pattern == loopback)
        ]
        assert hits == [], (name, hits)
        assert "CLIENT_SECRET" not in text


# ------------------------------------------------------------------ lock
def _pins(name: str) -> dict[str, set[str]]:
    """`name==version ; marker` -> its hashes, of a compiled lock."""
    pins: dict[str, set[str]] = {}
    current = ""
    for line in TEMPLATES.joinpath(name).read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.strip().startswith("#"):
            continue
        if line.startswith("    --hash="):
            pins[current].add(line.strip().removesuffix("\\").strip())
        elif line.strip():
            current = line.removesuffix("\\").strip()
            pins[current] = set()
    return pins


def test_the_docker_lock_is_the_apps_lock_plus_gunicorn() -> None:
    app, docker = _pins("requirements.txt"), _pins("requirements-docker.txt")
    assert all(hashes for hashes in docker.values())  # a hash on every pin
    assert {pin: docker.get(pin) for pin in app} == app
    added = {pin.split("==")[0] for pin in set(docker) - set(app)}
    assert added == {"gunicorn", "packaging"}  # packaging: gunicorn's one dependency
    assert "gunicorn==23.0.0" in docker
    lines = TEMPLATES.joinpath("requirements-docker.in").read_text(encoding="utf-8").split("\n")
    assert [line for line in lines if line] == ["-r requirements.in", "gunicorn==23.0.0"]
