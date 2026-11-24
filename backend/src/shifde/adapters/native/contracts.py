"""Native delivery wire models (Databricks Apps, Snowflake Native Apps and Palantir Marketplace
installations) and canonical hashing (FDE SDK `shifde_contracts`).

The values an executor reports back in a job result carry length and count limits (`NAME_MAX`,
`NATIVE_ID_MAX`, `EVIDENCE_MAX`, `HANDLES_MAX`, `RESOURCES_MAX`, `ITEMS_MAX`; B-2736): a value
over a limit is refused with 422 before anything is stored."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self  # Any: arbitrary JSON values to hash
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def relative_path(value: str) -> str:
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("expected a relative POSIX source path")
    if value != "." and any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("source path cannot contain traversal or empty segments")
    if PurePosixPath(value).is_absolute():
        raise ValueError("source path must be relative")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
    }
    for part in value.split("/"):
        if part == "." and value == ".":
            continue
        if part.rstrip(" .") != part or part.split(".", 1)[0].upper() in reserved:
            raise ValueError("source path is ambiguous on Windows")
        if any(ord(char) < 32 or char in '<>"|?*' for char in part):
            raise ValueError("source path contains unsupported characters")
    return value


RelativePath = Annotated[str, AfterValidator(relative_path)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Timestamp = AwareDatetime

# Limits on executor-reported free text and collections (B-2736). The executor's names, native ids,
# handles, evidence and diagnostics are untrusted executor input: screens must render them as
# plain text only, never as HTML or markdown (issue #27).
NAME_MAX = 128  # resource and step keys, resource types, check names, handle keys
NATIVE_ID_MAX = 512  # platform ids and operation handle values
EVIDENCE_MAX = 2000  # check and reconcile evidence, each observe diagnostic
HANDLES_MAX = 32  # operation handles of one receipt
RESOURCES_MAX = 200  # resources of one receipt, observation, reconciliation or snapshot
ITEMS_MAX = 50  # checks of a verification, diagnostics of an observation, steps of a plan

Name = Annotated[str, Field(max_length=NAME_MAX)]
NativeId = Annotated[str, Field(max_length=NATIVE_ID_MAX)]
Evidence = Annotated[str, Field(max_length=EVIDENCE_MAX)]


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )


class Entity(Model):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID


# A Docker daemon id: a lowercase UUID or the legacy twelve colon-joined base32 groups.
DAEMON_ID = (
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[A-Z2-7]{4}(?::[A-Z2-7]{4}){11})"
)
DOCKER_HOST = re.compile(rf"docker://({DAEMON_ID})")


# --------------------------------------------------------------------------- common
class Target(Entity):
    revision: int = Field(default=1, ge=1)
    environment: Literal["dev", "test", "prod"] = "dev"
    host: str
    bindings: dict[str, str] = Field(default_factory=dict)

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if DOCKER_HOST.fullmatch(value):
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError("host must be an HTTPS origin without credentials or a path")
        return value.rstrip("/")


class SourceArchive(Entity):
    digest: Digest
    format: str = "zip"
    size: int = Field(ge=0)


class ResourceBinding(Model):
    key: Name
    native_id: NativeId
    resource_type: Name
    application_id: UUID
    target_id: UUID


class Diagnostic(Model):
    code: str
    message: str
    path: str | None = None


class ValidationReport(Model):
    passed: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class Check(Model):
    name: Name
    status: str = Field(pattern="^(passed|failed|skipped)$")
    required: bool = True
    evidence: Evidence = ""


class VerificationReport(Model):
    checks: list[Check] = Field(max_length=ITEMS_MAX)

    @property
    def passed(self) -> bool:
        return (
            bool(self.checks)
            and all(check.status == "passed" for check in self.checks if check.required)
            and any(check.required for check in self.checks)
        )


# --------------------------------------------------------------------------- platform spec
class DatabricksApplicationSpec(Model):
    kind: Literal["databricks.app/v1"] = "databricks.app/v1"
    bundle_target: Identifier = "dev"
    bundle_root: RelativePath = "."
    app_resource_key: str = Field(default="app", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    warehouse_binding: Identifier = "warehouse_id"
    health_path: Literal["/health"] = "/health"


class FabricItem(Model):
    key: Identifier
    item_type: Literal["Lakehouse", "Notebook"]
    definition_path: RelativePath
    depends_on: list[Identifier] = Field(default_factory=list)
    name_binding: Identifier | None = None
    lakehouse: Identifier | None = None


class FabricSolutionSpec(Model):
    """A Fabric solution: an acyclic graph of Lakehouse and Notebook items (SDK
    `platform_specs/fabric.py`, without Ontology and the legacy `bindings`)."""

    kind: Literal["fabric.solution/v1"] = "fabric.solution/v1"
    solution_root: RelativePath = "."
    items: list[FabricItem] = Field(min_length=1, max_length=16)
    workspace_binding: Literal["workspace"] = "workspace"
    release_digest: Digest
    verification_notebook: Identifier

    @model_validator(mode="after")
    def _validate_graph(self) -> Self:
        graph = {item.key: item.depends_on for item in self.items}
        if len(graph) != len(self.items):
            raise ValueError("item keys must be unique")
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(key: str) -> None:
            if key not in graph:
                raise ValueError(f"unknown dependency: {key}")
            if key in visiting:
                raise ValueError("cyclic item dependencies")
            if key in visited:
                return
            visiting.add(key)
            for dependency in graph[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)
        by_key = {item.key: item for item in self.items}
        paths = [item.definition_path for item in self.items]
        if (
            "." in paths
            or len(set(paths)) != len(paths)
            or any(a.startswith(b + "/") for a in paths for b in paths if a != b)
        ):
            raise ValueError("Fabric item definition paths must not overlap")
        if len({item.name_binding or item.key for item in self.items}) != len(self.items):
            raise ValueError("item name bindings must be unique")
        for item in self.items:
            if item.lakehouse and (
                item.item_type != "Notebook"
                or item.lakehouse not in item.depends_on
                or by_key[item.lakehouse].item_type != "Lakehouse"
            ):
                raise ValueError("notebook lakehouse must be a declared Lakehouse dependency")
        notebook = by_key.get(self.verification_notebook)
        if notebook is None or notebook.item_type != "Notebook":
            raise ValueError("verification_notebook must identify a declared Notebook")
        return self


class SnowflakeApplicationSpec(Model):
    """A Snowflake Native App. No names: the package, application and warehouse come from the
    target bindings (SP6 design 6.2)."""

    kind: Literal["snowflake.native-app/v1"] = "snowflake.native-app/v1"


class FoundryMarketplaceSpec(Model):
    """A Palantir Marketplace installation. The product version, object type, asset digest and RIDs
    come from the target bindings (SP8 design call S8-H)."""

    kind: Literal["palantir.marketplace/v1"] = "palantir.marketplace/v1"
    frontend_asset_path: RelativePath

    @field_validator("frontend_asset_path")
    @classmethod
    def _names_a_file(cls, value: str) -> str:
        if value == ".":
            raise ValueError("frontend asset path must identify a file")
        return value


class DockerComposeSpec(Model):
    kind: Literal["docker.compose/v1"] = "docker.compose/v1"
    health_path: Literal["/health"] = "/health"


ApplicationSpec = Annotated[
    DatabricksApplicationSpec
    | SnowflakeApplicationSpec
    | FabricSolutionSpec
    | FoundryMarketplaceSpec
    | DockerComposeSpec,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- builds
class BuildArtifact(Entity):
    application_id: UUID
    archive: SourceArchive
    source_digest: Digest
    spec_digest: Digest
    spec: ApplicationSpec
    adapter_version: str
    toolchain_digest: Digest
    template_version: str = "0.1.0"


class ScaffoldRequest(Model):
    spec: ApplicationSpec
    template_version: str = "0.1.0"


class ValidateRequest(Model):
    spec: ApplicationSpec
    source: SourceArchive


class BuildRequest(ValidateRequest):
    application_id: UUID


# --------------------------------------------------------------------------- plans
class EnvironmentSnapshot(Model):
    resources: list[ResourceBinding] = Field(default_factory=list, max_length=RESOURCES_MAX)
    fingerprint: Digest
    captured_at: Timestamp = Field(default_factory=utcnow)


class PlanStep(Model):
    key: Name
    action: Literal["create", "update", "start", "delete", "unknown"]
    resource_key: Name
    reversible: bool = False


class DeploymentPlan(Entity):
    application_id: UUID
    target_id: UUID
    target_revision: int
    artifact_id: UUID
    artifact_digest: Digest
    spec_digest: Digest
    bindings_digest: Digest
    adapter_version: str
    toolchain_digest: Digest
    snapshot: EnvironmentSnapshot
    steps: list[PlanStep]
    details: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: Timestamp = Field(default_factory=utcnow)
    expires_at: Timestamp
    digest: Digest


class PlanRequest(Model):
    artifact: BuildArtifact


class InspectRequest(Model):
    application_id: UUID
    app_name: str
    spec: ApplicationSpec | None = None


# --------------------------------------------------------------------------- execution
class ApplyRequest(Model):
    artifact: BuildArtifact
    plan: DeploymentPlan


class ApplyReceipt(Entity):
    application_id: UUID
    target_id: UUID
    artifact_digest: Digest
    resources: list[ResourceBinding] = Field(max_length=RESOURCES_MAX)
    operation_handles: dict[Name, NativeId] = Field(default_factory=dict, max_length=HANDLES_MAX)


class ObserveRequest(Model):
    receipt: ApplyReceipt
    spec: ApplicationSpec | None = None


class ReconcileRequest(Model):
    plan: DeploymentPlan


class VerifyRequest(Model):
    """`release_digest`: the plan's release digest when the deployment is a generated app (SP5
    6.6); the app's deep health must answer it."""

    receipt: ApplyReceipt
    spec: ApplicationSpec | None = None
    release_digest: Digest | None = None


class NativeObservation(Model):
    state: Literal["running", "succeeded", "failed", "unknown"]
    resources: list[ResourceBinding] = Field(default_factory=list, max_length=RESOURCES_MAX)
    diagnostics: list[Evidence] = Field(default_factory=list, max_length=ITEMS_MAX)


class ReconciliationResult(Model):
    state: Literal["applied", "not_applied", "in_progress", "ambiguous"]
    evidence: Evidence
    resources: list[ResourceBinding] = Field(default_factory=list, max_length=RESOURCES_MAX)


# --------------------------------------------------------------------------- capabilities
class Capability(Model):
    operation: str
    status: Literal["supported", "unsupported", "unknown"]
    reason: str


class CapabilityReport(Model):
    adapter_version: str
    maturity: Literal["experimental", "validated"] = "experimental"
    application_kinds: list[str] = Field(default_factory=list)
    capabilities: list[Capability]
    captured_at: Timestamp = Field(default_factory=utcnow)
    expires_at: Timestamp

    def supports(self, operation: str) -> bool:
        return self.expires_at > utcnow() and any(
            item.operation == operation and item.status == "supported" for item in self.capabilities
        )


# --------------------------------------------------------------------------- journal checkpoints
class NativeStepReceipt(Model):
    step_key: Identifier
    plan_digest: Digest
    resources: list[ResourceBinding] = Field(default_factory=list)
    handles: dict[str, str] = Field(default_factory=dict)


class JournalEntry(Model):
    """One journal step: the intent is recorded before the native write, the receipt after it."""

    step_key: Identifier
    input_digest: Digest
    state: Literal["intent", "complete"]
    receipt: dict[str, JsonValue] | None = None


# --------------------------------------------------------------------------- executor gateway
JobKind = Literal["plan", "apply", "observe", "verify", "reconcile"]


class LeaseProof(Model):
    """The current lease of a job; every per-job gateway call proves it."""

    lease_id: UUID
    fence: int = Field(ge=1)


class LeaseRequest(Model):
    request_id: UUID
    kinds: list[JobKind] = Field(min_length=1, max_length=5)

    @field_validator("kinds")
    @classmethod
    def _unique_kinds(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("kinds must be unique")
        return value


class LeasedJob(Model):
    id: UUID
    kind: JobKind
    plan_id: UUID | None
    deployment_id: UUID | None
    target_id: UUID | None
    deadline: Timestamp | None
    attempts: int
    max_attempts: int


class JobLease(Model):
    job: LeasedJob
    lease_id: UUID
    fence: int
    lease_until: Timestamp
    request_id: UUID


class HeartbeatAck(Model):
    lease_until: Timestamp


class CredentialGrant(Model):
    """What the executor may fetch a credential for; it never holds a secret."""

    job_id: UUID
    executor_id: UUID
    target_id: UUID
    credential_ref: str
    audience: str
    expires_at: Timestamp


class CheckpointSubmission(LeaseProof):
    sequence: int = Field(ge=1)
    step_key: Identifier
    state: Literal["intent", "complete"]
    input_digest: Digest
    receipt: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _receipt_matches_state(self) -> Self:
        if (self.state == "complete") != (self.receipt is not None):
            raise ValueError("a receipt is required for complete and refused for intent")
        return self


class JobResultSubmission(LeaseProof):
    status: Literal["succeeded", "failed"]
    value: dict[str, JsonValue] | None = None
    error_code: Literal["execution_failed", "credential_unavailable"] | None = None

    @model_validator(mode="after")
    def _value_or_error(self) -> Self:
        if self.status == "succeeded" and (self.value is None or self.error_code is not None):
            raise ValueError("succeeded requires a value and no error_code")
        if self.status == "failed" and (self.error_code is None or self.value is not None):
            raise ValueError("failed requires an error_code and no value")
        return self


class JobResultAck(Model):
    job_id: UUID
    status: Literal["done"]


class PlanJobResult(Model):
    """The value of a succeeded `plan` job: what the executor found and would change."""

    steps: list[PlanStep] = Field(max_length=ITEMS_MAX)
    snapshot: EnvironmentSnapshot
    adapter_version: str = Field(min_length=1, max_length=64)
    toolchain_digest: Digest


class ContextPlan(Model):
    """The job's plan as the executor needs it; the sealed fields are `None` before sealing.
    `release_digest` is the plan's release digest and `generator_version` its release's app
    generator version (`None` unless the release is a generated app, SP5 6.6)."""

    id: UUID
    tenant_id: UUID
    application_key: str
    display_name: str
    artifact_id: UUID
    artifact_digest: Digest
    target_revision: int
    status: str
    steps: list[PlanStep] | None
    snapshot: EnvironmentSnapshot | None
    adapter_version: str | None
    toolchain_digest: Digest | None
    digest: Digest | None
    expires_at: Timestamp | None
    release_digest: Digest | None = None
    generator_version: str | None = None


class ContextDeployment(Model):
    id: UUID
    tenant_id: UUID
    status: str
    receipt: ApplyReceipt | None


class OwnedResource(Model):
    """A native resource the job's target and application key already own."""

    resource_type: str
    native_id: str


class JobContext(Model):
    """What a leased job runs against (never a credential, the claim or the artifact bytes).
    `platform_kind` is the target's platform; it is not on `Target`, whose digest every sealed
    plan's `bindings_digest` is."""

    job: LeasedJob
    target: Target
    platform_kind: Literal["databricks", "snowflake"] = "databricks"
    plan: ContextPlan | None
    deployment: ContextDeployment | None
    owned_resources: list[OwnedResource]
