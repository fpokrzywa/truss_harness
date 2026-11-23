"""The `docker.compose/v1` adapter over `FakeContainerRuntime`: the happy path, steps, journal,
every fault, verify, bundle validation and the contract changes (design SP8 section 6)."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: compose service values
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shifde.adapters.native.base import InMemoryJournal, NeedsAttention, create_archive
from shifde.adapters.native.contracts import (
    ApplyRequest,
    BuildArtifact,
    DatabricksApplicationSpec,
    DockerComposeSpec,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ScaffoldRequest,
    Target,
    ValidateRequest,
    VerifyRequest,
)
from shifde.adapters.native.docker import LIVE_VALIDATED, MATURITY
from shifde.adapters.native.docker.fake import FakeContainerRuntime
from shifde.adapters.native.docker.runtime import DockerRefused
from shifde.adapters.native.docker.source import BASE_IMAGE, RELEASE_LABEL, TARGET_LABEL
from tests.adapters.native.conftest import Setup
from tests.adapters.native.docker_bundle import (
    DAEMON,
    LEGACY_DAEMON,
    MARKER,
    RELEASE,
    DockerSetup,
    edit_compose,
    make_docker_setup,
)

SECOND = "2" * 64


@pytest.fixture
def runtime() -> FakeContainerRuntime:
    return FakeContainerRuntime(DAEMON, hang_timeout=0.05)


@pytest.fixture
def docker(runtime: FakeContainerRuntime) -> DockerSetup:
    return make_docker_setup(runtime)


def _deploy(docker: DockerSetup, release_digest: str = RELEASE, marker: str = MARKER) -> Any:
    artifact, plan, deploy = docker.prepare(release_digest, marker)
    receipt = docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    return artifact, plan, deploy, receipt


def _actions(runtime: FakeContainerRuntime, kind: str) -> list[tuple[str, str]]:
    return [action for action in runtime.actions if action[0] == kind]


# --------------------------------------------------------------------------- happy path
def test_docker_happy_path_runs_the_staged_app(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    marker = f"marker-{uuid4().hex}"
    artifact, plan, deploy = docker.prepare(marker=marker)
    assert [step.key for step in plan.steps] == ["image-build", "compose-up", "container-ready"]
    assert [step.action for step in plan.steps] == ["create", "create", "start"]
    assert plan.details == {
        "project": "shop",
        "port": 8701,
        "image": f"shifde-shop:{RELEASE[:12]}",
        "release_digest": RELEASE,
        "daemon_id": DAEMON,
    }
    receipt = docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert receipt.operation_handles == {
        "project": "shop",
        "image": f"shifde-shop:{RELEASE[:12]}",
        "port": "8701",
        "release_digest": RELEASE,
    }
    assert [(r.resource_type, r.native_id) for r in receipt.resources] == [
        ("docker.compose_project", "shop")
    ]
    journal = deploy.journal
    assert isinstance(journal, InMemoryJournal)
    assert list(journal.steps) == ["image-build", "image-verify", "compose-up", "container-ready"]
    assert all(entry.state == "complete" for entry in journal.steps.values())
    assert docker.adapter.observe(ObserveRequest(receipt=receipt), docker.read).state == (
        "succeeded"
    )
    report = docker.adapter.verify(VerifyRequest(receipt=receipt), docker.read)
    assert report.passed
    assert [(c.name, c.status) for c in report.checks] == [
        ("native-version", "passed"),
        ("runtime-health", "passed"),
        ("app-deep-health", "passed"),
    ]
    status, body = runtime.http_get_local(8701, "/health?deep=1")
    assert status == 200
    assert json.loads(body)["generator_version"] == marker  # executed from the staged app.py
    assert json.loads(body)["release_digest"] == RELEASE


def test_docker_completed_apply_returns_the_saved_receipt(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, deploy, receipt = _deploy(docker)
    again = docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert again == receipt
    assert len(_actions(runtime, "build")) == 1
    assert len(_actions(runtime, "compose-up")) == 1


def test_docker_image_holds_the_artifact_bytes(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    files = docker.files()
    _deploy(docker)
    image = runtime.images[f"shifde-shop:{RELEASE[:12]}"]
    assert image.dockerfile == files["Dockerfile"]
    assert image.compose == files["compose.yaml"]
    assert image.labels[RELEASE_LABEL] == RELEASE


def _stage(files: Mapping[str, bytes], directory: Path) -> dict[str, str]:
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return {
        name: hashlib.sha256(files[name]).hexdigest() for name in ("Dockerfile", "compose.yaml")
    }


def test_docker_build_refuses_altered_staged_files(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    files = docker.files()
    tag = f"shifde-shop:{RELEASE[:12]}"
    with tempfile.TemporaryDirectory(prefix="shifde-docker-") as temporary:
        directory = Path(temporary)
        expected = _stage(files, directory)
        (directory / "Dockerfile").write_bytes(files["Dockerfile"] + b"RUN echo altered\n")
        with pytest.raises(ValueError, match="differ"):
            runtime.build(directory, tag=tag, expected_sha256=expected)
        _stage(files, directory)
        with pytest.raises(ValueError, match="differ"):
            runtime.build(
                directory, tag=tag, expected_sha256={**expected, "compose.yaml": "0" * 64}
            )
    assert runtime.images == {}


def test_docker_second_release_replaces_the_container(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    _deploy(docker)
    artifact, plan, deploy = docker.prepare(SECOND)
    assert [step.action for step in plan.steps] == ["create", "update", "start"]
    receipt = docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    containers = runtime.compose_ps("shop")
    assert len(containers) == 1
    assert containers[0].image == f"shifde-shop:{SECOND[:12]}"
    report = docker.adapter.verify(VerifyRequest(receipt=receipt), docker.read)
    assert report.passed
    status, body = runtime.http_get_local(8701, "/health?deep=1")
    assert json.loads(body)["release_digest"] == SECOND


def test_docker_prepare_app_hook_reaches_the_staged_module(docker: DockerSetup) -> None:
    seen: list[Mapping[str, str]] = []

    def prepare(module: ModuleType, environment: Mapping[str, str]) -> None:
        seen.append(environment)
        module.WORKSPACE = lambda: (False, "warehouse-123 is unreachable")  # type: ignore[attr-defined]

    runtime = FakeContainerRuntime(DAEMON, prepare_app=prepare)
    setup = make_docker_setup(runtime)
    _, _, _, receipt = _deploy(setup)
    report = setup.adapter.verify(VerifyRequest(receipt=receipt), setup.read)
    deep = report.checks[2]
    assert (deep.name, deep.status) == ("app-deep-health", "failed")
    assert "warehouse-123 is unreachable" in deep.evidence
    assert seen[-1] == {
        "DATABRICKS_WAREHOUSE_ID": "warehouse-123",
        "SERVING_ENDPOINT": "endpoint-1",
    }


# --------------------------------------------------------------------------- faults
@pytest.mark.parametrize("step", ["image-build", "compose-up"])
def test_docker_fail_after_accept_is_not_replayed(
    docker: DockerSetup, runtime: FakeContainerRuntime, step: str
) -> None:
    artifact, plan, deploy = docker.prepare()
    runtime.fail_after_accept = step
    with pytest.raises(TimeoutError):
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    before = list(runtime.actions)
    runtime.fail_after_accept = None
    with pytest.raises(NeedsAttention):
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert runtime.actions == before
    assert docker.adapter.reconcile(ReconcileRequest(plan=plan), docker.read).state == "ambiguous"


def test_docker_build_failed_stops_before_compose_up(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, deploy = docker.prepare()
    runtime.build_failed = True
    with pytest.raises(RuntimeError):
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert runtime.images == {}
    assert _actions(runtime, "compose-up") == []


def test_docker_unhealthy_container_fails_observe_and_verify(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    runtime.unhealthy = True
    _, _, _, receipt = _deploy(docker)
    assert docker.adapter.observe(ObserveRequest(receipt=receipt), docker.read).state == "failed"
    report = docker.adapter.verify(VerifyRequest(receipt=receipt), docker.read)
    assert not report.passed
    assert [c.status for c in report.checks] == ["failed", "skipped", "skipped"]


def test_docker_starting_container_is_running(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    _, _, _, receipt = _deploy(docker)
    runtime.projects["shop"].health = "starting"
    assert docker.adapter.observe(ObserveRequest(receipt=receipt), docker.read).state == "running"


def test_docker_container_without_health_status_is_unknown(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    _, _, _, receipt = _deploy(docker)
    runtime.projects["shop"].health = None
    observed = docker.adapter.observe(ObserveRequest(receipt=receipt), docker.read)
    assert observed.state == "unknown"
    assert not docker.adapter.verify(VerifyRequest(receipt=receipt), docker.read).passed


def test_docker_daemon_mismatch_refuses_plan_and_apply(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, deploy = docker.prepare()
    runtime.daemon_mismatch = True
    with pytest.raises(DockerRefused) as refused:
        docker.adapter.plan(PlanRequest(artifact=artifact), docker.read)
    assert refused.value.code == "docker_host_mismatch"
    with pytest.raises(DockerRefused) as refused:
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert refused.value.code == "docker_host_mismatch"
    assert runtime.actions == []


def test_docker_bad_deep_health_fails_verify_with_the_reason(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    _, _, _, receipt = _deploy(docker)
    runtime.deep_health_bad = True
    report = docker.adapter.verify(VerifyRequest(receipt=receipt), docker.read)
    assert not report.passed
    deep = report.checks[2]
    assert (deep.name, deep.status) == ("app-deep-health", "failed")
    assert "warehouse unreachable" in deep.evidence
    assert report.checks[1].status == "passed"


def test_docker_hang_times_out_then_needs_attention(
    docker: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, deploy = docker.prepare()
    runtime.hang = True
    with pytest.raises(TimeoutError):
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    runtime.hang = False
    with pytest.raises(NeedsAttention):
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(_actions(runtime, "build")) == 1


def test_docker_image_verify_stops_before_compose_up(
    docker: DockerSetup, runtime: FakeContainerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, plan, deploy = docker.prepare()
    monkeypatch.setattr(runtime, "image_labels", lambda tag: {RELEASE_LABEL: SECOND})
    with pytest.raises(ValueError, match="release digest"):
        docker.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert _actions(runtime, "compose-up") == []


# --------------------------------------------------------------------------- bundle validation
def _validate(docker: DockerSetup, files: dict[str, bytes]) -> bool:
    source = docker.build.artifacts.put(docker.build.tenant_id, create_archive(files))
    report = docker.adapter.validate(
        ValidateRequest(spec=DockerComposeSpec(), source=source), docker.build
    )
    return report.passed


def _dockerfile(edit: Callable[[str], str]) -> Callable[[dict[str, bytes]], None]:
    def apply(files: dict[str, bytes]) -> None:
        files["Dockerfile"] = edit(files["Dockerfile"].decode()).encode()

    return apply


def _service(edit: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, bytes]], None]:
    return lambda files: edit_compose(files, edit)


def _second_service(files: dict[str, bytes]) -> None:
    files["compose.yaml"] += b"  other:\n    image: busybox\n"


def _top_level(files: dict[str, bytes]) -> None:
    files["compose.yaml"] += b"volumes:\n  data: {}\n"


REFUSED: dict[str, Callable[[dict[str, bytes]], None]] = {
    "other-from": _dockerfile(lambda text: text.replace(BASE_IMAGE, "python:3.12-slim")),
    "second-from": _dockerfile(lambda text: text + f"FROM {BASE_IMAGE}\n"),
    "add": _dockerfile(lambda text: text + "ADD https://example.com/x /x\n"),
    "run-mount": _dockerfile(lambda text: text + "RUN --mount=type=secret,id=k cat /k\n"),
    "syntax": _dockerfile(lambda text: "# syntax=docker/dockerfile:1\n" + text),
    "no-label": _dockerfile(lambda text: text.replace(RELEASE_LABEL, "org.example.other")),
    "continuation-comment-mount": _dockerfile(
        lambda text: text + "RUN \\\n# c\n--mount=type=bind,from=alpine,target=/x true\n"
    ),
    "continuation-blank-network": _dockerfile(
        lambda text: text + "RUN \\\n\n--network=host true\n"
    ),
    "copy-from": _dockerfile(lambda text: text + "COPY --from=alpine /bin/sh /x\n"),
    "unknown-instruction": _dockerfile(lambda text: text + "ONBUILD RUN true\n"),
    "heredoc": _dockerfile(lambda text: text + "RUN <<EOF\ntrue\nEOF\n"),
    "privileged": _service(lambda s: s.update(privileged=True)),
    "network-host": _service(lambda s: s.update(network_mode="host")),
    "volumes": _service(lambda s: s.update(volumes=["/:/host"])),
    "build": _service(lambda s: s.update(build=".")),
    "cap-add": _service(lambda s: s.update(cap_add=["SYS_ADMIN"])),
    "post-start": _service(lambda s: s.update(post_start=[{"command": "sh"}])),
    "writable": _service(lambda s: s.update(read_only=False)),
    "security-opt-extra": _service(lambda s: s["security_opt"].append("seccomp=unconfined")),
    "tmpfs-other": _service(lambda s: s.update(tmpfs=["/tmp", "/run"])),
    "extra-environment": _service(lambda s: s["environment"].update(DATABRICKS_TOKEN="x")),
    "second-service": _second_service,
    "top-level": _top_level,
    "hidden-file": lambda files: files.update({"app/.env": b"X=1"}),
    "outside-file": lambda files: files.update({"setup.sh": b"echo"}),
    "bad-release": lambda files: files.update({"release.json": b'{"digest": "nope"}'}),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_docker_bundle_validation_refuses(docker: DockerSetup, case: str) -> None:
    files = docker.files()
    REFUSED[case](files)
    assert not _validate(docker, files)
    with pytest.raises(ValueError, match="validation failed"):
        docker.artifact(files)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("continuation-comment-mount", "dockerfile_run_flag"),
        ("continuation-blank-network", "dockerfile_run_flag"),
        ("copy-from", "dockerfile_copy_from"),
        ("unknown-instruction", "dockerfile_instruction"),
        ("heredoc", "dockerfile_heredoc"),
        ("security-opt-extra", "compose_hardening"),
        ("tmpfs-other", "compose_tmpfs"),
    ],
)
def test_docker_bundle_refusal_names_the_rule(docker: DockerSetup, case: str, code: str) -> None:
    files = docker.files()
    REFUSED[case](files)
    source = docker.build.artifacts.put(docker.build.tenant_id, create_archive(files))
    report = docker.adapter.validate(
        ValidateRequest(spec=DockerComposeSpec(), source=source), docker.build
    )
    assert [d.code for d in report.diagnostics] == [code]


TARGET_REFUSED: dict[str, Callable[[dict[str, Any]], None]] = {
    "ports-all-interfaces": lambda s: s.update(ports=["0.0.0.0:8701:8000"]),
    "ports-other": lambda s: s.update(ports=["127.0.0.1:8702:8000"]),
    "env-file-path": lambda s: s.update(env_file=["/etc/shifde-apps/other.env"]),
    "image": lambda s: s.update(image="shifde-shop:latest"),
}


@pytest.mark.parametrize("case", sorted(TARGET_REFUSED))
def test_docker_target_rules_refuse_at_plan(
    docker: DockerSetup, runtime: FakeContainerRuntime, case: str
) -> None:
    files = docker.files()
    edit_compose(files, TARGET_REFUSED[case])
    assert _validate(docker, files)  # target-free rules pass
    artifact = docker.artifact(files)
    with pytest.raises(ValueError):
        docker.adapter.plan(PlanRequest(artifact=artifact), docker.read)
    assert runtime.actions == []


def test_docker_env_file_as_a_single_string_is_accepted(docker: DockerSetup) -> None:
    files = docker.files()
    edit_compose(files, lambda s: s.update(env_file="/etc/shifde-apps/shop.env"))
    docker.adapter.plan(PlanRequest(artifact=docker.artifact(files)), docker.read)


def test_docker_valid_bundle_passes_validation(docker: DockerSetup) -> None:
    assert _validate(docker, docker.files())


def test_docker_continuation_comments_parse_as_docker_does(docker: DockerSetup) -> None:
    files = docker.files()
    files["Dockerfile"] += b"RUN echo a \\\n# a note\n\n    && echo b\n"
    edit_compose(files, lambda s: s.update(tmpfs="/tmp"))
    assert _validate(docker, files)


def test_docker_scaffold_is_refused(docker: DockerSetup) -> None:
    with pytest.raises(ValueError, match="generator"):
        docker.adapter.scaffold(ScaffoldRequest(spec=DatabricksApplicationSpec()), docker.build)


def test_docker_validate_refuses_a_databricks_spec(docker: DockerSetup) -> None:
    source = docker.build.artifacts.put(docker.build.tenant_id, create_archive(docker.files()))
    report = docker.adapter.validate(
        ValidateRequest(spec=DatabricksApplicationSpec(), source=source), docker.build
    )
    assert not report.passed
    assert report.diagnostics[0].code == "unsupported_spec"


# --------------------------------------------------------------------------- capabilities, inspect
def test_docker_integration_is_experimental_and_not_live_validated(docker: DockerSetup) -> None:
    assert MATURITY == "experimental"
    assert LIVE_VALIDATED is False
    report = docker.adapter.capabilities(docker.read)
    assert report.maturity == "experimental"
    assert report.application_kinds == ["docker.compose/v1"]
    assert report.supports("runtime")


def test_docker_capabilities_unknown_when_the_daemon_fails(docker: DockerSetup) -> None:
    def broken() -> str:
        raise RuntimeError("Docker command failed with exit code 1")

    docker.runtime.daemon_id = broken  # type: ignore[method-assign]
    assert not docker.adapter.capabilities(docker.read).supports("runtime")


def test_docker_inspect_fingerprints_the_project(docker: DockerSetup) -> None:
    request = InspectRequest(application_id=uuid4(), app_name="shop")
    empty = docker.adapter.inspect(request, docker.read)
    assert empty.resources == []
    _deploy(docker)
    snapshot = docker.adapter.inspect(request, docker.read)
    assert [r.native_id for r in snapshot.resources] == ["shop"]
    assert snapshot.fingerprint != empty.fingerprint
    assert docker.runtime.projects["shop"].labels[TARGET_LABEL] == str(docker.target.id)


# --------------------------------------------------------------------------- contracts
@pytest.mark.parametrize("daemon", [DAEMON, LEGACY_DAEMON])
def test_docker_target_host_accepts_daemon_ids(daemon: str) -> None:
    assert Target(tenant_id=uuid4(), host=f"docker://{daemon}").host == f"docker://{daemon}"


@pytest.mark.parametrize(
    "host",
    [
        "docker://",
        "docker://not-a-daemon",
        f"docker://{DAEMON.upper()}",
        f"docker://{DAEMON}/",
        f"docker://{LEGACY_DAEMON.lower()}",
        "tcp://127.0.0.1:2375",
        "http://workspace.example",
    ],
)
def test_docker_target_host_refuses_other_forms(host: str) -> None:
    with pytest.raises(ValidationError):
        Target(tenant_id=uuid4(), host=host)


def test_docker_union_reloads_each_spec_by_kind(setup: Setup) -> None:
    artifact = setup.artifact()
    reloaded = BuildArtifact.model_validate_json(artifact.model_dump_json())
    assert isinstance(reloaded.spec, DatabricksApplicationSpec)
    docker = artifact.model_copy(update={"spec": DockerComposeSpec()})
    assert isinstance(
        BuildArtifact.model_validate_json(docker.model_dump_json()).spec, DockerComposeSpec
    )


def test_docker_spec_artifact_is_refused_by_the_databricks_adapter(setup: Setup) -> None:
    artifact = setup.artifact().model_copy(update={"spec": DockerComposeSpec()})
    with pytest.raises(ValueError, match="Databricks"):
        setup.adapter.plan(PlanRequest(artifact=artifact), setup.read)
    with pytest.raises(ValueError, match="Databricks"):
        setup.adapter.validate(
            ValidateRequest(spec=DockerComposeSpec(), source=artifact.archive), setup.build
        )
    assert setup.platform.actions == []
