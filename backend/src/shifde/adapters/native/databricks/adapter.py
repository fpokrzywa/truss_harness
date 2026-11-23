"""`DatabricksAdapter`: the ten native operations for a Databricks App, synchronous.

The CLI is the only write path and every write is attempted once: a failed or timed-out write
propagates, and the journal turns any uncertain outcome into `NeedsAttention`.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal  # Any: decoded Databricks app JSON
from uuid import UUID

import yaml

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    NeedsAttention,
    ReadContext,
    create_archive,
    read_archive,
)
from shifde.adapters.native.contracts import (
    ApplicationSpec,
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    Capability,
    CapabilityReport,
    Check,
    DatabricksApplicationSpec,
    DeploymentPlan,
    Diagnostic,
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
    Target,
    ValidateRequest,
    ValidationReport,
    VerificationReport,
    VerifyRequest,
    digest,
    utcnow,
)
from shifde.adapters.native.databricks.clients import BundleRunner, WorkspaceGateway
from shifde.adapters.native.databricks.source import (
    ADAPTER_VERSION,
    CLI_VERSION,
    deployment_bundle,
    prefix,
    scaffold_files,
    validate_source,
)
from shifde.adapters.native.planning import (
    bindings_digest,
    seal_plan,
    validate_changes,
    validate_plan,
)

WAREHOUSE_BINDING = "warehouse_id"
# The deploying service principal's workspace user name; bundles deploy under its home folder.
PRINCIPAL_BINDING = "service_principal"
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]{0,254}$")


def app_name(application_id: UUID, target_id: UUID) -> str:
    return f"shifde-{application_id.hex[:12]}-{target_id.hex[:8]}"


def root_path(principal: str, application_id: UUID, target_id: UUID) -> str:
    if not _PRINCIPAL.fullmatch(principal) or ".." in principal:
        raise ValueError("service principal binding is not a valid workspace user name")
    return f"/Workspace/Users/{principal}/.shifde/{application_id}/{target_id}"


def databricks_spec(spec: ApplicationSpec) -> DatabricksApplicationSpec:
    """`spec`, refused with `ValueError` unless it is a Databricks App spec."""
    if not isinstance(spec, DatabricksApplicationSpec):
        raise ValueError("the Databricks adapter requires a databricks.app/v1 spec")
    return spec


def identity(app: dict[str, Any] | None) -> dict[str, Any]:
    if app is None:
        return {"exists": False}
    return {
        "exists": True,
        "id": app.get("id", app.get("name")),
        "name": app.get("name"),
        "deployment_id": (app.get("active_deployment") or {}).get("deployment_id"),
        "resources": app.get("resources", []),
    }


class DatabricksAdapter:
    def __init__(
        self,
        *,
        cli: BundleRunner,
        workspace_factory: Callable[[Target], WorkspaceGateway],
    ) -> None:
        self.cli = cli
        self.workspace_factory = workspace_factory

    def _workspace(self, ctx: ReadContext) -> WorkspaceGateway:
        ctx.check_deadline()
        return self.workspace_factory(ctx.target)

    @staticmethod
    def _resource(
        app: dict[str, Any], key: str, application_id: UUID, target_id: UUID
    ) -> ResourceBinding:
        return ResourceBinding(
            key=key,
            native_id=app.get("id") or app["name"],
            resource_type="databricks.app",
            application_id=application_id,
            target_id=target_id,
        )

    def capabilities(self, ctx: ReadContext) -> CapabilityReport:
        items = []
        try:
            self.cli.check_version()
            items.append(
                Capability(operation="cli", status="supported", reason=f"pinned CLI {CLI_VERSION}")
            )
        except (OSError, ValueError, RuntimeError):
            items.append(
                Capability(operation="cli", status="unsupported", reason="pinned CLI unavailable")
            )
        warehouse = ctx.target.bindings.get(WAREHOUSE_BINDING)
        if not warehouse:
            items.append(
                Capability(
                    operation="workspace", status="unsupported", reason="warehouse binding missing"
                )
            )
        else:
            try:
                self._workspace(ctx).probe(warehouse)
                items.append(
                    Capability(
                        operation="workspace",
                        status="supported",
                        reason="Apps metadata and warehouse APIs accessible; "
                        "write privileges enforced by native apply",
                    )
                )
            except Exception:
                items.append(
                    Capability(
                        operation="workspace",
                        status="unknown",
                        reason="workspace prerequisites could not be verified",
                    )
                )
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            maturity="experimental",
            application_kinds=["databricks.app/v1"],
            capabilities=items,
            expires_at=utcnow() + timedelta(minutes=5),
        )

    def inspect(self, request: InspectRequest, ctx: ReadContext) -> EnvironmentSnapshot:
        if request.spec is not None:
            databricks_spec(request.spec)
        if request.app_name != app_name(request.application_id, ctx.target.id):
            raise PermissionError("app name is not the one derived for this application and target")
        app = self._workspace(ctx).get_app(request.app_name)
        resources = (
            []
            if app is None
            else [self._resource(app, "app", request.application_id, ctx.target.id)]
        )
        return EnvironmentSnapshot(resources=resources, fingerprint=digest(identity(app)))

    def scaffold(self, request: ScaffoldRequest, ctx: BuildContext) -> SourceArchive:
        spec = databricks_spec(request.spec)
        if request.template_version != ADAPTER_VERSION:
            raise ValueError("unsupported template version")
        return ctx.artifacts.put(ctx.tenant_id, create_archive(scaffold_files(spec)))

    def validate(self, request: ValidateRequest, ctx: BuildContext) -> ValidationReport:
        spec = databricks_spec(request.spec)
        try:
            contents = read_archive(ctx.artifacts.read(request.source, ctx.tenant_id))
            validate_source(contents, spec)
            return ValidationReport(passed=True)
        except (ValueError, yaml.YAMLError) as error:
            return ValidationReport(
                passed=False, diagnostics=[Diagnostic(code="INVALID_SOURCE", message=str(error))]
            )

    def build(self, request: BuildRequest, ctx: BuildContext) -> BuildArtifact:
        databricks_spec(request.spec)
        report = self.validate(ValidateRequest(spec=request.spec, source=request.source), ctx)
        if not report.passed:
            raise ValueError("application source validation failed")
        # Databricks builds dependencies remotely. Locally we package, but never execute, source.
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

    def plan(self, request: PlanRequest, ctx: ReadContext) -> DeploymentPlan:
        artifact = request.artifact
        self._check_artifact(artifact, ctx)
        spec = databricks_spec(artifact.spec)
        if not ctx.target.bindings.get(spec.warehouse_binding):
            raise ValueError("required warehouse binding is missing")
        if spec.warehouse_binding != WAREHOUSE_BINDING:
            raise ValueError("initial adapter supports the warehouse_id binding key only")
        report = self.capabilities(ctx)
        if not all(report.supports(key) for key in ("cli", "workspace")):
            raise ValueError("required Databricks capabilities are unsupported or unknown")
        details = self._details(artifact, ctx)
        name = details["app_name"]
        app = self._workspace(ctx).get_app(name)
        resources = self._owned(app, artifact, ctx)
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
                    resources=resources, fingerprint=digest(identity(app))
                ),
                steps=[
                    PlanStep(
                        key="bundle-deploy",
                        action="update" if app else "create",
                        resource_key=spec.app_resource_key,
                    ),
                    PlanStep(key="bundle-run", action="start", resource_key=spec.app_resource_key),
                ],
                details={"cli_version": CLI_VERSION, **details},
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    @staticmethod
    def _details(artifact: BuildArtifact, ctx: ReadContext) -> dict[str, str]:
        """App name, root path and warehouse, derived from the artifact and the target only."""
        principal = ctx.target.bindings.get(PRINCIPAL_BINDING)
        if not principal:
            raise ValueError("required service principal binding is missing")
        warehouse = ctx.target.bindings.get(databricks_spec(artifact.spec).warehouse_binding)
        if not warehouse:
            raise ValueError("required warehouse binding is missing")
        return {
            "app_name": app_name(artifact.application_id, ctx.target.id),
            "root_path": root_path(principal, artifact.application_id, ctx.target.id),
            "warehouse_id": warehouse,
        }

    def _owned(
        self, app: dict[str, Any] | None, artifact: BuildArtifact, ctx: ReadContext
    ) -> list[ResourceBinding]:
        """The app's binding when `ctx` owns it; refuses an unowned app and a vanished owned one."""
        if app:
            binding = self._resource(
                app,
                databricks_spec(artifact.spec).app_resource_key,
                artifact.application_id,
                ctx.target.id,
            )
            if binding not in ctx.owned_resources:
                raise PermissionError("refusing to adopt an unowned native app by name")
            return [binding]
        if any(
            item.application_id == artifact.application_id and item.target_id == ctx.target.id
            for item in ctx.owned_resources
        ):
            raise ValueError("owned native resource is missing; resolve drift before redeploying")
        return []

    @staticmethod
    def _check_artifact(artifact: BuildArtifact, ctx: ReadContext) -> None:
        databricks_spec(artifact.spec)
        if artifact.tenant_id != ctx.tenant_id or artifact.archive.tenant_id != ctx.tenant_id:
            raise PermissionError("artifact belongs to another tenant")
        if artifact.adapter_version != ADAPTER_VERSION or artifact.spec_digest != digest(
            artifact.spec
        ):
            raise ValueError("artifact spec or adapter version mismatch")

    def apply(self, request: ApplyRequest, ctx: DeployContext) -> ApplyReceipt:
        artifact, plan = request.artifact, request.plan
        spec = databricks_spec(artifact.spec)
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
        details = self._details(artifact, ctx)
        if any(plan.details.get(key) != value for key, value in details.items()):
            raise PermissionError("deployment plan details do not match the artifact and target")
        contents = read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id))
        validate_source(contents, spec)
        for key in ("bundle-deploy", "bundle-run"):
            if ctx.journal.has_intent(key) and ctx.journal.completed(key) is None:
                raise NeedsAttention(f"reconcile uncertain step {key} before resubmission")
        saved = ctx.journal.completed("bundle-run")
        if saved:
            ctx.journal.intent("bundle-run", {"plan": plan.digest})
            receipt = ApplyReceipt.model_validate(saved)
            self._check_receipt(receipt, ctx)
            if (
                receipt.artifact_digest != artifact.archive.digest
                or receipt.application_id != artifact.application_id
            ):
                raise ValueError("saved receipt does not match the artifact")
            return receipt
        if ctx.journal.completed("bundle-deploy"):
            raise NeedsAttention(
                "remote source provenance is unproven after checkpoint recovery; "
                "refusing to start it"
            )
        # The TTL limits starting new native work, not replaying a completed receipt.
        validate_plan(plan, ctx.target)
        workspace = self._workspace(ctx)
        name = details["app_name"]
        current = workspace.get_app(name)
        self._owned(current, artifact, ctx)
        if digest(identity(current)) != plan.snapshot.fingerprint:
            raise ValueError("native resource changed since planning")
        # Fresh trusted configuration; no source-provided hooks/configuration reach the CLI.
        with tempfile.TemporaryDirectory(prefix="shifde-deploy-") as temporary:
            directory = Path(temporary)
            root = prefix(spec)
            for path, data in contents.items():
                local = path.removeprefix(root)
                if local.startswith("app/"):
                    destination = directory / local
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
            config = deployment_bundle(
                spec,
                host=ctx.target.host,
                app_name=name,
                root_path=details["root_path"],
                warehouse_id=details["warehouse_id"],
            )
            (directory / "databricks.yml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
            if ctx.journal.intent("bundle-deploy", {"plan": plan.digest}):
                ctx.check_deadline()
                self.cli.execute(
                    "deploy", directory, target=spec.bundle_target, host=ctx.target.host
                )
                current = workspace.get_app(name)
                if not current:
                    raise NeedsAttention("bundle accepted but native app could not be observed")
                ctx.journal.complete(
                    "bundle-deploy", {"native_id": current.get("id") or current["name"]}
                )
            ctx.journal.intent("bundle-run", {"plan": plan.digest})
            ctx.check_deadline()
            before = ((current or {}).get("active_deployment") or {}).get("deployment_id")
            submitted = self.cli.execute(
                "run",
                directory,
                target=spec.bundle_target,
                host=ctx.target.host,
                app_key=spec.app_resource_key,
            )
            current = workspace.get_app(name)
            if not current:
                raise NeedsAttention("app start accepted but native app could not be observed")
            active = current.get("pending_deployment") or current.get("active_deployment") or {}
            deployment_id = submitted.get("deployment_id") or active.get("deployment_id")
            if not deployment_id or deployment_id == before:
                raise NeedsAttention(
                    "app start accepted but a new native deployment handle is not yet observable"
                )
            receipt = ApplyReceipt(
                tenant_id=ctx.tenant_id,
                application_id=artifact.application_id,
                target_id=ctx.target.id,
                artifact_digest=artifact.archive.digest,
                resources=[
                    self._resource(
                        current, spec.app_resource_key, artifact.application_id, ctx.target.id
                    )
                ],
                operation_handles={"app_name": name, "deployment_id": deployment_id},
            )
            ctx.journal.complete("bundle-run", receipt.model_dump(mode="json"))
            return receipt

    def observe(self, request: ObserveRequest, ctx: ReadContext) -> NativeObservation:
        if request.spec is not None:
            databricks_spec(request.spec)
        receipt = request.receipt
        self._check_receipt(receipt, ctx)
        app = self._workspace(ctx).get_app(receipt.operation_handles["app_name"])
        if not app:
            return NativeObservation(state="failed", diagnostics=["native app is missing"])
        if (app.get("id") or app.get("name")) != receipt.resources[0].native_id:
            return NativeObservation(state="failed", diagnostics=["native identity changed"])
        active = app.get("active_deployment") or {}
        pending = app.get("pending_deployment") or {}
        expected_id = receipt.operation_handles.get("deployment_id")
        observed = pending if pending.get("deployment_id") == expected_id else active
        deployment_state = (observed.get("status") or {}).get("state")
        runtime_state = (app.get("app_status") or {}).get("state")
        state: Literal["running", "succeeded", "failed", "unknown"]
        if runtime_state == "CRASHED" or deployment_state in ("FAILED", "CANCELLED"):
            state = "failed"
        elif (
            expected_id
            and active.get("deployment_id") == expected_id
            and deployment_state == "SUCCEEDED"
            and runtime_state == "RUNNING"
        ):
            state = "succeeded"
        else:
            state = "running"
        return NativeObservation(state=state, resources=receipt.resources)

    @staticmethod
    def _check_receipt(receipt: ApplyReceipt, ctx: ReadContext) -> None:
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("receipt belongs to another tenant or target")
        if not receipt.resources:
            raise ValueError("receipt has no native resources")

    def reconcile(self, request: ReconcileRequest, ctx: ReadContext) -> ReconciliationResult:
        if request.plan.tenant_id != ctx.tenant_id or request.plan.target_id != ctx.target.id:
            raise PermissionError("plan context mismatch")
        app = self._workspace(ctx).get_app(str(request.plan.details["app_name"]))
        # Name/state alone cannot prove which artifact was accepted. Never replay automatically.
        return ReconciliationResult(
            state="ambiguous",
            evidence="native app exists but artifact acceptance is unproven"
            if app
            else "absence does not prove a submitted bundle cannot still complete",
        )

    def verify(self, request: VerifyRequest, ctx: ReadContext) -> VerificationReport:
        if request.spec is not None:
            databricks_spec(request.spec)
        observed = self.observe(ObserveRequest(receipt=request.receipt), ctx)
        checks = [
            Check(
                name="native-runtime",
                status="passed" if observed.state == "succeeded" else "failed",
            )
        ]
        if observed.state != "succeeded":
            checks.append(Check(name="runtime-warehouse", status="skipped"))
            return VerificationReport(checks=checks)
        workspace = self._workspace(ctx)
        app = workspace.get_app(request.receipt.operation_handles["app_name"])
        try:
            if not app or not isinstance(app.get("url"), str):
                raise ValueError("native app has no health origin")
            health = workspace.health_check(app["url"])
            good = health.get("status") == "ok" and health.get("warehouse") == "ok"
            checks.append(Check(name="runtime-warehouse", status="passed" if good else "failed"))
        except Exception:
            checks.append(
                Check(
                    name="runtime-warehouse",
                    status="failed",
                    evidence="authenticated application health check failed",
                )
            )
        return VerificationReport(checks=checks)
