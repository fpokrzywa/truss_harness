"""Databricks native delivery wire models and canonical hashing (FDE SDK `shifde_contracts`)."""

import hashlib
import json
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


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )


class Entity(Model):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID


# --------------------------------------------------------------------------- common
class Target(Entity):
    revision: int = Field(default=1, ge=1)
    environment: Literal["dev", "test", "prod"] = "dev"
    host: str
    bindings: dict[str, str] = Field(default_factory=dict)

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
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
    key: str
    native_id: str
    resource_type: str
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
    name: str
    status: str = Field(pattern="^(passed|failed|skipped)$")
    required: bool = True
    evidence: str = ""


class VerificationReport(Model):
    checks: list[Check]

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


# --------------------------------------------------------------------------- builds
class BuildArtifact(Entity):
    application_id: UUID
    archive: SourceArchive
    source_digest: Digest
    spec_digest: Digest
    spec: DatabricksApplicationSpec
    adapter_version: str
    toolchain_digest: Digest
    template_version: str = "0.1.0"


class ScaffoldRequest(Model):
    spec: DatabricksApplicationSpec
    template_version: str = "0.1.0"


class ValidateRequest(Model):
    spec: DatabricksApplicationSpec
    source: SourceArchive


class BuildRequest(ValidateRequest):
    application_id: UUID


# --------------------------------------------------------------------------- plans
class EnvironmentSnapshot(Model):
    resources: list[ResourceBinding] = Field(default_factory=list)
    fingerprint: Digest
    captured_at: Timestamp = Field(default_factory=utcnow)


class PlanStep(Model):
    key: str
    action: Literal["create", "update", "start", "delete", "unknown"]
    resource_key: str
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
    spec: DatabricksApplicationSpec | None = None


# --------------------------------------------------------------------------- execution
class ApplyRequest(Model):
    artifact: BuildArtifact
    plan: DeploymentPlan


class ApplyReceipt(Entity):
    application_id: UUID
    target_id: UUID
    artifact_digest: Digest
    resources: list[ResourceBinding]
    operation_handles: dict[str, str] = Field(default_factory=dict)


class ObserveRequest(Model):
    receipt: ApplyReceipt
    spec: DatabricksApplicationSpec | None = None


class ReconcileRequest(Model):
    plan: DeploymentPlan


class VerifyRequest(Model):
    receipt: ApplyReceipt
    spec: DatabricksApplicationSpec | None = None


class NativeObservation(Model):
    state: Literal["running", "succeeded", "failed", "unknown"]
    resources: list[ResourceBinding] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class ReconciliationResult(Model):
    state: Literal["applied", "not_applied", "in_progress", "ambiguous"]
    evidence: str
    resources: list[ResourceBinding] = Field(default_factory=list)


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

    steps: list[PlanStep]
    snapshot: EnvironmentSnapshot
    adapter_version: str = Field(min_length=1, max_length=64)
    toolchain_digest: Digest
