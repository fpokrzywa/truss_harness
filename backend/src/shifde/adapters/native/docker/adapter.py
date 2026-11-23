"""`DockerComposeAdapter`: the ten native operations for a `docker.compose/v1` bundle, synchronous.

The project, port, image and daemon are derived from the artifact and the target, never taken from
the caller; the compose project, image and env file are named after the target id
(`shifde-<target id hex>`), never a client-chosen name. Every write is attempted once: a failed or
timed-out write propagates, and the journal turns any uncertain outcome into `NeedsAttention`. A
target's daemon must be the runtime's (`docker_host_mismatch`), and the target's compose project
must carry this target's label (`foreign_project`). There is no down and no image prune.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal  # Any: decoded health JSON
from uuid import UUID

from shifde.adapters.native.base import (
    ArtifactStore,
    BuildContext,
    DeployContext,
    NeedsAttention,
    ReadContext,
    read_archive,
)
from shifde.adapters.native.contracts import (
    DOCKER_HOST,
    EVIDENCE_MAX,
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    Capability,
    CapabilityReport,
    Check,
    DeploymentPlan,
    Diagnostic,
    DockerComposeSpec,
    EnvironmentSnapshot,
    InspectRequest,
    NativeObservation,
    ObserveRequest,
    PlanRequest,
    PlanStep,
    ReconcileRequest,
    ReconciliationResult,
    ResourceBinding,
    ScaffoldRequest,
    SourceArchive,
    ValidateRequest,
    ValidationReport,
    VerificationReport,
    VerifyRequest,
    digest,
    utcnow,
)
from shifde.adapters.native.docker.runtime import ContainerRuntime, ContainerState, DockerRefused
from shifde.adapters.native.docker.source import (
    ADAPTER_VERSION,
    RELEASE_LABEL,
    TARGET_LABEL,
    BundleInvalid,
    image_tag,
    target_project_port,
    validate_bundle,
    validate_for_target,
)
from shifde.adapters.native.planning import (
    bindings_digest,
    seal_plan,
    validate_changes,
    validate_plan,
)

RESOURCE_TYPE = "docker.compose_project"
RESOURCE_KEY = "compose_project"
STEPS = ("image-build", "image-verify", "compose-up", "container-ready")


def identity(containers: list[ContainerState]) -> list[dict[str, Any]]:
    return sorted(
        ({"name": c.name, "image": c.image, "labels": c.labels} for c in containers),
        key=lambda item: str(item["name"]),
    )


class DockerComposeAdapter:
    def __init__(self, runtime: ContainerRuntime, *, artifacts: ArtifactStore) -> None:
        self.runtime = runtime
        # Plans read the artifact's bundle; a `ReadContext` carries no artifact store.
        self.artifacts = artifacts

    @staticmethod
    def _resource(project: str, application_id: UUID, target_id: UUID) -> ResourceBinding:
        return ResourceBinding(
            key=RESOURCE_KEY,
            native_id=project,
            resource_type=RESOURCE_TYPE,
            application_id=application_id,
            target_id=target_id,
        )

    def _daemon(self, ctx: ReadContext) -> str:
        """The target's daemon id; refuses a runtime on another daemon."""
        match = DOCKER_HOST.fullmatch(ctx.target.host)
        if not match:
            raise ValueError("the target is not a Docker daemon address")
        ctx.check_deadline()
        if self.runtime.daemon_id() != match.group(1):
            raise DockerRefused("docker_host_mismatch")
        return match.group(1)

    def _containers(self, project: str, ctx: ReadContext) -> list[ContainerState]:
        """The project's containers; refuses a project not labelled for this target."""
        containers = self.runtime.compose_ps(project)
        if any(c.labels.get(TARGET_LABEL) != str(ctx.target.id) for c in containers):
            raise DockerRefused("foreign_project")
        return containers

    def capabilities(self, ctx: ReadContext) -> CapabilityReport:
        try:
            self.runtime.daemon_id()
            item = Capability(operation="runtime", status="supported", reason="Docker reachable")
        except (FileNotFoundError, PermissionError, ValueError):
            item = Capability(
                operation="runtime", status="unsupported", reason="Docker Engine 28+ unavailable"
            )
        except Exception:
            item = Capability(
                operation="runtime", status="unknown", reason="Docker could not be verified"
            )
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            maturity="experimental",
            application_kinds=["docker.compose/v1"],
            capabilities=[item],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    def inspect(self, request: InspectRequest, ctx: ReadContext) -> EnvironmentSnapshot:
        project, _ = target_project_port(ctx.target)
        if request.app_name != project:
            raise PermissionError("app name is not the target's compose project")
        self._daemon(ctx)
        containers = self._containers(project, ctx)
        resources = (
            [self._resource(project, request.application_id, ctx.target.id)] if containers else []
        )
        return EnvironmentSnapshot(resources=resources, fingerprint=digest(identity(containers)))

    def scaffold(self, request: ScaffoldRequest, ctx: BuildContext) -> SourceArchive:
        raise ValueError("docker.compose/v1 artifacts come only from the generator")

    def validate(self, request: ValidateRequest, ctx: BuildContext) -> ValidationReport:
        if not isinstance(request.spec, DockerComposeSpec):
            return ValidationReport(
                passed=False,
                diagnostics=[
                    Diagnostic(code="unsupported_spec", message="the spec is not docker.compose/v1")
                ],
            )
        try:
            validate_bundle(read_archive(ctx.artifacts.read(request.source, ctx.tenant_id)))
            return ValidationReport(passed=True)
        except BundleInvalid as error:
            return ValidationReport(
                passed=False, diagnostics=[Diagnostic(code=error.code, message=str(error))]
            )
        except ValueError as error:
            return ValidationReport(
                passed=False, diagnostics=[Diagnostic(code="invalid_source", message=str(error))]
            )

    def build(self, request: BuildRequest, ctx: BuildContext) -> BuildArtifact:
        report = self.validate(ValidateRequest(spec=request.spec, source=request.source), ctx)
        if not report.passed:
            raise ValueError("application source validation failed")
        # Only the runtime builds, at apply, from the staged bundle.
        return BuildArtifact(
            tenant_id=ctx.tenant_id,
            application_id=request.application_id,
            archive=request.source,
            source_digest=request.source.digest,
            spec_digest=digest(request.spec),
            spec=request.spec,
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=ctx.toolchain_digest,
        )

    @staticmethod
    def _check_artifact(artifact: BuildArtifact, ctx: ReadContext) -> None:
        if not isinstance(artifact.spec, DockerComposeSpec):
            raise ValueError("the artifact is not a docker.compose/v1 application")
        if artifact.tenant_id != ctx.tenant_id or artifact.archive.tenant_id != ctx.tenant_id:
            raise PermissionError("artifact belongs to another tenant")
        if artifact.adapter_version != ADAPTER_VERSION or artifact.spec_digest != digest(
            artifact.spec
        ):
            raise ValueError("artifact spec or adapter version mismatch")

    @staticmethod
    def _details(
        contents: dict[str, bytes], daemon_id: str, ctx: ReadContext
    ) -> dict[str, str | int]:
        """Project, port, image, release and daemon, from the artifact and the target only."""
        bundle = validate_bundle(contents)
        project, port = validate_for_target(bundle, ctx.target)
        return {
            "project": project,
            "port": port,
            "image": image_tag(project, bundle.release_digest),
            "release_digest": bundle.release_digest,
            "daemon_id": daemon_id,
        }

    def plan(self, request: PlanRequest, ctx: ReadContext) -> DeploymentPlan:
        artifact = request.artifact
        self._check_artifact(artifact, ctx)
        contents = read_archive(self.artifacts.read(artifact.archive, ctx.tenant_id))
        project, _ = target_project_port(ctx.target)
        daemon_id = self._daemon(ctx)
        containers = self._containers(project, ctx)
        details = self._details(contents, daemon_id, ctx)
        resources = (
            [self._resource(project, artifact.application_id, ctx.target.id)] if containers else []
        )
        return seal_plan(
            DeploymentPlan(
                tenant_id=ctx.tenant_id,
                application_id=artifact.application_id,
                target_id=ctx.target.id,
                target_revision=ctx.target.revision,
                artifact_id=artifact.id,
                artifact_digest=artifact.archive.digest,
                spec_digest=artifact.spec_digest,
                bindings_digest=bindings_digest(ctx.target),
                adapter_version=ADAPTER_VERSION,
                toolchain_digest=artifact.toolchain_digest,
                snapshot=EnvironmentSnapshot(
                    resources=resources, fingerprint=digest(identity(containers))
                ),
                steps=[
                    PlanStep(key="image-build", action="create", resource_key=RESOURCE_KEY),
                    PlanStep(
                        key="compose-up",
                        action="update" if containers else "create",
                        resource_key=RESOURCE_KEY,
                    ),
                    PlanStep(key="container-ready", action="start", resource_key=RESOURCE_KEY),
                ],
                details=dict(details),
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    def apply(self, request: ApplyRequest, ctx: DeployContext) -> ApplyReceipt:
        artifact, plan = request.artifact, request.plan
        if plan.tenant_id != ctx.tenant_id or plan.target_id != ctx.target.id:
            raise PermissionError("plan belongs to another tenant or target")
        validate_plan(plan, ctx.target, check_start_ttl=False)
        if ctx.toolchain_digest != plan.toolchain_digest:
            raise ValueError("executing toolchain does not match the deployment plan")
        self._check_artifact(artifact, ctx)
        validate_changes(plan.steps)
        expected = (
            artifact.id,
            artifact.archive.digest,
            artifact.spec_digest,
            artifact.toolchain_digest,
            artifact.application_id,
            artifact.adapter_version,
        )
        actual = (
            plan.artifact_id,
            plan.artifact_digest,
            plan.spec_digest,
            plan.toolchain_digest,
            plan.application_id,
            plan.adapter_version,
        )
        if actual != expected:
            raise ValueError("artifact does not match deployment plan")
        contents = read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id))
        match = DOCKER_HOST.fullmatch(ctx.target.host)
        if not match:
            raise ValueError("the target is not a Docker daemon address")
        details = self._details(contents, match.group(1), ctx)
        if any(plan.details.get(key) != value for key, value in details.items()):
            raise PermissionError("deployment plan details do not match the artifact and target")
        for key in STEPS:
            if ctx.journal.has_intent(key) and ctx.journal.completed(key) is None:
                raise NeedsAttention(f"reconcile uncertain step {key} before resubmission")
        saved = ctx.journal.completed("container-ready")
        if saved:
            ctx.journal.intent("container-ready", {"plan": plan.digest})
            receipt = ApplyReceipt.model_validate(saved)
            self._check_receipt(receipt, ctx)
            if (
                receipt.artifact_digest != artifact.archive.digest
                or receipt.application_id != artifact.application_id
            ):
                raise ValueError("saved receipt does not match the artifact")
            return receipt
        # The TTL limits starting new native work, not replaying a completed receipt.
        validate_plan(plan, ctx.target)
        project, port = str(details["project"]), int(details["port"])
        image, release_digest = str(details["image"]), str(details["release_digest"])
        self._daemon(ctx)
        containers = self._containers(project, ctx)
        if ctx.journal.completed("compose-up") is None and (
            digest(identity(containers)) != plan.snapshot.fingerprint
        ):
            raise ValueError("native resource changed since planning")
        with tempfile.TemporaryDirectory(prefix="shifde-docker-") as temporary:
            directory = Path(temporary)
            for path, data in contents.items():
                destination = directory / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            if ctx.journal.intent("image-build", {"plan": plan.digest}):
                ctx.check_deadline()
                self.runtime.build(
                    directory,
                    tag=image,
                    expected_sha256={
                        name: hashlib.sha256(contents[name]).hexdigest()
                        for name in ("Dockerfile", "compose.yaml")
                    },
                )
                ctx.journal.complete("image-build", {"image": image})
            if ctx.journal.intent("image-verify", {"plan": plan.digest}):
                labels = self.runtime.image_labels(image)
                if labels is None or labels.get(RELEASE_LABEL) != release_digest:
                    raise ValueError("the built image does not carry the release digest")
                ctx.journal.complete("image-verify", {"release_digest": release_digest})
            if ctx.journal.intent("compose-up", {"plan": plan.digest}):
                ctx.check_deadline()
                self.runtime.compose_up(directory, project=project)
                ctx.journal.complete("compose-up", {"project": project})
        ctx.journal.intent("container-ready", {"plan": plan.digest})
        receipt = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=[self._resource(project, artifact.application_id, ctx.target.id)],
            operation_handles={
                "project": project,
                "image": image,
                "port": str(port),
                "release_digest": release_digest,
                "daemon_id": str(details["daemon_id"]),
            },
        )
        ctx.journal.complete("container-ready", receipt.model_dump(mode="json"))
        return receipt

    @staticmethod
    def _check_receipt(receipt: ApplyReceipt, ctx: ReadContext) -> None:
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("receipt belongs to another tenant or target")
        if not receipt.resources:
            raise ValueError("receipt has no native resources")

    def observe(self, request: ObserveRequest, ctx: ReadContext) -> NativeObservation:
        receipt = request.receipt
        self._check_receipt(receipt, ctx)
        handles = receipt.operation_handles
        project, _ = target_project_port(ctx.target)
        if handles.get("project") != project:
            raise PermissionError("receipt project is not the target's compose project")
        if handles.get("daemon_id") != self._daemon(ctx):
            raise DockerRefused("docker_host_mismatch")
        containers = self.runtime.compose_ps(project)
        failed: Literal["failed"] = "failed"
        if not containers:
            return NativeObservation(state=failed, diagnostics=["the project has no container"])
        if any(
            c.labels.get(TARGET_LABEL) != str(ctx.target.id) or c.image != handles["image"]
            for c in containers
        ):
            return NativeObservation(state=failed, diagnostics=["the container identity changed"])
        if any(c.health == "unhealthy" or c.state != "running" for c in containers):
            return NativeObservation(
                state=failed,
                resources=receipt.resources,
                diagnostics=["the container is not running and healthy"],
            )
        if any(c.health is None for c in containers):
            return NativeObservation(
                state="unknown",
                resources=receipt.resources,
                diagnostics=["the container has no health status"],
            )
        if any(c.health != "healthy" for c in containers):
            return NativeObservation(state="running", resources=receipt.resources)
        labels = self.runtime.image_labels(handles["image"]) or {}
        if labels.get(RELEASE_LABEL) != handles["release_digest"]:
            return NativeObservation(
                state=failed, diagnostics=["the image does not carry the release digest"]
            )
        return NativeObservation(state="succeeded", resources=receipt.resources)

    def reconcile(self, request: ReconcileRequest, ctx: ReadContext) -> ReconciliationResult:
        if request.plan.tenant_id != ctx.tenant_id or request.plan.target_id != ctx.target.id:
            raise PermissionError("plan context mismatch")
        # A container alone cannot prove which build or up was accepted. Never replay.
        return ReconciliationResult(
            state="ambiguous",
            evidence="the daemon's state does not prove which release was accepted",
        )

    def _health(self, project: str, port: int, path: str) -> tuple[int, dict[str, Any]]:
        status, body = self.runtime.http_get_local(project, port, path)
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError("unexpected health response")
        return status, document

    def verify(self, request: VerifyRequest, ctx: ReadContext) -> VerificationReport:
        receipt = request.receipt
        observed = self.observe(ObserveRequest(receipt=receipt), ctx)
        checks = [
            Check(
                name="native-version",
                status="passed" if observed.state == "succeeded" else "failed",
            )
        ]
        if observed.state != "succeeded":
            checks += [
                Check(name="runtime-health", status="skipped"),
                Check(name="app-deep-health", status="skipped"),
            ]
            return VerificationReport(checks=checks)
        project, port = receipt.operation_handles["project"], int(receipt.operation_handles["port"])
        try:
            status, body = self._health(project, port, "/health")
            good = status == 200 and body.get("status") == "ok"
            checks.append(Check(name="runtime-health", status="passed" if good else "failed"))
        except Exception:
            checks.append(
                Check(name="runtime-health", status="failed", evidence="health request failed")
            )
        checks.append(self._deep_health(project, port, receipt.operation_handles["release_digest"]))
        return VerificationReport(checks=checks)

    def _deep_health(self, project: str, port: int, release_digest: str) -> Check:
        """SP5 6.6: 200, `status` ok, the plan's release digest and every check passed."""
        try:
            status, body = self._health(project, port, "/health?deep=1")
        except Exception:
            return Check(
                name="app-deep-health", status="failed", evidence="deep health request failed"
            )
        items = body.get("checks")
        items = items if isinstance(items, list) else []
        failed = [
            item for item in items if not isinstance(item, dict) or item.get("status") != "passed"
        ]
        reasons = "; ".join(
            f"{item.get('name')}: {item.get('reason')}" if isinstance(item, dict) else "malformed"
            for item in failed
        )
        good = (
            status == 200
            and body.get("status") == "ok"
            and body.get("release_digest") == release_digest
            and isinstance(body.get("checks"), list)
            and not failed
        )
        if good:
            return Check(name="app-deep-health", status="passed")
        if not reasons and body.get("release_digest") != release_digest:
            reasons = "the release digest differs from the plan's"
        return Check(
            name="app-deep-health",
            status="failed",
            evidence=(reasons or f"deep health answered {status}")[:EVIDENCE_MAX],
        )
