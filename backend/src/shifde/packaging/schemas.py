"""Solution and solution version DTOs (sub-project 3 design sections 4 and 5, settled calls 7 and
12).

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


class SolutionVersionCreate(BaseModel):
    model_config = Strict
    entrypoint_kind: Literal["agent", "pipeline"]
    entrypoint_version_id: UUID
    data_reads: list[DataRead] = Field(default_factory=list, max_length=50)
    evaluation: EvaluationContract | None = None


class EntrypointOut(BaseModel):
    kind: str
    version_id: UUID
    spec_hash: str | None


class PinOut(BaseModel):
    role: str
    version_id: UUID
    spec_hash: str | None


class SolutionVersionOut(BaseModel):
    id: UUID
    solution_id: UUID
    version_no: int
    entrypoint: EntrypointOut
    spec: dict[str, Any]
    spec_hash: str
    pins: list[PinOut]
    created_by: UUID
    created_at: datetime
