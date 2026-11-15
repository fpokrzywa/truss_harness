"""Request / response models of the P7 query, policy and lineage API (contract sections 3-4,
amendment F1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal  # Any: row values / attribute values
from uuid import UUID

from pydantic import BaseModel, Field

from shifde.core.bodies import Body

MAX_LIMIT = 1000
DEFAULT_LIMIT = 100
MAX_HOPS = 8
MAX_DEPTH = 3
SubjectType = Literal["role", "group", "user"]
Direction = Literal["forward", "reverse"]


class HopIn(Body):
    relationship: str = Field(min_length=1, max_length=200)
    direction: Direction = "forward"  # amendment F1
    select: list[str] = Field(min_length=1, max_length=200)
    filter: str | None = Field(default=None, max_length=1000)
    traverse: list[HopIn] = Field(default_factory=list, max_length=MAX_HOPS)


class QueryIn(Body):
    version_id: UUID | None = None  # default: the latest published version
    object_type: str = Field(min_length=1, max_length=200)
    select: list[str] = Field(min_length=1, max_length=200)
    filter: str | None = Field(default=None, max_length=1000)
    traverse: list[HopIn] = Field(default_factory=list, max_length=MAX_HOPS)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=500)


class QueryOut(BaseModel):
    rows: list[dict[str, Any]]
    next_cursor: str | None = None
    lineage_id: UUID
    truncated: bool = False  # a related-row cap was reached (additive; see DECISIONS)


class RelationshipSummaryOut(BaseModel):
    key: str
    label: str
    direction: Direction
    count: int
    capped: bool = False


class ObjectOut(BaseModel):
    object: dict[str, Any]
    relationships: list[RelationshipSummaryOut]
    lineage_id: UUID


class PolicySubject(Body):
    type: SubjectType
    id: UUID


class PolicyIn(Body):
    """Create / full replace (PATCH takes the whole body + `row_version`, amendment F1)."""

    object_type_key: str = Field(min_length=1, max_length=200)
    property_keys: list[str] | None = Field(default=None, min_length=1, max_length=500)
    subject: PolicySubject
    effect: Literal["allow"] = "allow"
    row_filter: str | None = Field(default=None, max_length=1000)
    row_version: int | None = None


class PolicyOut(BaseModel):
    id: UUID
    object_type_key: str
    property_keys: list[str] | None
    subject: PolicySubject
    effect: Literal["allow"]
    row_filter: str | None
    row_version: int
    created_at: datetime
    updated_at: datetime | None = None


class PrincipalOut(BaseModel):
    id: UUID
    display: str


class LineageOut(BaseModel):
    id: UUID
    at: datetime
    principal: PrincipalOut
    run_id: UUID | None
    ontology_id: UUID
    ontology_version_id: UUID
    channel: str
    object_types: list[str]
    connections: list[UUID]
    compiled_query_hashes: list[str]
    row_counts: dict[str, int]
    duration_ms: int
    outcome: str
    error_code: str | None = None


class AttributesIn(Body):
    attributes: dict[str, Any] = Field(default_factory=dict, max_length=50)  # Any: scalar / list
    row_version: int | None = None


class AttributesOut(BaseModel):
    principal_type: Literal["user", "service_account"]
    principal_id: UUID
    attributes: dict[str, Any]
    row_version: int
    updated_at: datetime


HopIn.model_rebuild()
