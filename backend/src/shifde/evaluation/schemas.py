"""Evaluation dataset and evaluation run DTOs (sub-project 3 design section 6, settled calls 7, 19
to 22).

A dataset holds 1 to 200 cases, each with a unique `case_id` and 1 to 50 assertions of a closed
list of kinds, each with an `id` unique in its case. A `regex` pattern is at most 200 characters
and must compile with Python `re`. The cases' JSON text is at most 1,048,576 bytes, measured as
the database measures it (`octet_length(cases::text)`, jsonb's text form). Unknown kinds and
extra fields are refused (422).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal, Self  # Any: case inputs and expected JSON values
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Strict = ConfigDict(extra="forbid")
MAX_CASES = 200
MAX_ASSERTIONS = 50
MAX_CASES_BYTES = 1_048_576
MAX_PATTERN = 200

AssertionId = Annotated[str, Field(min_length=1, max_length=100)]


class TextAssertion(BaseModel):
    """`equals`: the output text equals `value`; `contains` / `not_contains`: case-sensitive
    substring."""

    model_config = Strict
    id: AssertionId
    kind: Literal["equals", "contains", "not_contains"]
    value: str


class RegexAssertion(BaseModel):
    """The whole output text matches `value` (Python `re`, full match, 1 s budget)."""

    model_config = Strict
    id: AssertionId
    kind: Literal["regex"]
    value: str = Field(max_length=MAX_PATTERN)

    @field_validator("value")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        return value


class JsonAssertion(BaseModel):
    """The output text parses as JSON and equals `value`."""

    model_config = Strict
    id: AssertionId
    kind: Literal["json_equals"]
    value: Any


Assertion = Annotated[TextAssertion | RegexAssertion | JsonAssertion, Field(discriminator="kind")]


class Case(BaseModel):
    """`input`: `{"message": "..."}` (an agent's input text) or any object (an agent's input as
    canonical JSON text, a pipeline's params)."""

    model_config = Strict
    case_id: str = Field(min_length=1, max_length=100)
    input: dict[str, Any]
    assertions: list[Assertion] = Field(min_length=1, max_length=MAX_ASSERTIONS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [a.id for a in self.assertions]
        if len(ids) != len(set(ids)):
            raise ValueError("assertion ids must be unique within a case")
        return self


class DatasetCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    cases: list[Case] = Field(min_length=1, max_length=MAX_CASES)

    @model_validator(mode="after")
    def _cases(self) -> Self:
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique within a dataset")
        if len(json.dumps(self.stored_cases(), ensure_ascii=False).encode("utf-8")) > (
            MAX_CASES_BYTES
        ):
            raise ValueError(f"the cases exceed {MAX_CASES_BYTES} bytes")
        return self

    def stored_cases(self) -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in self.cases]


class DatasetOut(BaseModel):
    id: UUID
    workspace: str | None
    project: str | None
    name: str
    version_no: int
    cases: list[dict[str, Any]]
    digest: str
    created_by: UUID
    created_at: datetime


class EvaluationRequest(BaseModel):
    """`{}`: the dataset and thresholds come from the release's manifest."""

    model_config = Strict


class AssertionResult(BaseModel):
    id: str
    passed: bool
    reason: str | None


class CaseResult(BaseModel):
    """One case's score; never the case's output text."""

    case_id: str
    run_id: UUID
    passed: bool
    assertions: list[AssertionResult]
    latency_ms: int
    cost_usd: str


class EvaluationRunOut(BaseModel):
    """`accuracy` and `total_cost_usd` are decimal strings; result fields are null until the
    evaluation is scored."""

    id: UUID
    release_id: UUID
    release_digest: str
    dataset_id: UUID
    dataset_digest: str
    thresholds: dict[str, Any]
    status: Literal["queued", "running", "passed", "failed", "error"]
    status_reason: str | None
    accuracy: str | None
    p95_latency_ms: int | None
    total_cost_usd: str | None
    results: list[CaseResult] | None
    requested_by: UUID
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
