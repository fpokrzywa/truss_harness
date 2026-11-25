"""`SnowflakeNativeAdapter`: the ten native operations for a Snowflake Native App, synchronous
(FDE SDK 1fd902b `shifde_snowflake.adapter`, SP6 design 6.2).

One version `V1` per package and a new patch per release (Call S6-J). Each of the eight steps is a
journal checkpoint: the intent is recorded before the write and the receipt after it, so a write
with an unknown outcome is never sent again (`NeedsAttention`). Names come only from the target
bindings, objects of the bound names that this target does not own are never adopted, ownership
is re-checked after every step and nothing is ever dropped.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal  # Any: SQL API rows and JSON step receipts
from uuid import UUID

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    NeedsAttention,
    ReadContext,
    read_archive,
)
from shifde.adapters.native.contracts import (
    EVIDENCE_MAX,
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
    SnowflakeApplicationSpec,
    SourceArchive,
    Target,
    ValidateRequest,
    ValidationReport,
    VerificationReport,
    VerifyRequest,
    digest,
    utcnow,
)
from shifde.adapters.native.planning import (
    bindings_digest,
    seal_plan,
    validate_changes,
    validate_plan,
)
from shifde.adapters.native.snowflake.clients import SqlGateway, StageCopier
from shifde.adapters.native.snowflake.source import (
    ADAPTER_VERSION,
    CLI_VERSION,
    PATCH_LIMIT,
    REQUIRED_FILES,
    SCHEMA,
    STAGE,
    TERMINAL_STEP,
    VERSION,
    literal,
    names_for,
    quoted,
    stage_location,
    stage_name,
    validate_source,
)

STEPS = (
    "package-create",
    "schema-create",
    "stage-create",
    "stage-upload",
    "setup-validate",
    "version-create",
    "application-apply",
    TERMINAL_STEP,
)
RESOURCE_KEYS = ("package", "stage", "version", "application")
# The resource key of each native resource type `_resources` binds (an executor maps the target's
# owned resources back to these keys).
RESOURCE_KEY_OF_TYPE = {
    "snowflake.application_package": "package",
    "snowflake.stage": "stage",
    "snowflake.application_version": "version",
    "snowflake.application": "application",
}
SESSION = "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_ROLE() AS ROLE"
RECONCILE_EVIDENCE = "inspect query history; no automatic replay"
FAILED_STATES = ("FAILED", "DISABLED")

Row = dict[str, Any]  # Any: one SQL API row, values as the API gives them


class SnowflakePatchLimit(ValueError):
    """Version V1 of the package has no free patch (0..129 are used)."""

    code = "snowflake_patch_limit"

    def __init__(self) -> None:
        super().__init__("this package has no free patch; a new package is needed")


def snowflake_spec(spec: ApplicationSpec) -> SnowflakeApplicationSpec:
    """`spec`, refused with `ValueError` unless it is a Snowflake Native App spec."""
    if not isinstance(spec, SnowflakeApplicationSpec):
        raise ValueError("the Snowflake adapter requires a snowflake.native-app/v1 spec")
    return spec


def native_id(kind: str, name: str, created: object, account: str) -> str:
    return f"snowflake:{kind}:{name}:" + digest([account, name, str(created)])


def _exact(rows: list[Row], name: str) -> Row | None:
    return next((row for row in rows if row.get("name") == name), None)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _patches(state: dict[str, Any]) -> list[int]:  # Any: snapshot JSON
    return sorted(
        int(v["patch"])
        for v in state["versions"]
        if v.get("version") == VERSION and not v.get("dropped_on")
    )


def relevant(state: dict[str, Any]) -> dict[str, Any]:  # Any: snapshot JSON
    """The identity and state facts a plan is sealed over (its snapshot fingerprint)."""
    fields = {
        "package": ("name", "created_on", "owner"),
        "application": (
            "name",
            "created_on",
            "owner",
            "source",
            "source_type",
            "version",
            "patch",
            "upgrade_state",
        ),
        "stage": ("name", "created_on", "owner", "schema_name"),
    }
    output: dict[str, Any] = {"account": state["account"], "role": state["role"]}  # Any: JSON
    for key, allowed in fields.items():
        row = state.get(key)
        output[key] = {k: row.get(k) for k in allowed} if row else None
    output["versions"] = sorted(
        (
            {k: v.get(k) for k in ("version", "patch", "label", "created_on", "dropped_on")}
            for v in state["versions"]
        ),
        key=lambda v: (str(v["version"]), int(v["patch"])),
    )
    return output


class SnowflakeNativeAdapter:
    def __init__(
        self,
        *,
        gateway_factory: Callable[[Target], SqlGateway],
        copier_factory: Callable[[Target], StageCopier],
    ) -> None:
        self.gateway_factory = gateway_factory
        self.copier_factory = copier_factory

    def _gateway(self, ctx: ReadContext) -> SqlGateway:
        ctx.check_deadline()
        return self.gateway_factory(ctx.target)

    # ------------------------------------------------------------------ state
    @staticmethod
    def _snapshot(gateway: SqlGateway, names: dict[str, str]) -> dict[str, Any]:  # Any: JSON
        session = gateway.execute(SESSION).rows[0]
        package = _exact(
            gateway.execute("SHOW APPLICATION PACKAGES LIKE " + literal(names["package"])).rows,
            names["package"],
        )
        application = _exact(
            gateway.execute("SHOW APPLICATIONS LIKE " + literal(names["application"])).rows,
            names["application"],
        )
        versions: list[Row] = []
        stage = None
        if package:
            versions = gateway.execute(
                "SHOW VERSIONS IN APPLICATION PACKAGE " + quoted(names["package"])
            ).rows
            stages = gateway.execute(
                f"SHOW STAGES LIKE {literal(STAGE)} IN DATABASE {quoted(names['package'])}"
            ).rows
            stage = next(
                (s for s in stages if s.get("name") == STAGE and s.get("schema_name") == SCHEMA),
                None,
            )
        return relevant(
            {
                "account": str(session.get("account")),
                "role": str(session.get("role")),
                "package": package,
                "application": application,
                "stage": stage,
                "versions": versions,
            }
        )

    @staticmethod
    def _binding(
        key: str, kind: str, name: str, created: object, account: str, app_id: UUID, target: UUID
    ) -> ResourceBinding:
        if not created:
            raise NeedsAttention("native creation identity is missing")
        return ResourceBinding(
            key=key,
            resource_type="snowflake." + kind,
            native_id=native_id(kind, name, created, account),
            application_id=app_id,
            target_id=target,
        )

    def _resources(
        self, state: dict[str, Any], names: dict[str, str], app_id: UUID, ctx: ReadContext
    ) -> dict[str, ResourceBinding]:  # Any: snapshot JSON
        account, target = state["account"], ctx.target.id
        found: dict[str, ResourceBinding] = {}
        if state["package"]:
            found["package"] = self._binding(
                "package",
                "application_package",
                names["package"],
                state["package"]["created_on"],
                account,
                app_id,
                target,
            )
        if state["stage"]:
            found["stage"] = self._binding(
                "stage",
                "stage",
                stage_name(names["package"]),
                state["stage"]["created_on"],
                account,
                app_id,
                target,
            )
        first = next(
            (v for v in state["versions"] if v.get("version") == VERSION and not v["dropped_on"]),
            None,
        )
        if first:
            found["version"] = self._binding(
                "version",
                "application_version",
                f"{names['package']}:{VERSION}",
                first["created_on"],
                account,
                app_id,
                target,
            )
        if state["application"]:
            found["application"] = self._binding(
                "application",
                "application",
                names["application"],
                state["application"]["created_on"],
                account,
                app_id,
                target,
            )
        return found

    def _check_ownership(
        self, state: dict[str, Any], names: dict[str, str], app_id: UUID, ctx: ReadContext
    ) -> dict[str, ResourceBinding]:  # Any: snapshot JSON
        """The bindings of the objects found; refuses an unowned one and a vanished owned one."""
        found = self._resources(state, names, app_id, ctx)
        for binding in found.values():
            if binding not in ctx.owned_resources:
                raise PermissionError("refusing to adopt an unowned Snowflake object")
        application = state["application"]
        if application and (
            application.get("source") != names["package"]
            or application.get("source_type") != "APPLICATION PACKAGE"
        ):
            raise PermissionError("installed application belongs to another package")
        if any(
            r.key in RESOURCE_KEYS
            and r.key not in found
            and r.application_id == app_id
            and r.target_id == ctx.target.id
            for r in ctx.owned_resources
        ):
            raise ValueError("owned Snowflake object is missing; resolve drift before redeploying")
        return found

    @staticmethod
    def _check_artifact(artifact: BuildArtifact, ctx: ReadContext) -> None:
        if artifact.tenant_id != ctx.tenant_id or artifact.archive.tenant_id != ctx.tenant_id:
            raise PermissionError("artifact belongs to another tenant")
        snowflake_spec(artifact.spec)
        if artifact.adapter_version != ADAPTER_VERSION or artifact.spec_digest != digest(
            artifact.spec
        ):
            raise ValueError("artifact spec or adapter version mismatch")

    @staticmethod
    def _check_receipt(receipt: ApplyReceipt, ctx: ReadContext) -> None:
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("receipt belongs to another tenant or target")
        if not receipt.resources:
            raise ValueError("receipt has no native resources")

    # ------------------------------------------------------------------ read operations
    def capabilities(self, ctx: ReadContext) -> CapabilityReport:
        items = []
        try:
            self.copier_factory(ctx.target).check_version()
            items.append(
                Capability(operation="cli", status="supported", reason=f"pinned CLI {CLI_VERSION}")
            )
        except (OSError, ValueError, RuntimeError):
            items.append(
                Capability(operation="cli", status="unsupported", reason="pinned CLI unavailable")
            )
        try:
            self._gateway(ctx).execute(SESSION)
            items.append(
                Capability(
                    operation="metadata",
                    status="supported",
                    reason="SQL API session verified; deploy privileges enforced by native apply",
                )
            )
        except Exception:
            items.append(
                Capability(
                    operation="metadata",
                    status="unknown",
                    reason="account metadata could not be verified",
                )
            )
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            maturity="experimental",
            application_kinds=["snowflake.native-app/v1"],
            capabilities=items,
            expires_at=utcnow() + timedelta(minutes=5),
        )

    def inspect(self, request: InspectRequest, ctx: ReadContext) -> EnvironmentSnapshot:
        if request.spec is not None:
            snowflake_spec(request.spec)
        names = names_for(ctx.target)
        if request.app_name.upper() != names["application"]:
            raise PermissionError("app name is not the one bound for this target")
        state = self._snapshot(self._gateway(ctx), names)
        resources = self._resources(state, names, request.application_id, ctx)
        return EnvironmentSnapshot(resources=list(resources.values()), fingerprint=digest(state))

    def scaffold(self, request: ScaffoldRequest, ctx: BuildContext) -> SourceArchive:
        raise ValueError("the Snowflake Native App source is always rendered; there is no scaffold")

    def validate(self, request: ValidateRequest, ctx: BuildContext) -> ValidationReport:
        snowflake_spec(request.spec)
        try:
            validate_source(read_archive(ctx.artifacts.read(request.source, ctx.tenant_id)))
            return ValidationReport(passed=True)
        except ValueError as error:
            return ValidationReport(
                passed=False, diagnostics=[Diagnostic(code="INVALID_SOURCE", message=str(error))]
            )

    def build(self, request: BuildRequest, ctx: BuildContext) -> BuildArtifact:
        spec = snowflake_spec(request.spec)
        report = self.validate(ValidateRequest(spec=spec, source=request.source), ctx)
        if not report.passed:
            raise ValueError("application source validation failed")
        # Snowflake runs the package's code itself; locally it is packaged, never executed.
        return BuildArtifact(
            tenant_id=ctx.tenant_id,
            application_id=request.application_id,
            archive=request.source,
            source_digest=request.source.digest,
            spec_digest=digest(spec),
            spec=spec,
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=ctx.toolchain_digest,
        )

    def _planned(
        self, release: str, application_id: UUID, ctx: ReadContext
    ) -> tuple[dict[str, Any], dict[str, ResourceBinding], list[PlanStep], dict[str, Any]]:
        """(state, owned resources found, steps, plan details) of deploying the release with
        digest `release` on the current native state; reads only."""  # Any: snapshot JSON
        names = names_for(ctx.target)
        state = self._snapshot(self._gateway(ctx), names)
        resources = self._check_ownership(state, names, application_id, ctx)
        application = state["application"]
        patches = _patches(state)
        if application:
            if application.get("upgrade_state") != "COMPLETE":
                raise ValueError("application upgrade is not complete")
            if not patches:
                raise ValueError("installed application has no observed version; resolve drift")
            patch = max(patches) + 1
            if patch >= PATCH_LIMIT:
                raise SnowflakePatchLimit
        else:
            if patches:
                raise ValueError(
                    f"version {VERSION} exists without an installed application; resolve drift"
                )
            patch = 0
        steps = [
            PlanStep(
                key="package-create",
                action="update" if state["package"] else "create",
                resource_key="package",
            ),
            PlanStep(key="schema-create", action="update", resource_key="package"),
            PlanStep(
                key="stage-create",
                action="update" if state["stage"] else "create",
                resource_key="stage",
            ),
            PlanStep(key="stage-upload", action="update", resource_key="stage"),
            PlanStep(key="setup-validate", action="update", resource_key="stage"),
            PlanStep(
                key="version-create",
                action="update" if application else "create",
                resource_key="version",
            ),
            PlanStep(
                key="application-apply",
                action="update" if application else "create",
                resource_key="application",
            ),
            PlanStep(key=TERMINAL_STEP, action="update", resource_key="application"),
        ]
        details = {
            "cli_version": CLI_VERSION,
            **names,
            "version": VERSION,
            "patch": patch,
            "label": f"shifde {release[:12]}",
            "stage": stage_name(names["package"]),
            "stage_location": stage_location(names["package"], release),
            "role": state["role"],
            "existing_package": bool(state["package"]),
            "existing_application": bool(application),
        }
        return state, resources, steps, details

    def plan_details(
        self, release: str, application_id: UUID, ctx: ReadContext
    ) -> dict[str, Any]:  # Any: plan details JSON
        """The details `plan()` seals for the release with digest `release` on the current
        native state (read only), so an executor can rebuild a sealed plan for `apply`."""
        return self._planned(release, application_id, ctx)[3]

    def plan(self, request: PlanRequest, ctx: ReadContext) -> DeploymentPlan:
        artifact = request.artifact
        self._check_artifact(artifact, ctx)
        release = artifact.archive.digest
        state, resources, steps, details = self._planned(release, artifact.application_id, ctx)
        return seal_plan(
            DeploymentPlan(
                tenant_id=ctx.tenant_id,
                application_id=artifact.application_id,
                target_id=ctx.target.id,
                target_revision=ctx.target.revision,
                artifact_id=artifact.id,
                artifact_digest=release,
                spec_digest=artifact.spec_digest,
                bindings_digest=bindings_digest(ctx.target),
                adapter_version=ADAPTER_VERSION,
                toolchain_digest=artifact.toolchain_digest,
                snapshot=EnvironmentSnapshot(
                    resources=list(resources.values()), fingerprint=digest(state)
                ),
                steps=steps,
                details=details,
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    # ------------------------------------------------------------------ apply
    def apply(self, request: ApplyRequest, ctx: DeployContext) -> ApplyReceipt:
        artifact, plan = request.artifact, request.plan
        if plan.tenant_id != ctx.tenant_id or plan.target_id != ctx.target.id:
            raise PermissionError("plan belongs to another tenant or target")
        self._check_artifact(artifact, ctx)
        validate_plan(plan, ctx.target, check_start_ttl=False)
        if ctx.toolchain_digest != plan.toolchain_digest:
            raise ValueError("executing toolchain does not match the deployment plan")
        validate_changes(plan.steps)
        if tuple(step.key for step in plan.steps) != STEPS:
            raise ValueError("deployment plan steps are not the Native App steps")
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
        names = names_for(ctx.target)
        d = plan.details
        if any(d.get(key) != value for key, value in names.items()):
            raise PermissionError("deployment plan details do not match the target bindings")
        files = read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id))
        validate_source(files)
        saved = ctx.journal.completed(TERMINAL_STEP)
        if saved:
            receipt = ApplyReceipt.model_validate(saved)
            self._check_receipt(receipt, ctx)
            if (
                receipt.artifact_digest != artifact.archive.digest
                or receipt.application_id != artifact.application_id
            ):
                raise ValueError("saved receipt does not match the artifact")
            return receipt
        for key in STEPS:
            if ctx.journal.has_intent(key):
                raise NeedsAttention(f"reconcile uncertain step {key} before resubmission")
        # The TTL limits starting new native work, not replaying a completed receipt.
        validate_plan(plan, ctx.target)
        gateway = self._gateway(ctx)
        state = self._snapshot(gateway, names)
        known = self._check_ownership(state, names, artifact.application_id, ctx)
        if digest(state) != plan.snapshot.fingerprint:
            raise ValueError("native Snowflake state changed since planning")
        if state["role"] != d.get("role"):
            raise ValueError("the deploy role changed since planning")
        return _Apply(self, ctx, gateway, artifact, plan, names, files, known).run()

    # ------------------------------------------------------------------ observe and verify
    def observe(self, request: ObserveRequest, ctx: ReadContext) -> NativeObservation:
        receipt = request.receipt
        self._check_receipt(receipt, ctx)
        if request.spec is not None:
            snowflake_spec(request.spec)
        names = names_for(ctx.target)
        gateway = self._gateway(ctx)
        account = str(gateway.execute(SESSION).rows[0].get("account"))
        app = _exact(
            gateway.execute("SHOW APPLICATIONS LIKE " + literal(names["application"])).rows,
            names["application"],
        )
        if not app:
            return NativeObservation(state="failed", diagnostics=["native application is missing"])
        binding = self._binding(
            "application",
            "application",
            names["application"],
            app.get("created_on"),
            account,
            receipt.application_id,
            ctx.target.id,
        )
        if binding not in receipt.resources:
            return NativeObservation(state="failed", diagnostics=["native identity changed"])
        rows = gateway.execute("DESCRIBE APPLICATION " + quoted(names["application"])).rows
        facts = {str(r.get("property")).lower(): r.get("value") for r in rows}
        if facts.get("source_application_package") != names["package"]:
            return NativeObservation(
                state="failed", diagnostics=["installed application belongs to another package"]
            )
        upgrade = facts.get("upgrade_state")
        if upgrade in FAILED_STATES or facts.get("disablement_reasons") not in (None, "", "[]"):
            return NativeObservation(
                state="failed",
                resources=receipt.resources,
                diagnostics=["application upgrade failed or the application is disabled"],
            )
        state: Literal["running", "succeeded"] = (
            "succeeded"
            if facts.get("version") == VERSION
            and str(facts.get("patch")) == receipt.operation_handles.get("patch")
            and upgrade == "COMPLETE"
            else "running"
        )
        return NativeObservation(state=state, resources=receipt.resources)

    def verify(self, request: VerifyRequest, ctx: ReadContext) -> VerificationReport:
        receipt = request.receipt
        observed = self.observe(ObserveRequest(receipt=receipt), ctx)
        if observed.state != "succeeded":
            detail = "; ".join(observed.diagnostics) or f"application is {observed.state}"
            return VerificationReport(
                checks=[
                    Check(
                        name="native-version",
                        status="failed",
                        evidence=f"native-version: {detail}"[:EVIDENCE_MAX],
                    ),
                    Check(name="runtime-health", status="skipped", evidence="native-version"),
                    Check(name="app-deep-health", status="skipped", evidence="native-version"),
                ]
            )
        names = names_for(ctx.target)
        gateway = self._gateway(ctx)
        return VerificationReport(
            checks=[
                Check(name="native-version", status="passed"),
                self._runtime_health(gateway, names),
                self._deep_health(gateway, names, request.release_digest),
            ]
        )

    @staticmethod
    def _runtime_health(gateway: SqlGateway, names: dict[str, str]) -> Check:
        sql = f'SELECT {quoted(names["application"])}."APP_PUBLIC"."HEALTH"() AS STATUS'
        try:
            rows = gateway.execute(sql, warehouse=names["warehouse"]).rows
        except Exception:
            return Check(
                name="runtime-health", status="failed", evidence="runtime-health: query failed"
            )
        if len(rows) == 1 and rows[0].get("status") == "OK":
            return Check(name="runtime-health", status="passed")
        return Check(
            name="runtime-health", status="failed", evidence="runtime-health: HEALTH() is not OK"
        )

    @staticmethod
    def _deep_health(gateway: SqlGateway, names: dict[str, str], release: str | None) -> Check:
        """`status` ok, the plan's release digest (never the artifact's) and every check of a
        non-empty list passed; the problems are the evidence. No release digest: failed, no
        call."""
        if release is None:
            return Check(
                name="app-deep-health",
                status="failed",
                evidence="app-deep-health: the request has no release digest",
            )
        sql = f'CALL {quoted(names["application"])}."APP_PUBLIC"."DEEP_HEALTH"()'
        try:
            rows = gateway.execute(sql, warehouse=names["warehouse"]).rows
            raw = next(iter(rows[0].values()))
            report = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(report, dict):
                raise ValueError("deep health is not a JSON object")
        except Exception:
            return Check(
                name="app-deep-health", status="failed", evidence="app-deep-health: call failed"
            )
        problems = []
        if report.get("status") != "ok":
            problems.append(f"status {str(report.get('status'))[:20]}")
        if report.get("release_digest") != release:
            problems.append("release digest differs from the plan's")
        checks = report.get("checks")
        for item in checks if isinstance(checks, list) and checks else [{"name": "checks"}]:
            if not isinstance(item, dict) or item.get("status") != "passed":
                item = item if isinstance(item, dict) else {}
                reason = item.get("reason") or "failed"
                problems.append(f"{str(item.get('name'))[:200]}: {str(reason)[:200]}")
        if not problems:
            return Check(name="app-deep-health", status="passed")
        evidence = "app-deep-health: " + "; ".join(problems)
        return Check(name="app-deep-health", status="failed", evidence=evidence[:EVIDENCE_MAX])

    def reconcile(self, request: ReconcileRequest, ctx: ReadContext) -> ReconciliationResult:
        if request.plan.tenant_id != ctx.tenant_id or request.plan.target_id != ctx.target.id:
            raise PermissionError("plan context mismatch")
        # Object names and states cannot prove which statement was accepted. Never replay.
        return ReconciliationResult(state="ambiguous", evidence=RECONCILE_EVIDENCE)


class _Apply:
    """One apply run: the eight checkpoints and the ownership re-check after each step."""

    def __init__(
        self,
        adapter: SnowflakeNativeAdapter,
        ctx: DeployContext,
        gateway: SqlGateway,
        artifact: BuildArtifact,
        plan: DeploymentPlan,
        names: dict[str, str],
        files: dict[str, bytes],
        known: dict[str, ResourceBinding],
    ) -> None:
        self.adapter, self.ctx, self.gateway = adapter, ctx, gateway
        self.artifact, self.plan, self.names, self.files = artifact, plan, names, files
        self.known = known
        d = plan.details
        self.patch = int(str(d["patch"]))
        self.upgrade = bool(d["existing_application"])
        self.location = str(d["stage_location"])
        self.release = artifact.archive.digest

    def run(self) -> ApplyReceipt:
        pkg, app = quoted(self.names["package"]), quoted(self.names["application"])
        d = self.plan.details
        self.step(
            "package-create",
            lambda: (
                None
                if d["existing_package"]
                else self.sql(f"CREATE APPLICATION PACKAGE {pkg} ENABLE_RELEASE_CHANNELS = FALSE")
            ),
            new={"package"},
        )
        self.step("schema-create", lambda: self.sql(f'CREATE SCHEMA IF NOT EXISTS {pkg}."SHIFDE"'))
        self.step(
            "stage-create",
            lambda: self.sql(
                f'CREATE STAGE IF NOT EXISTS {pkg}."SHIFDE"."SOURCE" '
                "ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')"
            ),
            new={"stage"},
        )
        self.step("stage-upload", self.upload)
        self.step("setup-validate", self.setup_validate)
        if self.upgrade:
            clause = f"ADD PATCH {self.patch} FOR VERSION {VERSION}"
        else:
            clause = f"ADD VERSION {VERSION}"
        label = literal(str(d["label"]))
        self.step(
            "version-create",
            lambda: self.sql(
                f"ALTER APPLICATION PACKAGE {pkg} {clause} "
                f"USING {literal(self.location)} LABEL = {label}"
            ),
            new=set() if self.upgrade else {"version"},
            check=self.check_release,
        )
        if self.upgrade:
            install = f"ALTER APPLICATION {app} UPGRADE USING VERSION {VERSION} PATCH {self.patch}"
        else:
            install = (
                f"CREATE APPLICATION {app} FROM APPLICATION PACKAGE {pkg} "
                f"USING VERSION {VERSION} PATCH {self.patch}"
            )
        self.step(
            "application-apply",
            lambda: self.sql(install),
            new=set() if self.upgrade else {"application"},
        )
        role = quoted(str(d["role"]))
        self.ctx.journal.intent(TERMINAL_STEP, {"plan": self.plan.digest})
        self.ctx.check_deadline()
        self.recheck(set())
        self.sql(f'GRANT APPLICATION ROLE {app}."APP_USER" TO ROLE {role}')
        missing = [key for key in RESOURCE_KEYS if key not in self.known]
        if missing:
            raise NeedsAttention("a native object of the deployment could not be observed")
        handles = {
            "package": self.names["package"],
            "application": self.names["application"],
            "warehouse": self.names["warehouse"],
            "stage": str(d["stage"]),
            "version": VERSION,
            "patch": str(self.patch),
            "digest": self.release,
        }
        if self.gateway.platform == "fake":
            handles["platform"] = "fake"
        receipt = ApplyReceipt(
            tenant_id=self.ctx.tenant_id,
            application_id=self.artifact.application_id,
            target_id=self.ctx.target.id,
            artifact_digest=self.release,
            resources=[self.known[key] for key in RESOURCE_KEYS],
            operation_handles=handles,
        )
        self.ctx.journal.complete(TERMINAL_STEP, receipt.model_dump(mode="json"))
        return receipt

    def sql(self, statement: str) -> str:
        return self.gateway.execute(statement).handle

    def step(
        self,
        key: str,
        write: Callable[[], str | None],
        *,
        new: set[str] | None = None,
        check: Callable[[dict[str, Any]], None] | None = None,  # Any: snapshot JSON
    ) -> None:
        self.ctx.journal.intent(key, {"plan": self.plan.digest})
        self.ctx.check_deadline()
        handle = write()
        state = self.recheck(new or set())
        if check:
            check(state)
        receipt = NativeStepReceipt(
            step_key=key,
            plan_digest=self.plan.digest,
            resources=list(self.known.values()),
            handles={"statement": handle or ""},
        )
        self.ctx.journal.complete(key, receipt.model_dump(mode="json"))

    def recheck(self, new: set[str]) -> dict[str, Any]:  # Any: snapshot JSON
        """Re-read the objects: a known one must keep its identity, only `new` ones may appear,
        and the application must still come from the package."""
        state = self.adapter._snapshot(self.gateway, self.names)
        found = self.adapter._resources(state, self.names, self.artifact.application_id, self.ctx)
        for key, binding in found.items():
            if key in self.known and binding != self.known[key]:
                raise NeedsAttention("native object identity changed during apply")
            if key not in self.known and key not in new:
                raise NeedsAttention("unexpected native object appeared during apply")
        if any(key not in found for key in self.known):
            raise NeedsAttention("a native object disappeared during apply")
        application = state["application"]
        if application and application.get("source") != self.names["package"]:
            raise NeedsAttention("native application source package changed")
        self.known = found
        return state

    def check_release(self, state: dict[str, Any]) -> None:  # Any: snapshot JSON
        if self.patch not in _patches(state):
            raise NeedsAttention("the new version or patch could not be observed")

    def listed(self, statement: str) -> dict[str, tuple[int, str]]:
        """`path -> (size, md5)` of the release directory's files in a `LIST` answer."""
        files = {}
        for row in self.gateway.execute(statement).rows:
            _, sep, path = str(row.get("name")).partition(f"/{self.release}/")
            if sep:
                files[path] = (int(str(row.get("size"))), str(row.get("md5")))
        return files

    def expected(self, names: list[str] | tuple[str, ...]) -> dict[str, tuple[int, str]]:
        return {name: (len(self.files[name]), _md5(self.files[name])) for name in names}

    def upload(self) -> str | None:
        listing = f"LIST {self.location}"
        if not self.listed(listing):
            with tempfile.TemporaryDirectory(prefix="shifde-snow-source-") as temporary:
                for name, data in self.files.items():
                    path = Path(temporary, *name.split("/"))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                self.ctx.check_deadline()
                copier = self.adapter.copier_factory(self.ctx.target)
                copier.copy(Path(temporary), str(self.plan.details["stage"]), self.release)
        if self.listed(listing) != self.expected(sorted(self.files)):
            raise NeedsAttention("staged source differs from the immutable artifact")
        return None

    def setup_validate(self) -> str | None:
        pattern = literal(f".*/{self.release}/(manifest[.]yml|setup[.]sql|ui/environment[.]yml)")
        staged = self.listed(f"LIST {self.location} PATTERN = {pattern}")
        if staged != self.expected(REQUIRED_FILES):
            raise ValueError("staged setup files differ from the artifact")
        return None
