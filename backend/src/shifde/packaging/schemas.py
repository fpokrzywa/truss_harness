"""Solution, solution version and release DTOs (sub-project 3 design sections 4 and 5, settled
calls 7, 12 and 13).

Evaluation thresholds are decimal strings, normalized here (no exponent, no trailing zeros), so
`"0.90"` and `"0.9"` give one stored spec and one `spec_hash`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal  # Any: stored JSON specs
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Strict = ConfigDict(extra="forbid")
TARGET_KIND = "databricks.app/v1"
DECIMAL = r"^[0-9]{1,12}(\.[0-9]{1,12})?$"

TableName = Annotated[str, Field(min_length=1, max_length=256)]


def normalize_decimal(value: str) -> str:
    """`value` without exponent, leading or trailing zeros ("0.90" -> "0.9", "1.0" -> "1")."""
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("not a decimal") from exc
    if not number.is_finite():
        raise ValueError("not a finite decimal")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


class SolutionCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    target_kind: Literal["databricks.app/v1"]


class SolutionOut(BaseModel):
    id: UUID
    workspace: str | None
    project: str | None
    name: str
    target_kind: str
    status: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime | None


class DataRead(BaseModel):
    model_config = Strict
    connection_id: UUID
    tables: list[TableName] = Field(min_length=1, max_length=200)


class EvaluationContract(BaseModel):
    model_config = Strict
    dataset_id: UUID
    min_accuracy: str = Field(pattern=DECIMAL)
    max_p95_latency_ms: int = Field(gt=0, strict=True)
    max_total_cost_usd: str = Field(pattern=DECIMAL)

    @field_validator("min_accuracy")
    @classmethod
    def _accuracy(cls, value: str) -> str:
        normalized = normalize_decimal(value)
        if Decimal(normalized) > 1:
            raise ValueError("min_accuracy must be between 0 and 1")
        return normalized

    @field_validator("max_total_cost_usd")
    @classmethod
    def _cost(cls, value: str) -> str:
        return normalize_decimal(value)


# the screens of a generated app (SP5 design 5.3 and 6.2): a chat with the agent, or a table of one
# read-only tool's rows; `appgen` drafts them and solution versions carry them
TOOL_NAME = r"^[a-z][a-z0-9_]{1,62}$"
PARAMETER_NAME = r"^[a-z_][a-z0-9_]{0,62}$"
ScreenTitle = Annotated[str, Field(min_length=1, max_length=80)]


class ChatScreen(BaseModel):
    model_config = Strict
    kind: Literal["chat"]
    title: ScreenTitle
    placeholder: str = Field(default="", max_length=200)


class TableScreen(BaseModel):
    model_config = Strict
    kind: Literal["table"]
    title: ScreenTitle
    tool: str = Field(pattern=TOOL_NAME)
    inputs: list[Annotated[str, Field(pattern=PARAMETER_NAME)]] = Field(
        default_factory=list, max_length=10
    )


Screen = Annotated[ChatScreen | TableScreen, Field(discriminator="kind")]


class SolutionVersionCreate(BaseModel):
    model_config = Strict
    entrypoint_kind: Literal["agent", "pipeline"]
    entrypoint_version_id: UUID
    data_reads: list[DataRead] = Field(default_factory=list, max_length=50)
    evaluation: EvaluationContract | None = None
    screens: list[Screen] = Field(default_factory=list)


class EntrypointOut(BaseModel):
    """`version_id` and `spec_hash` are null for a version the caller cannot see."""

    kind: str
    version_id: UUID | None
    spec_hash: str | None


class SolutionPinOut(BaseModel):
    """`version_id` and `spec_hash` are null for a version the caller cannot see."""

    role: str
    version_id: UUID | None
    spec_hash: str | None


class SolutionVersionOut(BaseModel):
    id: UUID
    solution_id: UUID
    version_no: int
    entrypoint: EntrypointOut
    spec: dict[str, Any]
    spec_hash: str
    pins: list[SolutionPinOut]
    created_by: UUID
    created_at: datetime


# ------------------------------------------------------------------ releases
PinName = Annotated[str, Field(min_length=1, max_length=200)]
MAX_BINDINGS = 200


class ModelBinding(BaseModel):
    model_config = Strict
    provider_id: UUID
    model: str = Field(min_length=1, max_length=300)


class EnvironmentBindings(BaseModel):
    """Keys are pin names: a model id (or `route-<route id>`) and a connection id."""

    model_config = Strict
    model_bindings: dict[PinName, ModelBinding] = Field(
        default_factory=dict, max_length=MAX_BINDINGS
    )
    connection_bindings: dict[PinName, UUID] = Field(default_factory=dict, max_length=MAX_BINDINGS)


class ReleaseCreate(BaseModel):
    model_config = Strict
    environment_bindings: EnvironmentBindings


class ReleaseWithdraw(BaseModel):
    model_config = Strict
    note: str = Field(min_length=1, max_length=2000)


class Diagnostic(BaseModel):
    """One feature of the compatibility check; `reason` is null when it is supported."""

    path: str
    status: Literal["supported", "unsupported"]
    reason: str | None
    mandatory: bool


class ReleaseOut(BaseModel):
    """A release; pins and tool paths of versions the caller cannot see are redacted, as are
    model and connection paths and bindings only those versions name, and connection ids the
    caller cannot read (null)."""

    id: UUID
    solution_id: UUID
    solution_version_id: UUID
    version_no: int
    workspace: str | None
    project: str | None
    digest: str
    status: str
    manifest: dict[str, Any]
    compatibility: list[Diagnostic]
    sealed_by: UUID
    sealed_at: datetime
    updated_at: datetime | None
