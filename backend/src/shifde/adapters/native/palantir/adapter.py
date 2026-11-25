"""`PalantirMarketplaceAdapter`: the ten native operations for a Marketplace installation.

It installs the operator-approved product version named on the target (Call S8-H). Every write is
attempted once and each accepted RID is checkpointed before the next request; nothing is ever
aborted, uninstalled, deleted or rolled back, and an installation with an Ontology override is
refused at plan, apply and observe. The client's `authorize` runs before every request. An apply
asks for its token before the intent of each writing step too (`bearer`: `authorize`, and a mint
when the credentials changed), so a refusal there leaves no intent; one that comes only at the
write's own check, after the intent, leaves it open with no write sent.

In verify the job fails, with no report, when `authorize` or the credential provider refuses, when
the token endpoint answers anything but a token (a 4xx, a 5xx, an unreadable body) and when the
credentials change while a token is minted. Any other error of the `ontology-read` or
`frontend-asset` check is that check's failure, a timeout or lost connection of the token request
included. Before those checks, in the reads of the observation, every error is the job's failure.

The accepted toolchain digest is the caller's (the executor's names its platform, so a plan made on
the fake is refused on a real stack before any request). Every verification report carries
`content-check: not_run`: the product's content is not derived from the release, and no check here
reads it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal  # Any: decoded Foundry JSON
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
    FoundryMarketplaceSpec,
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
from shifde.adapters.native.palantir.clients import PalantirClient
from shifde.adapters.native.palantir.receipts import (
    CREATE,
    SUBMIT,
    installation_rid,
    job_payload,
    job_rid,
    validate_palantir_receipt,
)
from shifde.adapters.native.palantir.source import (
    ADAPTER_VERSION,
    TERMINAL,
    TOOLCHAIN_DIGEST,
    Bindings,
    bindings,
    files_for,
    installation_suffix,
    native_id,
    palantir_spec,
    validate_source,
)
from shifde.adapters.native.planning import (
    bindings_digest,
    seal_plan,
    validate_changes,
    validate_plan,
)

RESOURCE_TYPE = "palantir.marketplace-installation"
CONTENT_CHECK = Check(
    name="content-check",
    status="not_run",
    required=False,
    evidence="the product content is not derived from the release and was not checked",
)
RUNNING = frozenset({"PREPARING", "IN_PROGRESS", "BUILDING", "ABORTING"})


def _preview(b: Bindings) -> None:
    if not b.allow_preview_apis:
        raise PermissionError("Foundry preview APIs are not allowed for this target")


def _check_installation(native: dict[str, Any], b: Bindings, identity: str) -> None:
    if native.get("ontologyRid"):
        raise NeedsAttention(
            "Foundry installation has an Ontology write destination outside this profile"
        )
    if (
        native.get("rid") != identity
        or native.get("marketplaceRid") != b.marketplace_rid
        or native.get("productId") != b.product_id
        or native.get("parentFolderRid") != b.parent_folder_rid
        or native.get("namespaceRid") != b.namespace_rid
    ):
        raise NeedsAttention("Foundry installation identity or bindings changed")


def _steps() -> list[PlanStep]:
    return [
        PlanStep(key=key, action=action, resource_key="installation")
        for key, action in ((CREATE, "create"), (SUBMIT, "update"), (TERMINAL, "update"))
    ]


class PalantirMarketplaceAdapter:
    kind = "palantir.marketplace/v1"

    def __init__(
        self,
        *,
        client_factory: Callable[[Target], PalantirClient],
        toolchain_digest: str = TOOLCHAIN_DIGEST,
    ) -> None:
        self.client_factory = client_factory
        self.toolchain_digest = toolchain_digest

    def _client(self, ctx: ReadContext) -> PalantirClient:
        ctx.check_deadline()
        client = self.client_factory(ctx.target)
        client.deadline = ctx.deadline
        return client

    @staticmethod
    def _owned(application_id: UUID, ctx: ReadContext) -> str | None:
        """The owned installation's RID, after checking the binding belongs to `ctx`."""
        if not ctx.owned_resources:
            return None
        if len(ctx.owned_resources) != 1:
            raise ValueError("Foundry delivery owns exactly one installation")
        binding = ctx.owned_resources[0]
        prefix = "palantir:" + digest(ctx.target.host) + ":"
        if (
            binding.key != "installation"
            or binding.resource_type != RESOURCE_TYPE
            or binding.application_id != application_id
            or binding.target_id != ctx.target.id
            or not binding.native_id.startswith(prefix)
        ):
            raise PermissionError("Foundry resource ownership mismatch")
        return installation_rid(binding.native_id.removeprefix(prefix))

    def _snapshot(
        self, b: Bindings, application_id: UUID, ctx: ReadContext, client: PalantirClient
    ) -> dict[str, Any]:
        identity = self._owned(application_id, ctx)
        state: dict[str, Any] = {"schema": client.schema(b), "installation": None}
        if identity:
            native = client.get_installation(identity)
            _check_installation(native, b, identity)
            state["installation"] = native
        return state

    def capabilities(self, ctx: ReadContext) -> CapabilityReport:
        allowed = ctx.target.bindings.get("allow_preview_apis") == "true"
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            maturity="experimental",
            application_kinds=[self.kind],
            capabilities=[
                Capability(
                    operation="deployment",
                    status="unknown",
                    reason="Experimental Marketplace preview; "
                    "only locally approved published product versions",
                )
                if allowed
                else Capability(
                    operation="deployment",
                    status="unsupported",
                    reason="Foundry preview APIs are not allowed for this target",
                )
            ],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    def inspect(self, request: InspectRequest, ctx: ReadContext) -> EnvironmentSnapshot:
        if request.spec is not None:
            palantir_spec(request.spec)
        if request.app_name != installation_suffix(request.application_id, ctx.target.id):
            raise PermissionError(
                "installation suffix is not the one derived for this application and target"
            )
        b = bindings(ctx.target)
        _preview(b)
        state = self._snapshot(b, request.application_id, ctx, self._client(ctx))
        return EnvironmentSnapshot(resources=list(ctx.owned_resources), fingerprint=digest(state))

    def scaffold(self, request: ScaffoldRequest, ctx: BuildContext) -> SourceArchive:
        spec = palantir_spec(request.spec)
        if request.template_version != ADAPTER_VERSION:
            raise ValueError("unsupported Foundry template version")
        return ctx.artifacts.put(ctx.tenant_id, create_archive(files_for(spec)))

    def validate(self, request: ValidateRequest, ctx: BuildContext) -> ValidationReport:
        spec = palantir_spec(request.spec)
        try:
            validate_source(read_archive(ctx.artifacts.read(request.source, ctx.tenant_id)), spec)
            return ValidationReport(passed=True)
        except (ValueError, KeyError, TypeError):
            return ValidationReport(
                passed=False,
                diagnostics=[
                    Diagnostic(
                        code="INVALID_PALANTIR_SOURCE",
                        message="Expected an immutable Marketplace deployment descriptor",
                    )
                ],
            )

    def build(self, request: BuildRequest, ctx: BuildContext) -> BuildArtifact:
        spec = palantir_spec(request.spec)
        if ctx.toolchain_digest != self.toolchain_digest or not self.validate(request, ctx).passed:
            raise ValueError("Foundry descriptor/toolchain validation failed")
        # Nothing is built for the platform: the descriptor names an already published version.
        return BuildArtifact(
            tenant_id=ctx.tenant_id,
            application_id=request.application_id,
            archive=request.source,
            source_digest=request.source.digest,
            spec_digest=digest(spec),
            spec=spec,
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=self.toolchain_digest,
        )

    def _artifact(self, artifact: BuildArtifact, ctx: ReadContext) -> FoundryMarketplaceSpec:
        spec = palantir_spec(artifact.spec)
        if artifact.tenant_id != ctx.tenant_id or artifact.archive.tenant_id != ctx.tenant_id:
            raise PermissionError("artifact belongs to another tenant")
        if (
            artifact.spec_digest != digest(spec)
            or artifact.source_digest != artifact.archive.digest
            or artifact.adapter_version != ADAPTER_VERSION
            or artifact.toolchain_digest != self.toolchain_digest
        ):
            raise ValueError("Foundry artifact identity/toolchain mismatch")
        return spec

    def plan_details(self, toolchain_digest: str | None, ctx: ReadContext) -> dict[str, Any]:
        """The details `plan` seals, read from the target's current state, for a plan sealed under
        `toolchain_digest`; another toolchain's plan is refused before any request."""
        if toolchain_digest != self.toolchain_digest:
            raise ValueError("Foundry plan/toolchain mismatch")
        b = bindings(ctx.target)
        _preview(b)
        return {"schema_digest": self._client(ctx).schema(b)}

    def plan(self, request: PlanRequest, ctx: ReadContext) -> DeploymentPlan:
        artifact = request.artifact
        self._artifact(artifact, ctx)
        b = bindings(ctx.target)
        _preview(b)
        state = self._snapshot(b, artifact.application_id, ctx, self._client(ctx))
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
                toolchain_digest=self.toolchain_digest,
                snapshot=EnvironmentSnapshot(
                    resources=list(ctx.owned_resources), fingerprint=digest(state)
                ),
                steps=_steps(),
                details={"schema_digest": state["schema"]},
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    def apply(self, request: ApplyRequest, ctx: DeployContext) -> ApplyReceipt:
        artifact, plan = request.artifact, request.plan
        spec = self._artifact(artifact, ctx)
        if plan.tenant_id != ctx.tenant_id or plan.target_id != ctx.target.id:
            raise PermissionError("plan belongs to another tenant or target")
        b = bindings(ctx.target)
        _preview(b)
        validate_plan(plan, ctx.target)
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
            self.toolchain_digest,
            self.toolchain_digest,
        ):
            raise ValueError("Foundry plan/artifact/toolchain mismatch")
        if any(ctx.journal.has_intent(step.key) for step in plan.steps):
            raise NeedsAttention("Foundry native intent cannot be replayed; reconcile first")
        validate_source(read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id)), spec)
        client = self._client(ctx)
        state = self._snapshot(b, artifact.application_id, ctx, client)
        if (
            digest(state) != plan.snapshot.fingerprint
            or plan.steps != _steps()
            or plan.details != {"schema_digest": state["schema"]}
        ):
            raise ValueError("Foundry environment changed since planning")
        extra = {"platform": "fake"} if client.platform == "fake" else {}

        def begin(key: str) -> None:
            ctx.check_deadline()
            if not ctx.journal.intent(key, {"plan": plan.digest}):
                raise NeedsAttention("Foundry submission already attempted")

        def complete(key: str, binding: ResourceBinding, handles: dict[str, str]) -> None:
            ctx.journal.complete(
                key,
                NativeStepReceipt(
                    step_key=key,
                    plan_digest=plan.digest,
                    resources=[binding],
                    handles={**handles, **extra},
                ).model_dump(mode="json"),
            )

        client.bearer()
        begin(CREATE)
        native = state["installation"] or client.create_installation(
            {
                "marketplaceRid": b.marketplace_rid,
                "productId": b.product_id,
                "parentFolderRid": b.parent_folder_rid,
                "installationSuffix": installation_suffix(artifact.application_id, ctx.target.id),
                "targetVersionHint": {"type": "versionId", "versionId": b.version_id},
            }
        )
        identity = installation_rid(native.get("rid"))
        binding = ResourceBinding(
            key="installation",
            native_id=native_id(ctx.target.host, identity),
            resource_type=RESOURCE_TYPE,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
        )
        # Persist the accepted identity before readback: metadata may be unavailable after it.
        complete(CREATE, binding, {"installation": identity})
        _check_installation(client.get_installation(identity), b, identity)
        payload = job_payload(identity, b.version_id)
        client.bearer()
        begin(SUBMIT)
        job = job_rid(client.submit_job(payload).get("rid"))
        handles = {"installation": identity, "job_id": job, "request_digest": digest(payload)}
        complete(SUBMIT, binding, handles)
        receipt = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=[binding],
            operation_handles={
                **handles,
                "schema_digest": state["schema"],
                "spec_digest": digest(spec),
                **extra,
            },
        )
        validate_palantir_receipt(
            receipt,
            spec,
            ctx.target,
            platform=client.platform,
            completed=ctx.journal.completed,
            plan=plan,
        )
        begin(TERMINAL)
        ctx.journal.complete(TERMINAL, receipt.model_dump(mode="json"))
        return receipt

    def observe(self, request: ObserveRequest, ctx: ReadContext) -> NativeObservation:
        return self._observe(request.receipt, request.spec, ctx)

    def _observe(
        self, receipt: ApplyReceipt, requested: ApplicationSpec | None, ctx: ReadContext
    ) -> NativeObservation:
        spec = palantir_spec(requested)
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("Foundry receipt belongs to another tenant or target")
        b = bindings(ctx.target)
        _preview(b)
        client = self._client(ctx)
        validate_palantir_receipt(receipt, spec, ctx.target, platform=client.platform)
        h = receipt.operation_handles
        _check_installation(client.get_installation(h["installation"]), b, h["installation"])
        job = client.get_job(h["job_id"])
        if job.get("rid") != h["job_id"]:
            raise NeedsAttention("Foundry job identity changed")
        status = job.get("status")
        state: Literal["running", "succeeded", "failed", "unknown"]
        if status == "SUCCEEDED":
            installation = client.get_installation(h["installation"])
            _check_installation(installation, b, h["installation"])
            version = (installation.get("latestTargetVersion") or {}).get("versionId")
            pinned = version == b.version_id and client.schema(b) == h["schema_digest"]
            state = "succeeded" if pinned else "unknown"
        elif status in RUNNING:
            state = "running"
        elif status == "FAILED":
            state = "failed"
        else:
            state = "unknown"
        return NativeObservation(state=state, resources=receipt.resources)

    def verify(self, request: VerifyRequest, ctx: ReadContext) -> VerificationReport:
        observed = self._observe(request.receipt, request.spec, ctx)
        if observed.state != "succeeded":
            return VerificationReport(
                checks=[
                    Check(
                        name="native-version",
                        status="failed",
                        evidence=f"installation job is {observed.state}",
                    ),
                    Check(name="ontology-read", status="skipped"),
                    Check(name="frontend-asset", status="skipped"),
                    CONTENT_CHECK,
                ]
            )
        spec = palantir_spec(request.spec)
        b = bindings(ctx.target)
        client = self._client(ctx)
        checks = [
            Check(
                name="native-version",
                status="passed",
                evidence="installation is at the pinned version_id with an unchanged schema",
            )
        ]
        try:
            client.schema(b)
            client.read_one_object(b)
            checks.append(
                Check(name="ontology-read", status="passed", evidence="one object read, read-only")
            )
        except Exception:
            if client.refused:
                raise
            checks.append(
                Check(
                    name="ontology-read",
                    status="failed",
                    evidence="read-only ontology object read failed",
                )
            )
        try:
            matches = client.frontend_asset_sha256(b, spec) == b.frontend_asset_sha256
            checks.append(
                Check(
                    name="frontend-asset",
                    status="passed" if matches else "failed",
                    evidence="frontend asset SHA-256 matches the binding"
                    if matches
                    else "frontend asset SHA-256 differs from the binding",
                )
            )
        except Exception:
            if client.refused:
                raise
            checks.append(
                Check(
                    name="frontend-asset",
                    status="failed",
                    evidence="frontend asset could not be fetched",
                )
            )
        return VerificationReport(checks=[*checks, CONTENT_CHECK])

    def reconcile(self, request: ReconcileRequest, ctx: ReadContext) -> ReconciliationResult:
        if request.plan.tenant_id != ctx.tenant_id or request.plan.target_id != ctx.target.id:
            raise PermissionError("plan context mismatch")
        return ReconciliationResult(
            state="ambiguous",
            resources=list(ctx.owned_resources),
            evidence="Inspect checkpointed installation/job RIDs and native audit logs; "
            "uncertain non-idempotent writes are never replayed",
        )
