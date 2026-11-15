"""Request / response models of the P5 API (docs/api/PHASE_5_CONTRACT.md sections 2-3, D1.x)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self  # Any: run input variables / JSON documents
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from shifde.core.bodies import Body

RunKind = Literal["agent", "orchestration", "pipeline"]
OutRunKind = Literal["agent", "orchestration", "pipeline", "ontology_action"]
ApprovalDecisionName = Literal["approve", "reject", "mark_succeeded", "retry", "fail"]


class RunCreate(Body):
    kind: RunKind
    agent_id: UUID | None = None
    orchestration_id: UUID | None = None
    pipeline_id: UUID | None = None
    # a published version id; "draft" (pipelines only) runs the saved draft as a test run
    version_id: UUID | Literal["draft"] | None = None
    workspace: str = Field(min_length=1, max_length=100)
    project: str = Field(min_length=1, max_length=100)
    input: str = Field(default="", max_length=200_000)
    variables: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)  # pipelines: validated against spec.params
    is_test: bool = False

    @model_validator(mode="after")
    def _target(self) -> Self:
        ids = {
            "agent": self.agent_id,
            "orchestration": self.orchestration_id,
            "pipeline": self.pipeline_id,
        }
        others = [k for k, v in ids.items() if k != self.kind and v is not None]
        if ids[self.kind] is None or others:
            raise ValueError(f"kind={self.kind} needs {self.kind}_id (and no other target id)")
        if self.kind != "pipeline":
            if "input" not in self.model_fields_set:
                raise ValueError("input is required")
            if self.version_id == "draft" or self.params or self.is_test:
                raise ValueError("version_id=draft, params and is_test are for pipelines only")
        elif self.input or self.variables:
            raise ValueError("a pipeline run takes params, not input / variables")
        return self


class ActionInvokeIn(Body):
    """`POST .../ontologies/{id}/actions/{key}/invoke` (contract section 4). `workspace` /
    `project` (slugs) are needed only when the ontology is not project-scoped."""

    object_pk: str = Field(min_length=1, max_length=500)
    extra_params: dict[str, Any] = Field(default_factory=dict, max_length=50)
    version_id: UUID | None = None
    workspace: str | None = Field(default=None, min_length=1, max_length=100)
    project: str | None = Field(default=None, min_length=1, max_length=100)


class DecideIn(Body):
    decision: ApprovalDecisionName
    comment: str | None = Field(default=None, max_length=2000)


class PrincipalOut(BaseModel):
    type: str
    id: UUID
    display: str | None = None


class ScopeOut(BaseModel):
    id: UUID
    slug: str


class UsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0


class RunOut(BaseModel):
    id: UUID
    kind: str
    status: str
    tenant_id: UUID
    workspace: ScopeOut
    project: ScopeOut
    initiated_by: PrincipalOut
    agent_id: UUID | None = None
    agent_version_id: UUID | None = None
    prompt_version_id: UUID | None = None
    orchestration_id: UUID | None = None
    orchestration_version_id: UUID | None = None
    pipeline_id: UUID | None = None
    pipeline_version_id: UUID | None = None
    ontology_id: UUID | None = None
    ontology_version_id: UUID | None = None
    action_key: str | None = None
    parent_run_id: UUID | None = None
    root_run_id: UUID
    node_id: str | None = None
    is_test: bool = False
    trace_id: str
    correlation_id: str
    idempotency_key: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    deadline_at: datetime
    error_class: str | None = None
    error_message: str | None = None
    attempt_count: int
    usage: UsageOut
    waiting_reason: str | None = None
    cancel_requested_at: datetime | None = None
    # True while a write's outcome is unknown and a decision about it is still open
    # (`GET /approvals?status=pending`, reason `indeterminate_side_effect`)
    outcome_unknown: bool = False


class StepOut(BaseModel):
    id: UUID
    run_id: UUID
    seq: int
    kind: str
    name: str
    node_id: str | None = None
    status: str
    attempt: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_id: UUID | None = None
    tool_version_id: UUID | None = None
    tool_invocation_id: UUID | None = None
    child_run_id: UUID | None = None
    idempotency_key: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    input_ref: UUID | None = None
    output_ref: UUID | None = None


class EventOut(BaseModel):
    seq: int
    at: datetime
    type: str
    from_: str | None = Field(default=None, serialization_alias="from")
    to: str | None = None
    step_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ResultOut(BaseModel):
    status: str
    output: Any = None  # Any: the run's final output document
    redacted: bool = False


class ToolRefOut(BaseModel):
    key: str | None
    version: int | None
    side_effect_class: str | None


class DecisionOut(BaseModel):
    decision: str | None
    comment: str | None
    decided_by: PrincipalOut | None
    decided_at: datetime | None


class ApprovalOut(BaseModel):
    id: UUID
    run_id: UUID
    step_id: UUID | None
    reason: str
    tool: ToolRefOut | None
    arguments: Any  # Any: the exact validated arguments, secrets redacted
    requested_by: PrincipalOut
    run_initiated_by: PrincipalOut
    requested_at: datetime
    expires_at: datetime
    status: str
    decision: DecisionOut | None = None


class NodeOut(BaseModel):
    """One node of a pipeline run (P6 section 4, amendment E1)."""

    node_id: str
    type: str
    status: Literal[
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "cancelled",
        "waiting_for_approval",
    ]
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_preview: Any = None  # Any: redacted JSON, at most 4 KiB
    output_ref: str | None = None
    error_class: str | None = None
    # True while a write of this node (or of a tool call it made, or of its child run) may have
    # been delivered and the decision about it is open
    outcome_unknown: bool = False


class NodeListOut(BaseModel):
    items: list[NodeOut]


class ArtifactOut(BaseModel):
    id: UUID
    run_id: UUID
    kind: str
    size_bytes: int
    created_at: datetime
    truncated: bool = False
    redacted: bool = False
    content: Any = None  # Any: stored JSON document, secrets masked


class ScheduleCreate(Body):
    version_policy: Literal["pinned", "latest_published"] = "latest_published"
    pipeline_version_id: UUID | None = None
    cron: str = Field(min_length=9, max_length=120)
    timezone: str = Field(default="UTC", max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    run_as_service_account_id: UUID
    status: Literal["active", "paused"] = "paused"


class ScheduleOut(BaseModel):
    id: UUID
    pipeline_id: UUID
    version_policy: str
    pipeline_version_id: UUID | None
    cron: str
    timezone: str
    params: dict[str, Any]
    run_as_service_account_id: UUID
    status: str
    next_run_at: datetime | None
    created_at: datetime
