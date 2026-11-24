"""Deployment target and executor DTOs (design sections 3, 5 and 9 items 2, 3, 12 and 17;
DECISIONS B-2501, B-2664).

`TargetOut` never carries the claim id or the address key: the claim is the organization's
private bookkeeping of the one-platform rule. `PlanOut` never carries the artifact's content.
`DeploymentDetail` (with the step journal) is returned only by the deployment item GET.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationInfo, field_validator

from shifde.adapters.native.contracts import EnvironmentSnapshot, PlanStep

Strict = ConfigDict(extra="forbid")
Environment = Literal["dev", "test", "prod"]
CREDENTIAL_REF = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
PlatformKind = Literal["databricks", "snowflake"]
SNOWFLAKE_IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$"  # `snowflake.source.identifier`


class TargetBindings(BaseModel):
    """A Databricks target's bindings. `serving_endpoint`: the model serving endpoint a generated
    app (a `/2` release) queries, by name; a template plan ignores it (SP5 6.6)."""

    model_config = Strict
    warehouse_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    serving_endpoint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,63}$")


class SnowflakeBindings(BaseModel):
    """A Snowflake target's bindings: the Native App package, application and warehouse names
    and the Cortex model (SP6 design 6.4)."""

    model_config = Strict
    package: str = Field(pattern=SNOWFLAKE_IDENTIFIER)
    application: str = Field(pattern=SNOWFLAKE_IDENTIFIER)
    warehouse: str = Field(pattern=SNOWFLAKE_IDENTIFIER)
    cortex_model: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


BINDINGS: dict[str, type[TargetBindings | SnowflakeBindings]] = {
    "databricks": TargetBindings,
    "snowflake": SnowflakeBindings,
}


class TargetCreate(BaseModel):
    model_config = Strict
    workspace: str
    project: str | None = None
    name: str = Field(min_length=1, max_length=200)
    platform_kind: PlatformKind = "databricks"
    address: str = Field(min_length=1, max_length=2048)
    environment: Environment
    bindings: TargetBindings | SnowflakeBindings
    credential_ref: str = Field(pattern=CREDENTIAL_REF)

    @field_validator("bindings")
    @classmethod
    def _bindings_of_the_platform(
        cls, value: TargetBindings | SnowflakeBindings, info: ValidationInfo
    ) -> TargetBindings | SnowflakeBindings:
        kind = info.data.get("platform_kind")
        if kind is not None and not isinstance(value, BINDINGS[kind]):
            raise ValueError(f"bindings are not {kind} target bindings")
        return value


class TargetOut(BaseModel):
    id: UUID
    name: str
    workspace: str | None
    project: str | None
    platform_kind: PlatformKind
    address: str
    environment: str
    bindings: TargetBindings | SnowflakeBindings
    credential_ref: str
    status: str
    revision: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime | None


class ExecutorCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)


class ExecutorPatch(BaseModel):
    model_config = Strict
    status: Literal["active", "disabled"]


class DeliveryExecutorOut(BaseModel):
    id: UUID
    name: str
    status: str
    last_seen_at: datetime | None
    version: str | None
    created_at: datetime
    updated_at: datetime | None


class CredentialOut(BaseModel):
    """Never the secret or its hash: the secret exists only in `CredentialIssued`."""

    id: UUID
    executor_id: UUID
    prefix: str
    status: Literal["live", "revoked", "expired"]
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class CredentialIssued(BaseModel):
    """Issue and rotate: the full `shx_` token, shown once."""

    credential: CredentialOut
    secret: str


class ExecutorWhoami(BaseModel):
    executor_id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: str | None
    last_seen_at: datetime | None


# ====================================================================== plans (B-2675..B-2679)
DIGEST = r"^[a-f0-9]{64}$"


class PlanCreate(BaseModel):
    """Settled call 17: the target, the application's slug and its display name; SP3 settled
    call 3: the sealed release it deploys."""

    model_config = Strict
    target_id: UUID
    release_id: UUID
    application_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    display_name: str = Field(min_length=1, max_length=200)


class PlanApprove(BaseModel):
    """The approver quotes the sealed digest they reviewed."""

    model_config = Strict
    plan_digest: str = Field(pattern=DIGEST)
    note: str | None = Field(default=None, max_length=2000)


class PlanReject(BaseModel):
    model_config = Strict
    note: str | None = Field(default=None, max_length=2000)


class PlanApprovalOut(BaseModel):
    decision: Literal["approve", "reject"]
    decided_by: UUID
    decided_at: datetime
    note: str | None


class PlanOut(BaseModel):
    """A plan; `release_id`, `release_digest` and `evaluation_run_id` are null unless the caller
    can read the plan's release. `digest` is returned even then: it is a commitment (a sha256 over
    the plan's inputs and its release digest) that reveals nothing on its own; a caller who already
    holds a candidate release digest can confirm it only by recomputing the digest from the fields
    they can read. It is kept because approve quotes it."""

    id: UUID
    target_id: UUID
    application_key: str
    display_name: str
    status: Literal[
        "requested", "planning", "sealed", "approved", "rejected", "expired", "plan_failed"
    ]
    status_reason: str | None
    requested_by: UUID
    artifact_id: UUID
    artifact_digest: str
    target_revision: int
    steps: list[PlanStep] | None
    snapshot: EnvironmentSnapshot | None
    adapter_version: str | None
    toolchain_digest: str | None
    digest: str | None
    sealed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    approval: PlanApprovalOut | None
    deployment_id: UUID | None
    release_id: UUID | None
    release_digest: str | None
    evaluation_run_id: UUID | None


# ====================================================================== deployments (B-2681..)
DeploymentStatus = Literal[
    "queued",
    "leased",
    "applying",
    "observing",
    "verifying",
    "verified",
    "expired",
    "cancelled",
    "failed",
    "unknown_outcome",
    "resolved_failed",
]


class DeploymentOut(BaseModel):
    id: UUID
    plan_id: UUID
    target_id: UUID
    application_key: str
    status: DeploymentStatus
    status_reason: str | None
    receipt: dict[str, JsonValue] | None
    verification: dict[str, JsonValue] | None
    reconcile_evidence: dict[str, JsonValue] | None
    observe_deadline: datetime | None
    resolved_by: UUID | None
    resolved_note: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


class DeploymentJournalStep(BaseModel):
    step_key: str
    state: Literal["intent", "complete"]
    input_digest: str
    receipt: dict[str, JsonValue] | None
    recorded_at: datetime


class DeploymentDetail(DeploymentOut):
    """The item GET only: the deployment with its journal, ordered by `recorded_at`."""

    journal: list[DeploymentJournalStep]


class DeploymentMarkFailed(BaseModel):
    """Mark an unknown outcome failed: a note (1-2000 characters after stripping) is required."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(min_length=1, max_length=2000)
