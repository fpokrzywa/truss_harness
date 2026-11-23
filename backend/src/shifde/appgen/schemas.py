"""App request and draft DTOs (SP5 design sections 4, 5.3 and 7).

`Draft` is the closed draft schema: every object refuses unknown fields, names follow fixed slug
patterns, and the evaluation cases use `evaluation.schemas.Assertion`. Its JSON schema
(`draft.DRAFT_SCHEMA`) is what the drafting model is asked to answer, and the same model validates
a person's edit. A draft never holds a float (`packaging.canonical` refuses them).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self  # Any: JSON documents
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shifde.evaluation.schemas import Assertion
from shifde.packaging.schemas import PARAMETER_NAME, TOOL_NAME, Screen

Strict = ConfigDict(extra="forbid")
AGENT_NAME = r"^[a-z][a-z0-9-]{1,62}$"
FLOATS_REFUSED = "floats are not allowed"


class RequestStatus(StrEnum):
    QUEUED = "queued"
    DRAFTING = "drafting"
    DRAFTED = "drafted"
    FAILED = "failed"
    ACCEPTING = "accepting"
    ACCEPTED = "accepted"
    DISCARDED = "discarded"


def holds_float(value: Any) -> bool:  # Any: a JSON value
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(holds_float(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(holds_float(v) for v in value)
    return False


# ------------------------------------------------------------------ the draft (design 5.3)
class DraftAgent(BaseModel):
    model_config = Strict
    name: str = Field(pattern=AGENT_NAME)
    instructions: str = Field(min_length=1, max_length=8000)


class DraftParameter(BaseModel):
    model_config = Strict
    name: str = Field(pattern=PARAMETER_NAME)
    type: Literal["string", "integer", "number", "boolean", "date"]
    required: bool


class DraftTool(BaseModel):
    model_config = Strict
    name: str = Field(pattern=TOOL_NAME)
    description: str = Field(min_length=1, max_length=500)
    sql: str = Field(min_length=1, max_length=8000)
    parameters: list[DraftParameter] = Field(default_factory=list, max_length=10)
    max_rows: int = Field(default=200, ge=1, le=1000)


class DraftCaseInput(BaseModel):
    model_config = Strict
    message: str = Field(min_length=1, max_length=4000)


class DraftCase(BaseModel):
    model_config = Strict
    case_id: str = Field(min_length=1, max_length=100)
    input: DraftCaseInput
    assertions: list[Assertion] = Field(min_length=1, max_length=10)


class Draft(BaseModel):
    model_config = Strict
    agent: DraftAgent
    tools: list[DraftTool] = Field(min_length=1, max_length=8)
    screens: list[Screen] = Field(min_length=1, max_length=6)
    evaluation_cases: list[DraftCase] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if sum(1 for s in self.screens if s.kind == "chat") > 1:
            raise ValueError("a draft has at most one chat screen")
        if any(holds_float(a.value) for c in self.evaluation_cases for a in c.assertions):
            raise ValueError(FLOATS_REFUSED)
        return self


# ------------------------------------------------------------------ requests (design section 7)
class AppRequestCreate(BaseModel):
    model_config = Strict
    connection_id: UUID
    tables: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        min_length=1, max_length=50
    )
    brief: str = Field(min_length=1, max_length=4000)
    model_id: UUID


class DraftEdit(BaseModel):
    model_config = Strict
    base_revision_no: int = Field(ge=1)
    draft: Draft


class DraftDiagnostic(BaseModel):
    """One diagnostic of a draft (design 5.4); named apart from `packaging`'s `Diagnostic` so the
    OpenAPI schema names stay unchanged."""

    path: str
    code: str
    message: str
    severity: Literal["error", "warning"]


Diagnostic = DraftDiagnostic


class DraftOut(BaseModel):
    revision_no: int
    source: Literal["model", "edited"]
    draft: dict[str, Any]
    draft_hash: str
    validation: list[DraftDiagnostic]
    created_by: UUID
    created_at: datetime


class AppRequestSummary(BaseModel):
    id: UUID
    workspace_id: UUID
    project_id: UUID
    connection_id: UUID
    status: RequestStatus
    status_reason: str | None
    latest_revision_no: int | None
    solution_version_id: UUID | None
    requested_by: UUID
    created_at: datetime
    updated_at: datetime | None


class AppRequestOut(AppRequestSummary):
    tables: list[str]
    brief: str
    model_id: UUID
    discovery_id: UUID
    accepted_revision_no: int | None
    accept_progress: dict[str, Any]
    revisions: list[DraftOut]
