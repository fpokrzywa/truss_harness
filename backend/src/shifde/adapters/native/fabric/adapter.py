"""`FabricAdapter`: the ten native operations for a Fabric solution, synchronous.

Ported from the FDE SDK `shifde_fabric/adapter.py` and `shifde_core/fabric_receipts.py`
(1fd902b) without `fabric.iq/v1` and the credential profile. Every native write is journaled
(`begin` before it, `complete` after it, the operation id recorded before polling) and attempted
once: an uncertain outcome becomes `NeedsAttention`. An item of a bound name that the target does
not own is never adopted (`ForeignItem`).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal  # Any: decoded Fabric JSON and journal receipts
from uuid import UUID

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
    DeploymentPlan,
    Diagnostic,
    EnvironmentSnapshot,
    FabricItem,
    FabricSolutionSpec,
    InspectRequest,
    NativeObservation,
    NativeStepReceipt,
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
from shifde.adapters.native.fabric.clients import FabricAPIError, FabricClient
from shifde.adapters.native.fabric.source import (
    ADAPTER_VERSION,
    COLLECTIONS,
    PLACEHOLDER,
    TOOLCHAIN_DIGEST,
    bindings,
    canonical_definition,
    definition,
    fabric_step,
    ordered,
    root,
    scaffold_files,
    validate_source,
)
from shifde.adapters.native.planning import (
    bindings_digest,
    seal_plan,
    validate_changes,
    validate_plan,
)

TERMINAL = "fabric-finalize"
VERIFICATION = "fabric-verification-submit"
RECEIPT_BUDGET = 16000
NOTEBOOK_DESCRIPTION = "Managed by SHIFDE"
# A receipt over the in-process fake is evidence only in development and test.
DEV_ENVS = ("development", "test")


def environment() -> str:
    """`SHIFDE_ENV` as the executor reads it: unset means production."""
    return os.environ.get("SHIFDE_ENV") or "production"


class ForeignItem(PermissionError):
    """An item of a bound display name exists in the workspace but this target does not own it."""

    code = "foreign_item"

    def __init__(self) -> None:
        super().__init__("a Fabric item of a bound name exists but is not owned by this target")


def fabric_spec(spec: ApplicationSpec | None) -> FabricSolutionSpec:
    """The spec, refused before any native call when it is of another kind (or absent)."""
    if not isinstance(spec, FabricSolutionSpec):
        raise ValueError("the Fabric adapter refuses a spec of another kind")
    return spec


def _description(item: FabricItem, spec: FabricSolutionSpec, files: dict[str, bytes]) -> str:
    if item.item_type == "Lakehouse":
        value = json.loads(files[root(spec) + item.definition_path]).get("description", "")
        return str(value)
    return NOTEBOOK_DESCRIPTION


def validate_receipt(
    receipt: ApplyReceipt,
    spec: FabricSolutionSpec,
    target: Target,
    *,
    env: str,
    completed: Callable[[str], dict[str, Any] | None] | None = None,
) -> None:
    """The receipt's shape against the spec and target, and (with `completed`) the journal.
    A `platform: fake` receipt is refused unless `env` is development or test."""
    workspace, names = bindings(spec, target)
    handles = receipt.operation_handles
    if receipt.tenant_id != target.tenant_id:
        raise PermissionError("Fabric receipt belongs to another tenant")
    if (
        receipt.target_id != target.id
        or handles.get("kind") != spec.kind
        or handles.get("workspace") != workspace
        or handles.get("verification") != spec.verification_notebook
        or handles.get("release_digest") != spec.release_digest
        or handles.get("platform") not in (("fabric", "fake") if env in DEV_ENVS else ("fabric",))
    ):
        raise ValueError("Fabric receipt/spec/target mismatch")
    resources = {r.key: r for r in receipt.resources}
    if len(resources) != len(receipt.resources) or set(resources) != {
        item.key for item in spec.items
    }:
        raise ValueError("Fabric receipt resource set is incomplete")
    allowed = {
        "workspace",
        "kind",
        "verification",
        "release_digest",
        "platform",
        "job_id",
        "verification_item",
    }
    prefix = "fabric:" + workspace + ":"
    for item in spec.items:
        binding = resources[item.key]
        if (
            binding.application_id != receipt.application_id
            or binding.target_id != target.id
            or binding.resource_type != "fabric." + item.item_type
            or not binding.native_id.startswith(prefix)
        ):
            raise ValueError("Fabric receipt resource context mismatch")
        UUID(binding.native_id.removeprefix(prefix))
        name_key, description_key = item.key + ":name", item.key + ":description"
        allowed.update({name_key, description_key})
        if (
            handles.get(name_key) != names[item.key]
            or description_key not in handles
            or len(handles[description_key]) > 256
        ):
            raise ValueError("Fabric receipt metadata evidence is missing")
        if item.item_type != "Lakehouse":
            key = item.key + ":definition"
            allowed.add(key)
            if not re.fullmatch(r"[a-f0-9]{64}", handles.get(key, "")):
                raise ValueError("Fabric definition evidence is missing")
        if completed:
            metadata = completed(fabric_step(item.key, "metadata-complete"))
            if (
                not metadata
                or binding.model_dump(mode="json") not in metadata["resources"]
                or metadata["handles"].get("metadata_digest")
                != digest(
                    {"displayName": handles[name_key], "description": handles[description_key]}
                )
            ):
                raise ValueError("Fabric receipt differs from accepted metadata checkpoint")
            if item.item_type != "Lakehouse":
                evidence = completed(fabric_step(item.key, "definition-complete")) or metadata
                if (
                    evidence["handles"].get("definition_digest")
                    != handles[item.key + ":definition"]
                ):
                    raise ValueError("Fabric receipt differs from accepted definition checkpoint")
    job_id = str(UUID(handles.get("job_id", "")))
    item_id = resources[spec.verification_notebook].native_id.removeprefix(prefix)
    if handles.get("verification_item") != item_id:
        raise ValueError("Fabric verification item evidence differs from spec")
    if completed:
        proof = completed(VERIFICATION)
        if not proof or proof["handles"].get("job_id") != job_id:
            raise ValueError("Fabric job evidence differs from accepted checkpoint")
    if set(handles) != allowed:
        raise ValueError("Fabric receipt has missing or unexpected evidence")


class FabricAdapter:
    kind = "fabric.solution/v1"

    def __init__(
        self, *, client_factory: Callable[[Target], FabricClient], env: str | None = None
    ) -> None:
        self.client_factory = client_factory
        self.env = env or environment()

    def _client(self, ctx: ReadContext) -> FabricClient:
        ctx.check_deadline()
        client = self.client_factory(ctx.target)
        client.deadline = ctx.deadline
        return client

    def capabilities(self, ctx: ReadContext) -> CapabilityReport:
        status: Literal["supported", "unknown"]
        try:
            client = self._client(ctx)
            workspace = str(UUID(ctx.target.bindings["workspace"]))
            info = client.workspace(workspace)
            status = "supported" if info.get("capacityId") else "unknown"
        except Exception:
            status = "unknown"
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            maturity="experimental",
            application_kinds=[self.kind],
            capabilities=[
                Capability(
                    operation="metadata",
                    status=status,
                    reason="workspace/capacity metadata only; item permissions and tenant "
                    "enablement remain native prerequisites",
                ),
                Capability(
                    operation="deployment",
                    status="unknown",
                    reason="experimental; native item writes require live acceptance",
                ),
            ],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    def scaffold(self, request: ScaffoldRequest, ctx: BuildContext) -> SourceArchive:
        spec = fabric_spec(request.spec)
        if request.template_version != ADAPTER_VERSION:
            raise ValueError("unsupported Fabric template version")
        return ctx.artifacts.put(ctx.tenant_id, create_archive(scaffold_files(spec)))

    def validate(self, request: ValidateRequest, ctx: BuildContext) -> ValidationReport:
        spec = fabric_spec(request.spec)
        try:
            validate_source(read_archive(ctx.artifacts.read(request.source, ctx.tenant_id)), spec)
            return ValidationReport(passed=True)
        except (ValueError, KeyError, TypeError):
            return ValidationReport(
                passed=False,
                diagnostics=[
                    Diagnostic(
                        code="INVALID_FABRIC_SOURCE",
                        message="Fabric source does not match the supported native definition "
                        "profile",
                    )
                ],
            )

    def build(self, request: BuildRequest, ctx: BuildContext) -> BuildArtifact:
        spec = fabric_spec(request.spec)
        if ctx.toolchain_digest != TOOLCHAIN_DIGEST or not self.validate(request, ctx).passed:
            raise ValueError("Fabric source/toolchain validation failed")
        return BuildArtifact(
            tenant_id=ctx.tenant_id,
            application_id=request.application_id,
            archive=request.source,
            source_digest=request.source.digest,
            spec_digest=digest(spec),
            spec=spec,
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=TOOLCHAIN_DIGEST,
        )

    @staticmethod
    def _artifact(artifact: BuildArtifact, ctx: ReadContext) -> FabricSolutionSpec:
        spec = fabric_spec(artifact.spec)
        if artifact.tenant_id != ctx.tenant_id or artifact.archive.tenant_id != ctx.tenant_id:
            raise PermissionError("Fabric artifact belongs to another tenant")
        if (
            artifact.spec_digest != digest(spec)
            or artifact.source_digest != artifact.archive.digest
            or artifact.adapter_version != ADAPTER_VERSION
            or artifact.toolchain_digest != TOOLCHAIN_DIGEST
        ):
            raise ValueError("Fabric artifact identity/toolchain mismatch")
        return spec

    @staticmethod
    def _identity(binding: ResourceBinding, workspace: str) -> str:
        prefix = f"fabric:{workspace}:"
        if not binding.native_id.startswith(prefix):
            raise PermissionError("Fabric resource belongs to another workspace")
        value = binding.native_id.removeprefix(prefix)
        if str(UUID(value)) != value:
            raise ValueError("Fabric resource id is not a canonical item id")
        return value

    @staticmethod
    def _binding(
        item: FabricItem, identity: str, workspace: str, application_id: UUID, ctx: ReadContext
    ) -> ResourceBinding:
        return ResourceBinding(
            key=item.key,
            native_id=f"fabric:{workspace}:{UUID(identity)}",
            resource_type="fabric." + item.item_type,
            application_id=application_id,
            target_id=ctx.target.id,
        )

    @staticmethod
    def _check_native(
        native: dict[str, Any],
        item: FabricItem,
        workspace: str,
        name: str,
        identity: str | None = None,
    ) -> str:
        if (
            native.get("workspaceId") != workspace
            or native.get("type") != item.item_type
            or native.get("displayName") != name
            or (identity and native.get("id") != identity)
        ):
            raise NeedsAttention("Fabric item identity/type/name changed")
        return str(UUID(native["id"]))

    def _owned(
        self, spec: FabricSolutionSpec, application_id: UUID, workspace: str, ctx: ReadContext
    ) -> dict[str, str]:
        """The owned item ids by key, checked against this context before any request."""
        owned = {r.key: r for r in ctx.owned_resources}
        if len(owned) != len(ctx.owned_resources):
            raise ValueError("duplicate Fabric owned resource keys")
        types = {item.key: item.item_type for item in spec.items}
        ids: dict[str, str] = {}
        for key, binding in owned.items():
            if binding.application_id != application_id or binding.target_id != ctx.target.id:
                raise PermissionError("Fabric owned resource context mismatch")
            ids[key] = self._identity(binding, workspace)
            if key in types and binding.resource_type != "fabric." + types[key]:
                raise PermissionError("Fabric owned resource context mismatch")
        if set(owned) - set(types):
            raise ValueError("removing managed Fabric items requires a separate migration")
        return ids

    def _snapshot(
        self,
        spec: FabricSolutionSpec,
        application_id: UUID,
        ctx: ReadContext,
        client: FabricClient,
    ) -> dict[str, Any]:
        workspace, names = bindings(spec, ctx.target)
        ids = self._owned(spec, application_id, workspace, ctx)
        info = client.workspace(workspace)
        if info.get("id") != workspace or not info.get("capacityId"):
            raise ValueError("Fabric workspace requires an assigned capacity")
        inventory = client.items(workspace)
        items: dict[str, Any] = {}
        state = {"workspace": workspace, "capacity": info["capacityId"], "items": items}
        for item in ordered(spec):
            identity = ids.get(item.key)
            if any(
                n.get("id") != identity and n.get("displayName") == names[item.key]
                for n in inventory
            ):
                raise ForeignItem
            if identity is None:
                items[item.key] = None
                continue
            native = client.item(workspace, item.item_type, identity)
            self._check_native(native, item, workspace, native.get("displayName", ""), identity)
            items[item.key] = {
                "id": identity,
                "name": native["displayName"],
                "type": native["type"],
                "description": native.get("description", ""),
            }
            if item.item_type != "Lakehouse":
                items[item.key]["definition"] = canonical_definition(
                    client.definition(workspace, item.item_type, identity), item.item_type
                )
        return state

    def inspect(self, request: InspectRequest, ctx: ReadContext) -> EnvironmentSnapshot:
        spec = fabric_spec(request.spec)
        state = self._snapshot(spec, request.application_id, ctx, self._client(ctx))
        return EnvironmentSnapshot(resources=list(ctx.owned_resources), fingerprint=digest(state))

    @staticmethod
    def _steps(spec: FabricSolutionSpec, state: dict[str, Any]) -> list[PlanStep]:
        steps: list[PlanStep] = []
        for item in ordered(spec):
            existing = bool(state["items"][item.key])
            phases = (
                ["metadata", "definition"]
                if existing and item.item_type != "Lakehouse"
                else ["metadata"]
            )
            for phase in phases:
                steps.append(
                    PlanStep(
                        key=fabric_step(item.key, phase + "-submit"),
                        action="update" if existing else "create",
                        resource_key=item.key,
                    )
                )
                steps.append(
                    PlanStep(
                        key=fabric_step(item.key, phase + "-complete"),
                        action="update",
                        resource_key=item.key,
                    )
                )
        steps.append(
            PlanStep(key=VERIFICATION, action="start", resource_key=spec.verification_notebook)
        )
        steps.append(PlanStep(key=TERMINAL, action="update", resource_key=spec.items[-1].key))
        return steps

    @staticmethod
    def _details(spec: FabricSolutionSpec, workspace: str, names: dict[str, str]) -> dict[str, Any]:
        return {
            "workspace": workspace,
            "names": names,
            "kind": spec.kind,
            "release_digest": spec.release_digest,
        }

    def plan(self, request: PlanRequest, ctx: ReadContext) -> DeploymentPlan:
        artifact = request.artifact
        spec = self._artifact(artifact, ctx)
        client = self._client(ctx)
        state = self._snapshot(spec, artifact.application_id, ctx, client)
        workspace, names = bindings(spec, ctx.target)
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
                toolchain_digest=TOOLCHAIN_DIGEST,
                snapshot=EnvironmentSnapshot(
                    resources=list(ctx.owned_resources), fingerprint=digest(state)
                ),
                steps=self._steps(spec, state),
                details=self._details(spec, workspace, names),
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    def apply(self, request: ApplyRequest, ctx: DeployContext) -> ApplyReceipt:
        artifact, plan = request.artifact, request.plan
        spec = fabric_spec(artifact.spec)
        validate_plan(plan, ctx.target)
        self._artifact(artifact, ctx)
        validate_changes(plan.steps)
        if (
            plan.artifact_id,
            plan.application_id,
            plan.artifact_digest,
            plan.spec_digest,
            plan.adapter_version,
            plan.toolchain_digest,
            ctx.toolchain_digest,
        ) != (
            artifact.id,
            artifact.application_id,
            artifact.archive.digest,
            artifact.spec_digest,
            ADAPTER_VERSION,
            TOOLCHAIN_DIGEST,
            TOOLCHAIN_DIGEST,
        ):
            raise ValueError("Fabric plan/artifact/toolchain mismatch")
        workspace, names = bindings(spec, ctx.target)
        if plan.details != self._details(spec, workspace, names):
            raise ValueError("Fabric planned bindings changed")
        if any(ctx.journal.has_intent(s.key) for s in plan.steps):
            raise NeedsAttention(
                "Fabric native intent requires reconciliation; submission cannot be replayed"
            )
        client = self._client(ctx)
        state = self._snapshot(spec, artifact.application_id, ctx, client)
        if digest(state) != plan.snapshot.fingerprint or plan.steps != self._steps(spec, state):
            raise ValueError("Fabric native state changed since planning")
        files = read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id))
        validate_source(files, spec)
        resources = {r.key: r for r in ctx.owned_resources}
        ids = {key: self._identity(value, workspace) for key, value in resources.items()}
        handles = {
            "workspace": workspace,
            "kind": spec.kind,
            "verification": spec.verification_notebook,
            "release_digest": spec.release_digest,
            "platform": client.platform,
        }
        preview_handles = dict(handles)
        for item in spec.items:
            preview_handles[item.key + ":name"] = names[item.key]
            preview_handles[item.key + ":description"] = _description(item, spec, files)
            if item.item_type != "Lakehouse":
                preview_handles[item.key + ":definition"] = "0" * 64
        preview_handles.update(job_id=PLACEHOLDER, verification_item=PLACEHOLDER)
        preview = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=[
                self._binding(item, PLACEHOLDER, workspace, artifact.application_id, ctx)
                for item in spec.items
            ],
            operation_handles=preview_handles,
        )
        if len(json.dumps(preview.model_dump(mode="json"))) > RECEIPT_BUDGET:
            raise ValueError("Fabric receipt exceeds hosted checkpoint budget; split the solution")

        def begin(key: str) -> None:
            ctx.check_deadline()
            if not ctx.journal.intent(key, {"plan": plan.digest}):
                raise NeedsAttention("Fabric native step was already submitted")

        def complete(key: str, native_handles: dict[str, str] | None = None) -> None:
            ctx.journal.complete(
                key,
                NativeStepReceipt(
                    step_key=key,
                    plan_digest=plan.digest,
                    resources=list(resources.values()),
                    handles=native_handles or {},
                ).model_dump(mode="json"),
            )

        for item in ordered(spec):
            wanted = definition(item, spec, files, workspace, names, ids)
            metadata = {
                "displayName": names[item.key],
                "description": _description(item, spec, files),
            }
            existing = item.key in ids
            for phase in ["metadata", "definition"] if existing and wanted else ["metadata"]:
                submit_key = fabric_step(item.key, phase + "-submit")
                complete_key = fabric_step(item.key, phase + "-complete")
                begin(submit_key)
                base = f"/v1/workspaces/{workspace}/{COLLECTIONS[item.item_type]}"
                if phase == "definition":
                    accepted = client.request(
                        "POST",
                        f"{base}/{ids[item.key]}/updateDefinition",
                        {"definition": wanted},
                        params={"updateMetadata": "false"},
                    )
                elif existing:
                    accepted = client.request("PATCH", f"{base}/{ids[item.key]}", metadata)
                else:
                    payload: dict[str, Any] = dict(metadata)
                    if wanted:
                        payload["definition"] = wanted
                    elif json.loads(files[root(spec) + item.definition_path]).get(
                        "enableSchemas", True
                    ):
                        payload["creationPayload"] = {"enableSchemas": True}
                    accepted = client.request("POST", base, payload)
                    if accepted["status"] != 202:
                        identity = self._check_native(
                            accepted["body"], item, workspace, names[item.key]
                        )
                        ids[item.key] = identity
                        resources[item.key] = self._binding(
                            item, identity, workspace, artifact.application_id, ctx
                        )
                complete(
                    submit_key,
                    {"operation_id": accepted["operation_id"]}
                    if accepted.get("operation_id")
                    else {},
                )
                begin(complete_key)
                result = client.wait(accepted, result_required=not existing)
                if item.key not in ids:
                    identity = self._check_native(result, item, workspace, names[item.key])
                    ids[item.key] = identity
                    resources[item.key] = self._binding(
                        item, identity, workspace, artifact.application_id, ctx
                    )
                evidence = {"metadata_digest": digest(metadata)}
                if wanted:
                    evidence["definition_digest"] = canonical_definition(wanted, item.item_type)
                complete(complete_key, evidence)
                native = client.item(workspace, item.item_type, ids[item.key])
                self._check_native(native, item, workspace, names[item.key], ids[item.key])
                if native.get("description", "") != metadata["description"]:
                    raise NeedsAttention("Fabric metadata readback differs from source")
            handles[item.key + ":name"] = names[item.key]
            handles[item.key + ":description"] = metadata["description"]
            if wanted:
                expected = canonical_definition(wanted, item.item_type)
                readback = client.definition(workspace, item.item_type, ids[item.key])
                if canonical_definition(readback, item.item_type) != expected:
                    raise NeedsAttention("Fabric definition readback differs from deployed source")
                handles[item.key + ":definition"] = expected
        begin(VERIFICATION)
        identity = ids[spec.verification_notebook]
        accepted = client.request(
            "POST",
            f"/v1/workspaces/{workspace}/items/{identity}/jobs/RunNotebook/instances",
            {},
            execution=True,
        )
        if accepted["status"] != 202:
            raise NeedsAttention("Fabric notebook execution acceptance is unknown")
        job = client.job_id(accepted["location"], workspace, identity)
        handles.update(job_id=job, verification_item=identity)
        complete(VERIFICATION, {"job_id": job})
        client.pause(accepted["retry_after"])
        receipt = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=list(resources.values()),
            operation_handles=handles,
        )
        validate_receipt(receipt, spec, ctx.target, env=self.env, completed=ctx.journal.completed)
        begin(TERMINAL)
        ctx.journal.complete(TERMINAL, receipt.model_dump(mode="json"))
        return receipt

    def _check_receipt(
        self, receipt: ApplyReceipt, spec: FabricSolutionSpec, ctx: ReadContext
    ) -> str:
        """The receipt's workspace, refused for another tenant or target before any request."""
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("Fabric receipt belongs to another tenant or target")
        validate_receipt(receipt, spec, ctx.target, env=self.env)
        return receipt.operation_handles["workspace"]

    def _items_drift(
        self, receipt: ApplyReceipt, workspace: str, client: FabricClient
    ) -> str | None:
        """Why the receipt's items differ from the workspace, or `None` when they match."""
        for binding in receipt.resources:
            item_type = binding.resource_type.removeprefix("fabric.")
            identity = self._identity(binding, workspace)
            native = client.item(workspace, item_type, identity)
            if (
                native.get("id") != identity
                or native.get("workspaceId") != workspace
                or native.get("type") != item_type
                or native.get("displayName") != receipt.operation_handles[binding.key + ":name"]
                or native.get("description", "")
                != receipt.operation_handles[binding.key + ":description"]
            ):
                return "Fabric native item drift requires reconciliation"
            expected = receipt.operation_handles.get(binding.key + ":definition")
            if expected and (
                canonical_definition(client.definition(workspace, item_type, identity), item_type)
                != expected
            ):
                return "Fabric native definition drift requires reconciliation"
        return None

    @staticmethod
    def _job(receipt: ApplyReceipt, workspace: str, client: FabricClient) -> dict[str, Any]:
        identity = str(UUID(receipt.operation_handles["verification_item"]))
        job = str(UUID(receipt.operation_handles["job_id"]))
        body: dict[str, Any] = client.request(
            "GET", f"/v1/workspaces/{workspace}/items/{identity}/jobs/instances/{job}", read=True
        )["body"]
        return body

    def observe(self, request: ObserveRequest, ctx: ReadContext) -> NativeObservation:
        receipt = request.receipt
        spec = fabric_spec(request.spec)
        workspace = self._check_receipt(receipt, spec, ctx)
        client = self._client(ctx)
        drift = self._items_drift(receipt, workspace, client)
        if drift:
            return NativeObservation(
                state="unknown", resources=receipt.resources, diagnostics=[drift]
            )
        job = self._job(receipt, workspace, client)
        status = job.get("status")
        if status == "Completed":
            if job.get("exitValue") != spec.release_digest:
                return NativeObservation(
                    state="failed",
                    resources=receipt.resources,
                    diagnostics=["verification job exit value differs from the release digest"],
                )
            return NativeObservation(state="succeeded", resources=receipt.resources)
        state: Literal["running", "failed", "unknown"] = (
            "failed"
            if status in {"Failed", "Cancelled", "Deduped"}
            else "running"
            if status in {"NotStarted", "InProgress"}
            else "unknown"
        )
        return NativeObservation(state=state, resources=receipt.resources)

    def verify(self, request: VerifyRequest, ctx: ReadContext) -> VerificationReport:
        receipt = request.receipt
        spec = fabric_spec(request.spec)
        workspace = self._check_receipt(receipt, spec, ctx)
        client = self._client(ctx)
        try:
            drift = self._items_drift(receipt, workspace, client)
        except (FabricAPIError, NeedsAttention, ValueError, KeyError):
            drift = "Fabric native items could not be read back"
        try:
            job = self._job(receipt, workspace, client)
            healthy = (
                job.get("status") == "Completed" and job.get("exitValue") == spec.release_digest
            )
        except (FabricAPIError, NeedsAttention, ValueError):
            healthy = False
        return VerificationReport(
            checks=[
                Check(
                    name="native-items",
                    status="failed" if drift else "passed",
                    evidence=drift or "items, metadata and notebook definition match the receipt",
                ),
                Check(
                    name="runtime-health",
                    status="passed" if healthy else "failed",
                    evidence="verification job completed with the release digest"
                    if healthy
                    else "verification job did not complete with the release digest",
                ),
            ]
        )

    def reconcile(self, request: ReconcileRequest, ctx: ReadContext) -> ReconciliationResult:
        if request.plan.tenant_id != ctx.tenant_id or request.plan.target_id != ctx.target.id:
            raise PermissionError("plan context mismatch")
        return ReconciliationResult(
            state="ambiguous",
            evidence="Inspect durable Fabric operation/job receipts and native state; never "
            "resubmit an uncertain mutation automatically",
            resources=list(ctx.owned_resources),
        )
