"""Tenant, target and host isolation of the `docker.compose/v1` adapter (design SP8 section 6).

The project, port, image and daemon are derived from the artifact and the target, and the compose
project, image and env file are named after the target id, so two clients' targets on one daemon
never share a name whatever display name they chose; every input of another tenant or target is
refused with `PermissionError` before any runtime write, a project holding a container not labelled
for the target is `foreign_project`, and another daemon is `docker_host_mismatch`.
"""

from __future__ import annotations

from typing import Any  # Any: plan detail values
from uuid import uuid4

import pytest

from shifde.adapters.native.base import (
    DeployContext,
    InMemoryJournal,
    ReadContext,
    read_archive,
)
from shifde.adapters.native.contracts import (
    ApplyRequest,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    Target,
    VerifyRequest,
)
from shifde.adapters.native.docker.fake import FakeContainer, FakeContainerRuntime
from shifde.adapters.native.docker.runtime import DockerRefused
from shifde.adapters.native.docker.source import TARGET_LABEL, env_file
from shifde.adapters.native.planning import bindings_digest, seal_plan
from tests.adapters.native.docker_bundle import (
    DAEMON,
    DockerSetup,
    edit_compose,
    make_docker_setup,
)


@pytest.fixture(autouse=True)
def _test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIFDE_ENV", "test")


@pytest.fixture
def runtime() -> FakeContainerRuntime:
    return FakeContainerRuntime(DAEMON, hang_timeout=0.05)


@pytest.fixture
def a(runtime: FakeContainerRuntime) -> DockerSetup:
    return make_docker_setup(runtime, project="shop", port=8701)


@pytest.fixture
def b(runtime: FakeContainerRuntime) -> DockerSetup:
    """Client B's target on the same daemon, with the same display project name as A's."""
    return make_docker_setup(runtime, project="shop", port=8702)


def _writes(runtime: FakeContainerRuntime) -> list[tuple[str, str]]:
    return [action for action in runtime.actions if action[0] in ("build", "compose-up")]


def test_docker_artifact_of_another_tenant_is_refused(
    a: DockerSetup, b: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, _ = a.prepare()
    with pytest.raises(PermissionError):
        b.adapter.plan(PlanRequest(artifact=artifact), b.read)
    with pytest.raises(PermissionError):
        b.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), b.deploy_context())
    assert _writes(runtime) == []


def test_docker_artifact_and_plan_of_another_target_are_refused(
    a: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    # A second target of the same tenant with the same bindings: the bundle names A's target.
    target = Target(tenant_id=a.read.tenant_id, host=a.target.host, bindings=a.target.bindings)
    read = ReadContext(tenant_id=a.read.tenant_id, target=target)
    operation_id = uuid4()
    deploy = DeployContext(
        tenant_id=a.read.tenant_id,
        target=target,
        operation_id=operation_id,
        artifacts=a.build.artifacts,
        journal=InMemoryJournal(a.read.tenant_id, operation_id),
        toolchain_digest=a.build.toolchain_digest,
    )
    artifact, plan, _ = a.prepare()
    with pytest.raises(PermissionError):
        a.adapter.plan(PlanRequest(artifact=artifact), read)
    with pytest.raises(PermissionError):
        a.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    moved = seal_plan(
        plan.model_copy(update={"target_id": target.id, "bindings_digest": bindings_digest(target)})
    )
    with pytest.raises(PermissionError):
        a.adapter.apply(ApplyRequest(artifact=artifact, plan=moved), deploy)
    assert _writes(runtime) == []


def test_docker_receipt_and_plan_of_another_tenant_are_refused(
    a: DockerSetup, b: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, deploy = a.prepare()
    receipt = a.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    writes = list(_writes(runtime))
    with pytest.raises(PermissionError):
        b.adapter.observe(ObserveRequest(receipt=receipt), b.read)
    with pytest.raises(PermissionError):
        b.adapter.verify(VerifyRequest(receipt=receipt), b.read)
    with pytest.raises(PermissionError):
        b.adapter.reconcile(ReconcileRequest(plan=plan), b.read)
    own_artifact = b.artifact()
    with pytest.raises(PermissionError):
        b.adapter.apply(ApplyRequest(artifact=own_artifact, plan=plan), b.deploy_context())
    assert _writes(runtime) == writes


@pytest.mark.parametrize(
    ("key", "value"),
    [("project", "other"), ("port", 8799), ("daemon_id", "9f0e1d2c-3b4a-4596-8778-695a4b3c2d1e")],
)
def test_docker_forged_plan_details_are_refused_at_apply(
    a: DockerSetup, runtime: FakeContainerRuntime, key: str, value: Any
) -> None:
    artifact, plan, deploy = a.prepare()
    forged = seal_plan(plan.model_copy(update={"details": {**plan.details, key: value}}))
    with pytest.raises(PermissionError):
        a.adapter.apply(ApplyRequest(artifact=artifact, plan=forged), deploy)
    assert runtime.actions == []


def test_docker_inspect_refuses_another_project_name(a: DockerSetup) -> None:
    with pytest.raises(PermissionError):
        a.adapter.inspect(InspectRequest(application_id=uuid4(), app_name="other"), a.read)


def test_docker_cross_client_same_display_name_never_shares_names(
    a: DockerSetup, b: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    # B deploys first under the display name A also chose.
    assert a.target.bindings["project"] == b.target.bindings["project"] == "shop"
    b_artifact, b_plan, b_deploy = b.prepare()
    b_receipt = b.adapter.apply(ApplyRequest(artifact=b_artifact, plan=b_plan), b_deploy)
    a_artifact, a_plan, a_deploy = a.prepare()
    a_receipt = a.adapter.apply(ApplyRequest(artifact=a_artifact, plan=a_plan), a_deploy)
    assert b_plan.details["project"] == f"shifde-{b.target.id.hex}"
    assert a_plan.details["project"] == f"shifde-{a.target.id.hex}"
    for key in ("project", "image"):
        assert b_plan.details[key] != a_plan.details[key]
        assert "shop" not in str(b_plan.details[key])
    b_files = read_archive(b.build.artifacts.read(b_artifact.archive, b.read.tenant_id))
    assert env_file(b.project).encode() in b_files["compose.yaml"]
    assert a.project.encode() not in b_files["compose.yaml"]
    # Each client sees only its own container; B's verify reached B's app, not A's.
    assert set(runtime.projects) == {a.project, b.project}
    assert runtime.projects[b.project].image == b_plan.details["image"]
    assert runtime.projects[b.project].labels[TARGET_LABEL] == str(b.target.id)
    assert runtime.projects[a.project].labels[TARGET_LABEL] == str(a.target.id)
    assert b.adapter.verify(VerifyRequest(receipt=b_receipt), b.read).passed
    assert a.adapter.verify(VerifyRequest(receipt=a_receipt), a.read).passed
    with pytest.raises(ConnectionError):
        runtime.http_get_local(a.project, b.port, "/health")


def test_docker_bundle_naming_another_targets_env_file_is_refused(
    a: DockerSetup, b: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    files = b.files()
    edit_compose(files, lambda service: service.update(env_file=[env_file(a.project)]))
    artifact = b.artifact(files)
    with pytest.raises(ValueError, match="env_file"):
        b.adapter.plan(PlanRequest(artifact=artifact), b.read)
    assert runtime.actions == []


def test_docker_project_with_another_targets_container_is_foreign(
    a: DockerSetup, b: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    b_artifact, b_plan, b_deploy = b.prepare()  # planned while B's project is still empty
    # A container labelled for A's target turns up in B's project (started outside SETH).
    runtime.projects[b.project] = FakeContainer(
        name=f"{b.project}-app-1",
        image="other:latest",
        state="running",
        health="healthy",
        port=8799,
        labels={TARGET_LABEL: str(a.target.id)},
    )
    before = runtime.compose_ps(b.project)
    writes = list(_writes(runtime))
    with pytest.raises(DockerRefused) as refused:
        b.adapter.plan(PlanRequest(artifact=b_artifact), b.read)
    assert refused.value.code == "foreign_project"
    with pytest.raises(DockerRefused) as refused:
        b.adapter.apply(ApplyRequest(artifact=b_artifact, plan=b_plan), b_deploy)
    assert refused.value.code == "foreign_project"
    with pytest.raises(DockerRefused) as refused:
        b.adapter.inspect(InspectRequest(application_id=uuid4(), app_name=b.project), b.read)
    assert refused.value.code == "foreign_project"
    assert _writes(runtime) == writes
    assert runtime.compose_ps(b.project) == before


def test_docker_observe_checks_the_receipts_daemon_and_project(
    a: DockerSetup, runtime: FakeContainerRuntime
) -> None:
    artifact, plan, deploy = a.prepare()
    receipt = a.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    handles = receipt.operation_handles
    other_daemon = receipt.model_copy(
        update={"operation_handles": {**handles, "daemon_id": str(uuid4())}}
    )
    with pytest.raises(DockerRefused) as refused:
        a.adapter.observe(ObserveRequest(receipt=other_daemon), a.read)
    assert refused.value.code == "docker_host_mismatch"
    other_project = receipt.model_copy(
        update={"operation_handles": {**handles, "project": f"shifde-{uuid4().hex}"}}
    )
    with pytest.raises(PermissionError, match="compose project"):
        a.adapter.observe(ObserveRequest(receipt=other_project), a.read)
    with pytest.raises(PermissionError):
        a.adapter.verify(VerifyRequest(receipt=other_project), a.read)
    runtime.daemon_mismatch = True
    with pytest.raises(DockerRefused) as refused:
        a.adapter.observe(ObserveRequest(receipt=receipt), a.read)
    assert refused.value.code == "docker_host_mismatch"


def test_docker_target_on_another_daemon_is_a_host_mismatch(
    runtime: FakeContainerRuntime,
) -> None:
    elsewhere = make_docker_setup(runtime, daemon=str(uuid4()))
    artifact = elsewhere.artifact()
    with pytest.raises(DockerRefused) as refused:
        elsewhere.adapter.plan(PlanRequest(artifact=artifact), elsewhere.read)
    assert refused.value.code == "docker_host_mismatch"
    with pytest.raises(DockerRefused) as refused:
        elsewhere.adapter.inspect(
            InspectRequest(application_id=uuid4(), app_name=elsewhere.project), elsewhere.read
        )
    assert refused.value.code == "docker_host_mismatch"
    assert runtime.actions == []
