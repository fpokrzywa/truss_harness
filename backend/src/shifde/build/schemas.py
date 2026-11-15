"""Request/response models of the P4 API (contract sections 1-5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal  # Any: spec / variable / argument JSON
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from shifde.core.bodies import Body

Strict = ConfigDict(extra="forbid")
Kind = Literal["prompts", "agents", "tools", "skills", "orchestrations", "pipelines", "ontologies"]
PublishRequestStatus = Literal["pending", "approved", "rejected"]
ScopeType = Literal["tenant", "workspace", "project"]
Visibility = Literal["private", "scope", "tenant"]
ArtifactStatus = Literal["active", "disabled", "retired"]
KEY_PATTERN = r"^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$"


class ScopeRef(BaseModel):
    model_config = Strict
    type: ScopeType
    id: UUID


class PrincipalRef(BaseModel):
    id: UUID
    display: str


class VersionRefOut(BaseModel):
    id: UUID
    version_no: int


class ArtifactCreate(Body):
    key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    scope: ScopeRef
    visibility: Visibility = "scope"
    spec: dict[str, Any] | None = None


class ArtifactPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    visibility: Visibility | None = None
    status: ArtifactStatus | None = None
    row_version: int | None = None


class ArtifactOut(BaseModel):
    id: UUID
    key: str
    name: str
    description: str | None
    scope: ScopeRef
    visibility: Visibility
    status: ArtifactStatus
    latest_published_version: VersionRefOut | None
    has_draft: bool
    owner: PrincipalRef
    row_version: int
    created_at: datetime
    updated_at: datetime | None


class PinOut(BaseModel):
    role: str
    version_id: UUID
    version_no: int
    state: str
    artifact_id: UUID
    kind: str
    key: str
    name: str


class VersionOut(BaseModel):
    id: UUID
    version_no: int
    state: Literal["draft", "published", "deprecated", "retired"]
    spec: dict[str, Any] | None = None
    spec_schema_version: int
    spec_hash: str | None
    change_note: str | None
    created_by: PrincipalRef | None
    created_at: datetime
    published_by: PrincipalRef | None = None
    published_at: datetime | None = None
    based_on_version_id: UUID | None = None
    row_version: int
    pins: list[PinOut] | None = None
    # pipelines: typed references (connections, versions, models) and the stored report
    refs: list[dict[str, Any]] | None = None
    validation_report: dict[str, Any] | None = None


class DraftPut(Body):
    spec: dict[str, Any]
    row_version: int = Field(ge=0)


class EdgeRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(serialization_alias="from", validation_alias="from")
    to: str


class IssueOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    path: str
    code: str
    message: str
    node_id: str | None = None  # pipelines (P6 amendment E1); ontologies: the object type key
    key: str | None = None  # ontologies (P7 amendment F1): the relationship key
    edge: EdgeRef | None = None


class ValidationOut(BaseModel):
    valid: bool
    errors: list[IssueOut]
    warnings: list[IssueOut]
    topo_order: list[str] | None = None  # pipelines only


class PublishIn(Body):
    change_note: str = Field(default="", max_length=2000)


class StateIn(Body):
    state: Literal["deprecated", "retired"]


class RollbackIn(Body):
    version_id: UUID


class DiffOut(BaseModel):
    changes: list[dict[str, Any]]  # Any: JSON values; `from` / `to` only when present


class RequestArtifact(BaseModel):
    kind: str
    id: UUID
    key: str
    name: str


class PublishRequestOut(BaseModel):
    id: UUID
    artifact: RequestArtifact
    version_id: UUID
    version_no: int
    change_note: str | None
    requested_by: PrincipalRef
    requested_at: datetime
    status: Literal["pending", "approved", "rejected"]
    decided_by: PrincipalRef | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class PublishPendingOut(BaseModel):
    publish_request: PublishRequestOut


class DecideIn(Body):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


# ------------------------------------------------------------------ prompts
class VersionSelector(Body):
    """`version_id`: a published/deprecated/retired version id, or the string "draft"."""

    version_id: str


class RenderIn(VersionSelector):
    variables: dict[str, Any] = Field(default_factory=dict)  # Any: typed variable values


class RenderedMessage(BaseModel):
    role: str
    content: str


class RenderOut(BaseModel):
    messages: list[RenderedMessage]
    warnings: list[str]


class PromptTestIn(RenderIn):
    model_id: UUID | None = None


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int


class CriterionOut(BaseModel):
    name: str
    passed: bool
    detail: str | None


class PromptTestRunOut(BaseModel):
    id: UUID
    output: str
    usage: UsageOut
    latency_ms: int
    evaluation: list[CriterionOut]
    verdict: Literal["pass", "fail"]
    created_at: datetime
    version_id: UUID
    model_id: UUID


# ------------------------------------------------------------------ tools
class ToolTestIn(VersionSelector):
    args: dict[str, Any] = Field(default_factory=dict)  # Any: tool arguments
    acknowledge_side_effects: bool = False


class ToolTestOut(BaseModel):
    output: Any  # Any: tool result JSON
    valid_output: bool
    duration_ms: int
    attempts: int


class FunctionOut(BaseModel):
    key: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect_class: str


class ExecutorOut(BaseModel):
    key: str
    maturity: str
    binding_schema: dict[str, Any]
    functions: list[FunctionOut] | None = None


class ExecutorCatalogOut(BaseModel):
    items: list[ExecutorOut]


# ------------------------------------------------------------------ agents
class AgentTestIn(VersionSelector):
    variables: dict[str, Any] = Field(default_factory=dict)  # Any: typed variable values
    input: str = Field(default="", max_length=100_000)


class AssembledOut(BaseModel):
    messages: list[RenderedMessage]
    tools: list[dict[str, Any]]  # Any: tool declarations


class AgentTestOut(BaseModel):
    assembled: AssembledOut
    output: str
    usage: UsageOut
    latency_ms: int
    model_id: UUID


class AgentStatusIn(Body):
    status: Literal["active", "disabled"]


class ResolvedOut(BaseModel):
    agent_version_id: UUID
    version_no: int
    spec_hash: str | None
    spec: dict[str, Any]
    prompt_version: dict[str, Any] | None
    tools: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    models: list[dict[str, Any]]


# ------------------------------------------------------------------ pipelines (P6)
class PortOut(BaseModel):
    name: str
    schema_: dict[str, Any] = Field(serialization_alias="schema", validation_alias="schema")
    required: bool = True

    model_config = ConfigDict(populate_by_name=True)


class NodeTypeOut(BaseModel):
    type: str
    label: str
    category: Literal["source", "logic", "ai", "action", "control"]
    description: str = ""
    config_schema: dict[str, Any]
    inputs: list[PortOut]
    outputs: list[PortOut]
    dynamic_inputs: bool = False
    side_effect: bool
    enabled: bool
    maturity: Literal["validated", "experimental", "planned"]
    reason_disabled: str | None = None


class NodeTypeCatalogOut(BaseModel):
    items: list[NodeTypeOut]


# ------------------------------------------------------------------ ontologies (P7)
MappingTargetKind = Literal["object_type", "relationship"]


class RelationIn(Body):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: str = Field(default="", max_length=128, alias="schema")  # "" = default schema
    name: str = Field(min_length=1, max_length=128)


class RelationOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_: str = Field(serialization_alias="schema", validation_alias="schema")
    name: str


class KeyMapIn(Body):
    relation: RelationIn | None = None  # link-table form repeats the mapping's relation
    from_column: str = Field(min_length=1, max_length=128)
    to_column: str = Field(min_length=1, max_length=128)


class MappingIn(Body):
    target_kind: MappingTargetKind
    target_key: str = Field(min_length=1, max_length=200)
    connection_id: UUID
    relation: RelationIn
    column_map: dict[str, str] = Field(default_factory=dict, max_length=500)
    key_map: KeyMapIn | None = None
    filters: list[str] = Field(default_factory=list, max_length=20)
    status: str | None = None  # server-computed: ignored when sent
    row_version: int | None = None  # ignored (the mapping is replaced as a whole)


class MappingOut(BaseModel):
    target_kind: MappingTargetKind
    target_key: str
    connection_id: UUID
    relation: RelationOut
    column_map: dict[str, str]
    key_map: dict[str, Any] | None = None
    filters: list[str]
    status: Literal["unchecked", "ok", "invalid"]
    row_version: int


class MappingIssueOut(BaseModel):
    code: str
    message: str
    path: str | None = None
    severity: Literal["error", "warning"] = "error"


class SampleColumnOut(BaseModel):
    name: str
    type: str | None = None
    nullable: bool | None = None


class MappingCheckOut(BaseModel):
    ok: bool
    issues: list[MappingIssueOut]
    sample_columns: list[SampleColumnOut]
