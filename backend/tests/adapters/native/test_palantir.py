"""The Palantir Marketplace adapter on `FakeFoundryStack` (ported from the SDK's palantir tests).

Every write is attempted once, each accepted RID is checkpointed before the next request, an
Ontology override is refused at plan, apply and observe, and every cross-tenant or cross-target
input is refused with `PermissionError` before any write. No database is used.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any  # Any: request and receipt values
from uuid import UUID, uuid4

import httpx
import pytest

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    NeedsAttention,
    ReadContext,
)
from shifde.adapters.native.contracts import (
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    DatabricksApplicationSpec,
    DeploymentPlan,
    FoundryMarketplaceSpec,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ResourceBinding,
    ScaffoldRequest,
    Target,
    ValidateRequest,
    VerifyRequest,
    digest,
)
from shifde.adapters.native.databricks.adapter import DatabricksAdapter
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.adapters.native.palantir import LIVE_VALIDATED, MATURITY
from shifde.adapters.native.palantir.adapter import PalantirMarketplaceAdapter
from shifde.adapters.native.palantir.clients import INSTALLATIONS, JOBS
from shifde.adapters.native.palantir.fake import FakeFoundryStack
from shifde.adapters.native.palantir.receipts import job_payload
from shifde.adapters.native.palantir.source import (
    DELIVERY_SPEC,
    TOOLCHAIN_DIGEST,
    Delivery,
    delivery_files,
    files_for,
    installation_suffix,
    native_id,
    read_delivery,
    validate_source,
)
from shifde.adapters.native.planning import bindings_digest, seal_plan
from tests.adapters.native.origins import NON_PUBLIC_ORIGINS

SPEC = FoundryMarketplaceSpec(frontend_asset_path="assets/health.txt")
HOST = "https://enrollment.example.test"
OTHER_HOST = "https://other-enrollment.example.test"


def make_stack(host: str = HOST) -> FakeFoundryStack:
    return FakeFoundryStack(
        environment="test",
        host=host,
        frontend_origin="https://native-app.example.test",
        ontology_rid=f"ri.ontology.main.ontology.{uuid4()}",
        hang_timeout=0.05,
    )


def make_bindings(stack: FakeFoundryStack, **overrides: str) -> dict[str, str]:
    return {
        "marketplace_rid": f"ri.marketplace.main.local.{uuid4()}",
        "product_id": str(uuid4()),
        "version_id": str(uuid4()),
        "namespace_rid": stack.namespace_rid,
        "parent_folder_rid": f"ri.compass.main.folder.{uuid4()}",
        "ontology_rid": stack.ontology_rid,
        "object_type": stack.object_type,
        "frontend_origin": stack.frontend_origin,
        "frontend_asset_sha256": stack.asset_sha256,
        "allow_preview_apis": "true",
        **overrides,
    }


@dataclass
class Stage:
    adapter: PalantirMarketplaceAdapter
    stack: FakeFoundryStack
    build: BuildContext
    read: ReadContext

    @property
    def posts(self) -> int:
        return sum(1 for method, path, _ in self.stack.requests if method == "POST")

    def deploy_context(self, journal: InMemoryJournal | None = None) -> DeployContext:
        operation_id = journal.operation_id if journal else uuid4()
        return DeployContext(
            tenant_id=self.read.tenant_id,
            target=self.read.target,
            owned_resources=self.read.owned_resources,
            operation_id=operation_id,
            artifacts=self.build.artifacts,
            journal=journal or InMemoryJournal(self.read.tenant_id, operation_id),
            toolchain_digest=TOOLCHAIN_DIGEST,
        )

    def artifact(self, application_id: UUID | None = None) -> BuildArtifact:
        source = self.adapter.scaffold(ScaffoldRequest(spec=SPEC), self.build)
        return self.adapter.build(
            BuildRequest(spec=SPEC, source=source, application_id=application_id or uuid4()),
            self.build,
        )

    def prepare(self) -> tuple[BuildArtifact, DeploymentPlan, DeployContext]:
        artifact = self.artifact()
        return (
            artifact,
            self.adapter.plan(PlanRequest(artifact=artifact), self.read),
            (self.deploy_context()),
        )

    def receipt(self) -> tuple[BuildArtifact, ApplyReceipt]:
        artifact, plan, deploy = self.prepare()
        return artifact, self.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)

    def with_target(self, **bindings: str) -> Stage:
        target = self.read.target.model_copy(
            update={"bindings": {**self.read.target.bindings, **bindings}}
        )
        return replace(self, read=ReadContext(tenant_id=self.read.tenant_id, target=target))

    def owning(self, resources: list[ResourceBinding]) -> Stage:
        return replace(self, read=replace(self.read, owned_resources=tuple(resources)))


def make_stage(stack: FakeFoundryStack, **overrides: str) -> Stage:
    tenant = uuid4()
    target = Target(tenant_id=tenant, host=stack.host, bindings=make_bindings(stack, **overrides))
    build = BuildContext(
        tenant_id=tenant, artifacts=InMemoryArtifactStore(), toolchain_digest=TOOLCHAIN_DIGEST
    )
    adapter = PalantirMarketplaceAdapter(client_factory=stack.client_factory)
    return Stage(adapter, stack, build, ReadContext(tenant_id=tenant, target=target))


@pytest.fixture
def stage() -> Stage:
    return make_stage(make_stack())


@pytest.fixture
def other() -> Stage:
    """A second tenant on another Foundry stack."""
    return make_stage(make_stack(OTHER_HOST))


# --------------------------------------------------------------------------- package and build
def test_palantir_is_experimental_and_not_live_validated(stage: Stage) -> None:
    assert MATURITY == "experimental"
    assert LIVE_VALIDATED is False
    report = stage.adapter.capabilities(stage.read)
    assert report.maturity == "experimental"
    assert report.application_kinds == ["palantir.marketplace/v1"]
    (deployment,) = report.capabilities
    assert (deployment.operation, deployment.status) == ("deployment", "unknown")


def test_build_is_offline_and_the_descriptor_holds_only_the_spec(stage: Stage) -> None:
    artifact = stage.artifact()
    assert stage.stack.requests == []
    assert artifact.spec == SPEC and artifact.toolchain_digest == TOOLCHAIN_DIGEST
    assert set(files_for(SPEC)) == {"deployment.json"}
    report = stage.adapter.validate(
        ValidateRequest(spec=SPEC, source=artifact.archive), stage.build
    )
    assert report.passed


def test_build_refuses_another_toolchain(stage: Stage) -> None:
    source = stage.adapter.scaffold(ScaffoldRequest(spec=SPEC), stage.build)
    ctx = replace(stage.build, toolchain_digest="b" * 64)
    with pytest.raises(ValueError, match="toolchain"):
        stage.adapter.build(BuildRequest(spec=SPEC, source=source, application_id=uuid4()), ctx)


def test_a_source_that_is_not_the_descriptor_is_invalid(stage: Stage) -> None:
    from shifde.adapters.native.base import create_archive

    source = stage.build.artifacts.put(
        stage.build.tenant_id, create_archive({"deployment.json": b"{}\n"})
    )
    report = stage.adapter.validate(ValidateRequest(spec=SPEC, source=source), stage.build)
    assert not report.passed
    assert [item.code for item in report.diagnostics] == ["INVALID_PALANTIR_SOURCE"]


def test_the_spec_names_a_file() -> None:
    with pytest.raises(ValueError):
        FoundryMarketplaceSpec(frontend_asset_path=".")
    with pytest.raises(ValueError):
        FoundryMarketplaceSpec(frontend_asset_path="../x")


# --------------------------------------------------------------------------- happy path
def test_apply_observe_and_verify_on_the_fake(stage: Stage) -> None:
    artifact, plan, deploy = stage.prepare()
    assert [step.key for step in plan.steps] == [
        "palantir-installation-create",
        "palantir-job-submit",
        "palantir-finalize",
    ]
    assert set(plan.details) == {"schema_digest"}
    receipt = stage.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    handles = receipt.operation_handles
    assert set(handles) == {
        "installation",
        "job_id",
        "request_digest",
        "schema_digest",
        "spec_digest",
        "platform",
    }
    assert handles["platform"] == "fake"
    for key in ("palantir-installation-create", "palantir-job-submit"):
        saved = deploy.journal.completed(key)
        assert saved is not None and saved["handles"]["platform"] == "fake"
    job = deploy.journal.completed("palantir-job-submit")
    assert job is not None and job["handles"]["job_id"] == handles["job_id"]
    assert deploy.journal.completed("palantir-finalize") == receipt.model_dump(mode="json")
    observed = stage.adapter.observe(ObserveRequest(receipt=receipt, spec=SPEC), stage.read)
    assert observed.state == "succeeded"
    report = stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)
    assert [(check.name, check.status) for check in report.checks] == [
        ("native-version", "passed"),
        ("ontology-read", "passed"),
        ("frontend-asset", "passed"),
        ("content-check", "not_run"),
    ]
    assert report.passed


def test_a_restarted_adapter_over_the_same_stack_observes_the_installation(stage: Stage) -> None:
    _, receipt = stage.receipt()
    restarted = PalantirMarketplaceAdapter(client_factory=stage.stack.client_factory)
    observed = restarted.observe(ObserveRequest(receipt=receipt, spec=SPEC), stage.read)
    assert observed.state == "succeeded"


def test_the_installation_body_and_the_job_payload_are_pinned(stage: Stage) -> None:
    artifact, _ = stage.receipt()
    bindings = stage.read.target.bindings
    (installation_path, body), (job_path, payload) = stage.stack.posts
    assert (installation_path, job_path) == (INSTALLATIONS, JOBS)
    assert set(body) == {
        "marketplaceRid",
        "productId",
        "parentFolderRid",
        "installationSuffix",
        "targetVersionHint",
    }
    assert body["targetVersionHint"] == {"type": "versionId", "versionId": bindings["version_id"]}
    assert body["installationSuffix"] == installation_suffix(
        artifact.application_id, stage.read.target.id
    )
    (target,) = payload["installations"]
    assert target["targetVersion"] == {"type": "versionId", "versionId": bindings["version_id"]}
    assert payload == job_payload(target["installationRid"], bindings["version_id"])


def test_the_asset_request_carries_no_authorization(stage: Stage) -> None:
    _, receipt = stage.receipt()
    stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)
    asset = [request for request in stage.stack.requests if request[1] == "/assets/health.txt"]
    assert asset == [("GET", "/assets/health.txt", False)]
    api = [request for request in stage.stack.requests if request[1].startswith("/api/")]
    assert api and all(authorized for _, _, authorized in api)


# --------------------------------------------------------------------------- faults
def test_asset_mismatch_fails_the_frontend_check(stage: Stage) -> None:
    _, receipt = stage.receipt()
    stage.stack.asset_mismatch = True
    report = stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)
    statuses = {check.name: check.status for check in report.checks}
    assert statuses == {
        "native-version": "passed",
        "ontology-read": "passed",
        "frontend-asset": "failed",
        "content-check": "not_run",
    }
    assert not report.passed


def test_a_failed_job_is_observed_failed_and_verify_skips_the_reads(stage: Stage) -> None:
    stage.stack.job_failed = True
    _, receipt = stage.receipt()
    observed = stage.adapter.observe(ObserveRequest(receipt=receipt, spec=SPEC), stage.read)
    assert observed.state == "failed"
    report = stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)
    assert [(check.name, check.status) for check in report.checks] == [
        ("native-version", "failed"),
        ("ontology-read", "skipped"),
        ("frontend-asset", "skipped"),
        ("content-check", "not_run"),
    ]


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("PREPARING", "running"),
        ("IN_PROGRESS", "running"),
        ("BUILDING", "running"),
        ("ABORTING", "running"),
        ("FAILED", "failed"),
        ("ABORTED", "unknown"),
        ("IDLE_DRAFT", "unknown"),
        ("invented", "unknown"),
    ],
)
def test_job_status_maps_to_a_state(stage: Stage, status: str, state: str) -> None:
    _, receipt = stage.receipt()
    stage.stack.jobs[receipt.operation_handles["job_id"]]["status"] = status
    observed = stage.adapter.observe(ObserveRequest(receipt=receipt, spec=SPEC), stage.read)
    assert observed.state == state


def test_a_succeeded_job_at_another_version_or_schema_is_unknown(stage: Stage) -> None:
    _, receipt = stage.receipt()
    installation = stage.stack.installations[receipt.operation_handles["installation"]]
    installation["latestTargetVersion"] = {"versionId": str(uuid4())}
    request = ObserveRequest(receipt=receipt, spec=SPEC)
    assert stage.adapter.observe(request, stage.read).state == "unknown"
    installation["latestTargetVersion"] = {"versionId": stage.read.target.bindings["version_id"]}
    assert stage.adapter.observe(request, stage.read).state == "succeeded"
    stage.stack.object_type_schema["properties"]["extra"] = {"dataType": {"type": "string"}}
    assert stage.adapter.observe(request, stage.read).state == "unknown"


@pytest.mark.parametrize("step", ["palantir-installation-create", "palantir-job-submit", "hang"])
def test_uncertain_writes_never_replay(stage: Stage, step: str) -> None:
    if step == "hang":
        stage.stack.hang = True
    else:
        stage.stack.fail_after_accept = step  # type: ignore[assignment]
    artifact, plan, deploy = stage.prepare()
    request = ApplyRequest(artifact=artifact, plan=plan)
    with pytest.raises(httpx.TransportError):
        stage.adapter.apply(request, deploy)
    assert len(stage.stack.installations) == 1
    if step == "palantir-job-submit":
        saved = deploy.journal.completed("palantir-installation-create")
        assert saved is not None
        assert saved["handles"]["installation"] == next(iter(stage.stack.installations))
        assert len(stage.stack.jobs) == 1
    posts = stage.posts
    requests = len(stage.stack.requests)
    with pytest.raises(NeedsAttention, match="replay"):
        stage.adapter.apply(request, deploy)
    assert stage.posts == posts
    assert len(stage.stack.requests) == requests


def test_a_released_hang_still_raises(stage: Stage) -> None:
    stage.stack.hang = True
    stage.stack.hang_timeout = 30.0
    stage.stack.release()
    artifact, plan, deploy = stage.prepare()
    with pytest.raises(httpx.ReadTimeout):
        stage.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def test_schema_drift_fails_before_any_mutation(stage: Stage) -> None:
    artifact, plan, deploy = stage.prepare()
    stage.stack.object_type_schema["revision"] = 2
    posts = stage.posts
    with pytest.raises(ValueError, match="changed"):
        stage.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert stage.posts == posts + 1  # the token only
    assert stage.stack.posts == []
    assert not deploy.journal.has_intent("palantir-installation-create")


def test_a_changed_version_binding_refuses_apply_before_any_request(stage: Stage) -> None:
    artifact, plan, _ = stage.prepare()
    moved = stage.with_target(version_id=str(uuid4()))
    requests = len(stage.stack.requests)
    with pytest.raises(ValueError, match="binding changed"):
        moved.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), moved.deploy_context())
    assert len(stage.stack.requests) == requests
    assert stage.stack.posts == []


# --------------------------------------------------------------------------- Ontology override
def test_an_ontology_override_is_refused_at_apply_readback(stage: Stage) -> None:
    stage.stack.ontology_override = True
    artifact, plan, deploy = stage.prepare()
    with pytest.raises(NeedsAttention, match="Ontology"):
        stage.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(stage.stack.installations) == 1
    assert stage.stack.jobs == {}
    assert deploy.journal.completed("palantir-installation-create") is not None


def test_an_ontology_override_is_refused_by_a_new_plan(stage: Stage) -> None:
    artifact, receipt = stage.receipt()
    stage.stack.ontology_override = True
    owner = stage.owning(receipt.resources)
    jobs = len(stage.stack.jobs)
    with pytest.raises(NeedsAttention, match="Ontology"):
        owner.adapter.plan(PlanRequest(artifact=artifact), owner.read)
    assert len(stage.stack.jobs) == jobs


def test_an_ontology_override_is_refused_at_observe(stage: Stage) -> None:
    _, receipt = stage.receipt()
    stage.stack.ontology_override = True
    with pytest.raises(NeedsAttention, match="Ontology"):
        stage.adapter.observe(ObserveRequest(receipt=receipt, spec=SPEC), stage.read)
    with pytest.raises(NeedsAttention, match="Ontology"):
        stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)


def test_an_owned_installation_is_reused_without_a_second_post(stage: Stage) -> None:
    artifact, receipt = stage.receipt()
    owner = stage.owning(receipt.resources)
    plan = owner.adapter.plan(PlanRequest(artifact=artifact), owner.read)
    again = owner.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), owner.deploy_context())
    assert again.operation_handles["installation"] == receipt.operation_handles["installation"]
    assert [path for path, _ in stage.stack.posts] == [INSTALLATIONS, JOBS, JOBS]


# --------------------------------------------------------------------------- preview opt-in
def test_preview_not_allowed_refuses_plan_and_apply_without_a_request(stage: Stage) -> None:
    artifact = stage.artifact()
    closed = stage.with_target(allow_preview_apis="false")
    with pytest.raises(PermissionError):
        closed.adapter.plan(PlanRequest(artifact=artifact), closed.read)
    plan = stage.adapter.plan(PlanRequest(artifact=artifact), stage.read)
    stage.stack.requests.clear()
    with pytest.raises(PermissionError):
        closed.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), closed.deploy_context())
    assert stage.stack.requests == []
    (deployment,) = closed.adapter.capabilities(closed.read).capabilities
    assert deployment.status == "unsupported"
    assert stage.stack.requests == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("marketplace_rid", "ri.marketplace.main.folder.00000000-0000-0000-0000-000000000000"),
        ("product_id", "not-a-uuid"),
        ("version_id", "latest"),
        ("namespace_rid", "ri.compass.main.folder.00000000-0000-0000-0000-000000000000"),
        ("parent_folder_rid", "ri.compass.main.folder.zzz"),
        ("ontology_rid", "ri.ontology.main.ontology.nope"),
        ("object_type", "1Example"),
        ("frontend_origin", "http://native-app.example.test"),
        ("frontend_asset_sha256", "A" * 64),
        ("allow_preview_apis", "yes"),
        ("version_id", ""),
    ],
)
def test_an_invalid_binding_is_refused_before_any_request(
    stage: Stage, key: str, value: str
) -> None:
    artifact = stage.artifact()
    broken = stage.with_target(**{key: value})
    with pytest.raises(ValueError, match=f"binding {key} ") as error:
        broken.adapter.plan(PlanRequest(artifact=artifact), broken.read)
    if value:
        assert value not in str(error.value)
    assert stage.stack.requests == []


# --------------------------------------------------------------------------- spec kind
def _databricks_artifact(stage: Stage) -> BuildArtifact:
    return stage.artifact().model_copy(update={"spec": DatabricksApplicationSpec()})


OPERATIONS: dict[str, Callable[[Stage], Any]] = {
    "scaffold": lambda s: s.adapter.scaffold(
        ScaffoldRequest(spec=DatabricksApplicationSpec()), s.build
    ),
    "validate": lambda s: s.adapter.validate(
        ValidateRequest(spec=DatabricksApplicationSpec(), source=s.artifact().archive), s.build
    ),
    "build": lambda s: s.adapter.build(
        BuildRequest(
            spec=DatabricksApplicationSpec(), source=s.artifact().archive, application_id=uuid4()
        ),
        s.build,
    ),
    "inspect": lambda s: s.adapter.inspect(
        InspectRequest(
            application_id=uuid4(), app_name="anything", spec=DatabricksApplicationSpec()
        ),
        s.read,
    ),
    "plan": lambda s: s.adapter.plan(PlanRequest(artifact=_databricks_artifact(s)), s.read),
    "apply": lambda s: s.adapter.apply(
        ApplyRequest(artifact=_databricks_artifact(s), plan=s.prepare()[1]), s.deploy_context()
    ),
    "observe": lambda s: s.adapter.observe(
        ObserveRequest(receipt=s.receipt()[1], spec=DatabricksApplicationSpec()), s.read
    ),
    "verify": lambda s: s.adapter.verify(
        VerifyRequest(receipt=s.receipt()[1], spec=DatabricksApplicationSpec()), s.read
    ),
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_every_operation_refuses_a_databricks_spec(stage: Stage, operation: str) -> None:
    with pytest.raises(ValueError, match="palantir.marketplace/v1"):
        OPERATIONS[operation](stage)


def test_the_databricks_adapter_refuses_a_marketplace_spec() -> None:
    platform = FakeDatabricksPlatform(service_principal="sp", warehouses={"wh"})
    adapter = DatabricksAdapter(cli=platform, workspace_factory=platform.workspace)
    build = BuildContext(
        tenant_id=uuid4(), artifacts=InMemoryArtifactStore(), toolchain_digest="a" * 64
    )
    with pytest.raises(ValueError, match="databricks.app/v1"):
        adapter.scaffold(ScaffoldRequest(spec=SPEC), build)
    assert platform.actions == []


# --------------------------------------------------------------------------- isolation
def _other_target_read(stage: Stage) -> ReadContext:
    target = Target(
        tenant_id=stage.read.tenant_id,
        host=stage.read.target.host,
        bindings=stage.read.target.bindings,
    )
    return ReadContext(tenant_id=stage.read.tenant_id, target=target)


Attempt = Callable[[], object]


def _plan_with_other_artifact(stage: Stage, other: Stage) -> Attempt:
    artifact = other.artifact()
    return lambda: stage.adapter.plan(PlanRequest(artifact=artifact), stage.read)


def _apply_with_other_artifact(stage: Stage, other: Stage) -> Attempt:
    _, plan, deploy = stage.prepare()
    artifact = other.artifact()
    return lambda: stage.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def _apply_with_other_tenant_plan(stage: Stage, other: Stage) -> Attempt:
    artifact, _, deploy = stage.prepare()
    _, plan, _ = other.prepare()
    return lambda: stage.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def _apply_with_plan_for_other_target(stage: Stage, other: Stage) -> Attempt:
    artifact, plan, deploy = stage.prepare()
    forged = seal_plan(plan.model_copy(update={"target_id": uuid4()}))
    return lambda: stage.adapter.apply(ApplyRequest(artifact=artifact, plan=forged), deploy)


def _observe_other_tenant_receipt(stage: Stage, other: Stage) -> Attempt:
    receipt = other.receipt()[1]
    return lambda: stage.adapter.observe(ObserveRequest(receipt=receipt, spec=SPEC), stage.read)


def _verify_other_tenant_receipt(stage: Stage, other: Stage) -> Attempt:
    receipt = other.receipt()[1]
    return lambda: stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)


def _observe_other_target_receipt(stage: Stage, other: Stage) -> Attempt:
    receipt, read = stage.receipt()[1], _other_target_read(stage)
    return lambda: stage.adapter.observe(ObserveRequest(receipt=receipt, spec=SPEC), read)


def _verify_other_target_receipt(stage: Stage, other: Stage) -> Attempt:
    receipt, read = stage.receipt()[1], _other_target_read(stage)
    return lambda: stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), read)


def _owned_on_another_host(stage: Stage, other: Stage) -> Attempt:
    artifact = stage.artifact()
    installation = f"ri.marketplace.main.block-set-installation.{uuid4()}"
    binding = ResourceBinding(
        key="installation",
        native_id=native_id(other.read.target.host, installation),
        resource_type="palantir.marketplace-installation",
        application_id=artifact.application_id,
        target_id=stage.read.target.id,
    )
    owner = stage.owning([binding])
    plan = stage.adapter.plan(PlanRequest(artifact=artifact), stage.read)
    return lambda: owner.adapter.apply(
        ApplyRequest(artifact=artifact, plan=plan), owner.deploy_context()
    )


def _owned_by_another_application(stage: Stage, other: Stage) -> Attempt:
    _, receipt = stage.receipt()
    owner = stage.owning(receipt.resources)
    artifact = stage.artifact()
    return lambda: owner.adapter.plan(PlanRequest(artifact=artifact), owner.read)


def _inspect_another_suffix(stage: Stage, other: Stage) -> Attempt:
    request = InspectRequest(
        application_id=uuid4(),
        app_name=installation_suffix(uuid4(), stage.read.target.id),
        spec=SPEC,
    )
    return lambda: stage.adapter.inspect(request, stage.read)


def _reconcile_other_tenant_plan(stage: Stage, other: Stage) -> Attempt:
    _, plan, _ = other.prepare()
    return lambda: stage.adapter.reconcile(ReconcileRequest(plan=plan), stage.read)


@pytest.mark.parametrize(
    "attempt",
    [
        _plan_with_other_artifact,
        _apply_with_other_artifact,
        _apply_with_other_tenant_plan,
        _apply_with_plan_for_other_target,
        _observe_other_tenant_receipt,
        _verify_other_tenant_receipt,
        _observe_other_target_receipt,
        _verify_other_target_receipt,
        _owned_on_another_host,
        _owned_by_another_application,
        _inspect_another_suffix,
        _reconcile_other_tenant_plan,
    ],
    ids=lambda attempt: attempt.__name__.strip("_"),
)
def test_palantir_cross_tenant_and_cross_target_inputs_are_refused(
    stage: Stage, other: Stage, attempt: Callable[[Stage, Stage], Attempt]
) -> None:
    call = attempt(stage, other)
    posts = (stage.posts, other.posts)
    with pytest.raises(PermissionError):
        call()
    assert (stage.posts, other.posts) == posts


def test_inspect_with_the_derived_suffix_reads_the_owned_installation(stage: Stage) -> None:
    artifact, receipt = stage.receipt()
    owner = stage.owning(receipt.resources)
    snapshot = owner.adapter.inspect(
        InspectRequest(
            application_id=artifact.application_id,
            app_name=installation_suffix(artifact.application_id, stage.read.target.id),
            spec=SPEC,
        ),
        owner.read,
    )
    assert snapshot.resources == receipt.resources


def test_more_than_one_owned_installation_is_refused(stage: Stage) -> None:
    artifact, receipt = stage.receipt()
    owner = stage.owning([*receipt.resources, *receipt.resources])
    with pytest.raises(ValueError, match="exactly one"):
        owner.adapter.plan(PlanRequest(artifact=artifact), owner.read)


def test_two_stacks_keep_separate_installations(stage: Stage, other: Stage) -> None:
    stage.receipt()
    assert len(stage.stack.installations) == 1
    assert other.stack.installations == {}
    other.receipt()
    assert set(stage.stack.installations).isdisjoint(other.stack.installations)


def test_reconcile_is_ambiguous_and_never_writes(stage: Stage) -> None:
    artifact, receipt = stage.receipt()
    owner = stage.owning(receipt.resources)
    plan = owner.adapter.plan(PlanRequest(artifact=artifact), owner.read)
    requests = len(stage.stack.requests)
    result = owner.adapter.reconcile(ReconcileRequest(plan=plan), owner.read)
    assert result.state == "ambiguous"
    assert result.resources == receipt.resources
    assert len(stage.stack.requests) == requests


def test_a_receipt_with_changed_handles_is_refused(stage: Stage) -> None:
    _, receipt = stage.receipt()
    for handles in (
        {**receipt.operation_handles, "extra": "x"},
        {k: v for k, v in receipt.operation_handles.items() if k != "platform"},
        {**receipt.operation_handles, "spec_digest": digest({"other": True})},
        {**receipt.operation_handles, "request_digest": "0" * 64},
    ):
        forged = receipt.model_copy(update={"operation_handles": handles})
        with pytest.raises(ValueError):
            stage.adapter.observe(ObserveRequest(receipt=forged, spec=SPEC), stage.read)


# --------------------------------------------------------------------------- delivery (SP8-7)
BOUND, RELEASED = "a" * 64, "b" * 64


def _descriptor(**changes: Any) -> dict[str, bytes]:
    """A delivered descriptor with `changes` (a `None` value drops the key), canonical JSON."""
    value: dict[str, Any] = {
        "spec": DELIVERY_SPEC.model_dump(mode="json"),
        "bindings_digest": BOUND,
        "release_digest": RELEASED,
        **changes,
    }
    value = {key: item for key, item in value.items() if item is not None}
    return {
        "deployment.json": json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    }


def test_the_delivered_descriptor_is_canonical_and_reads_back() -> None:
    files = delivery_files(bindings_digest=BOUND, release_digest=RELEASED)
    assert files == _descriptor()
    assert read_delivery(files) == Delivery(bindings_digest=BOUND, release_digest=RELEASED)
    validate_source(files, DELIVERY_SPEC)
    validate_source(files_for(SPEC), SPEC)  # the spec-only form stays valid
    with pytest.raises(ValueError):
        read_delivery(files_for(DELIVERY_SPEC))  # but is no delivery


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"other.json": b"{}"},
        {**_descriptor(), "extra.json": b"{}"},
        {"deployment.json": b"[]\n"},
        {"deployment.json": b"not json"},
        {"deployment.json": b"\xff\xfe"},
        {"deployment.json": _descriptor()["deployment.json"].rstrip(b"\n")},
        {"deployment.json": _descriptor()["deployment.json"] + b"\n"},
        {"deployment.json": b" " + _descriptor()["deployment.json"]},
        {
            "deployment.json": _descriptor()["deployment.json"].replace(
                b"{", b'{"release_digest":"' + b"c" * 64 + b'",', 1
            )
        },
        _descriptor(extra="x"),
        _descriptor(spec=None),
        _descriptor(spec=SPEC.model_dump(mode="json")),
        _descriptor(bindings_digest=None),
        _descriptor(release_digest=None),
        _descriptor(bindings_digest="A" * 64),
        _descriptor(bindings_digest="a" * 63),
        _descriptor(release_digest=BOUND + "\n"),
        _descriptor(release_digest=1),
        _descriptor(release_digest=["b" * 64]),
    ],
)
def test_a_tampered_delivery_descriptor_is_invalid(files: dict[str, bytes]) -> None:
    with pytest.raises(ValueError):
        validate_source(files, DELIVERY_SPEC)
    with pytest.raises(ValueError):
        read_delivery(files)


def test_the_bindings_digest_covers_every_binding_and_the_revision(stage: Stage) -> None:
    target = stage.read.target
    seen = {bindings_digest(target)}
    for key in target.bindings:
        changed = {**target.bindings, key: target.bindings[key] + "x"}
        seen.add(bindings_digest(target.model_copy(update={"bindings": changed})))
    seen.add(bindings_digest(target.model_copy(update={"revision": 2})))
    seen.add(bindings_digest(target.model_copy(update={"host": OTHER_HOST})))
    assert len(seen) == len(target.bindings) + 3


@pytest.mark.parametrize("environment", ["", "production", "staging"])
def test_the_fake_stack_refuses_to_construct_outside_development_and_test(
    environment: str,
) -> None:
    values = {
        "host": HOST,
        "frontend_origin": "https://native-app.example.test",
        "ontology_rid": f"ri.ontology.main.ontology.{uuid4()}",
    }
    with pytest.raises(RuntimeError, match="only in development or test"):
        FakeFoundryStack(environment=environment, **values)
    with pytest.raises(TypeError):
        FakeFoundryStack(**values)  # type: ignore[call-arg]
    for allowed in ("development", "test"):
        assert FakeFoundryStack(environment=allowed, **values).installations == {}


def test_another_toolchain_is_refused_before_any_request(stage: Stage) -> None:
    artifact, plan, deploy = stage.prepare()
    stage.stack.requests.clear()
    stage.stack.tokens.clear()
    other = PalantirMarketplaceAdapter(
        client_factory=stage.stack.client_factory, toolchain_digest="b" * 64
    )
    with pytest.raises(ValueError, match="toolchain"):
        other.plan(PlanRequest(artifact=artifact), stage.read)
    with pytest.raises(ValueError, match="toolchain"):
        other.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    moved = artifact.model_copy(update={"toolchain_digest": "b" * 64})
    with pytest.raises(ValueError, match="toolchain"):
        other.apply(ApplyRequest(artifact=moved, plan=plan), deploy)
    for sealed in (plan.toolchain_digest, None):
        with pytest.raises(ValueError, match="toolchain"):
            other.plan_details(sealed, stage.read)
    assert stage.stack.requests == [] and stage.stack.tokens == set()
    assert stage.adapter.plan_details(plan.toolchain_digest, stage.read) == plan.details


def test_plan_details_refuses_a_target_without_preview(stage: Stage) -> None:
    closed = stage.with_target(allow_preview_apis="false")
    with pytest.raises(PermissionError):
        closed.adapter.plan_details(TOOLCHAIN_DIGEST, closed.read)
    assert stage.stack.requests == []


def test_the_content_check_is_never_run_or_required(stage: Stage) -> None:
    _, receipt = stage.receipt()
    for failed in (False, True):
        stage.stack.jobs[receipt.operation_handles["job_id"]]["status"] = (
            "FAILED" if failed else "SUCCEEDED"
        )
        report = stage.adapter.verify(VerifyRequest(receipt=receipt, spec=SPEC), stage.read)
        (check,) = [c for c in report.checks if c.name == "content-check"]
        assert check.status == "not_run" and check.required is False
        assert "not derived from the release" in check.evidence
        assert "not checked" in check.evidence
        assert report.passed is not failed


@pytest.mark.parametrize("origin", NON_PUBLIC_ORIGINS)
def test_a_non_public_frontend_origin_is_refused_before_any_request(
    stage: Stage, origin: str
) -> None:
    artifact = stage.artifact()
    broken = stage.with_target(frontend_origin=origin)
    with pytest.raises(ValueError) as error:
        broken.adapter.plan(PlanRequest(artifact=artifact), broken.read)
    assert str(error.value) == "Foundry binding frontend_origin is missing or invalid"
    assert stage.stack.requests == [] and stage.stack.tokens == set()
