"""Model provider / model / allowlist / route DTOs (contract P3 section 3, amendments B1/B7/B9)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal  # Any: provider settings / route conditions JSON
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Strict = ConfigDict(extra="forbid")
Lifecycle = Literal["preview", "active", "deprecated", "retired"]
ModelCapability = Literal["chat", "tools", "vision", "json_mode", "embeddings"]
ScopeType = Literal["tenant", "workspace", "project"]


# ------------------------------------------------------------------ providers
class ProviderCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    adapter_key: str = Field(min_length=1, max_length=100)
    connection_id: UUID
    settings: dict[str, Any] = Field(default_factory=dict)
    vendor_id: UUID | None = None


class ProviderPatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class ProviderOut(BaseModel):
    id: UUID
    name: str
    adapter_key: str
    connection_id: UUID | None  # None only on the shared SHI provider (DECISIONS B-1716)
    vendor_id: UUID | None
    settings: dict[str, Any]
    status: str
    health_status: str
    health_checked_at: datetime | None
    row_version: int
    created_at: datetime


class HealthOut(BaseModel):
    status: Literal["healthy", "degraded", "unavailable"]
    latency_ms: int
    checked_at: datetime
    detail: str | None


class DiscoveredModelOut(BaseModel):
    model_key: str
    display_name: str | None
    context_window: int | None
    max_output_tokens: int | None
    capabilities: list[str]


# ------------------------------------------------------------------ models
class RateLimits(BaseModel):
    model_config = Strict
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1, le=1000)


class ModelCreate(BaseModel):
    model_config = Strict
    provider_id: UUID
    model_key: str = Field(min_length=1, max_length=300)
    display_name: str = Field(min_length=1, max_length=200)
    family: str | None = Field(default=None, max_length=200)
    version_label: str | None = Field(default=None, max_length=200)
    capabilities: list[ModelCapability] = Field(default_factory=list, max_length=10)
    context_window: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    lifecycle: Lifecycle = "preview"
    deprecation_date: date | None = None
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    pricing_ref: None = None  # reserved for the P8 pricing registry


class ModelPatch(BaseModel):
    model_config = Strict
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    family: str | None = Field(default=None, max_length=200)
    version_label: str | None = Field(default=None, max_length=200)
    capabilities: list[ModelCapability] | None = Field(default=None, max_length=10)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    lifecycle: Lifecycle | None = None
    deprecation_date: date | None = None
    rate_limits: RateLimits | None = None
    row_version: int | None = None


class ModelOut(BaseModel):
    id: UUID
    provider_id: UUID
    model_key: str
    display_name: str
    family: str | None
    version_label: str | None
    capabilities: list[str]
    context_window: int | None
    max_output_tokens: int | None
    lifecycle: str
    deprecation_date: date | None
    rate_limits: dict[str, Any]
    pricing_ref: str | None
    row_version: int
    created_at: datetime


# ------------------------------------------------------------------ the SHI-wide provider
class ShiProviderPut(BaseModel):
    """The platform administrator creates (first PUT) or changes the shared SHI provider. It has
    no settings: SHI's endpoint and key reference are installation configuration (B-1712)."""

    model_config = Strict
    adapter_key: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class ShiAccessPut(BaseModel):
    model_config = Strict
    enabled: bool
    row_version: int | None = None


class ShiAccessOut(BaseModel):
    """A client's own switch for the SHI provider (shown to its owner). Who changed it is a
    platform administrator, not a person of the client: the client's audit trail names them."""

    enabled: bool
    changed_at: datetime | None
    row_version: int | None


class PlatformShiAccessOut(ShiAccessOut):
    org: str
    changed_by: UUID | None


# ------------------------------------------------------------------ allowlists / routes
class AllowlistPut(BaseModel):
    model_config = Strict
    enabled: bool
    model_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class AllowlistOut(BaseModel):
    enabled: bool
    model_ids: list[UUID]
    scope_type: ScopeType
    scope_id: UUID


class RouteScope(BaseModel):
    model_config = Strict
    type: ScopeType
    id: UUID


class RouteCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    scope: RouteScope
    primary_model_id: UUID
    fallback_model_ids: list[UUID] = Field(default_factory=list, max_length=20)
    conditions: dict[str, Any] = Field(default_factory=dict)


class RoutePatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    primary_model_id: UUID | None = None
    fallback_model_ids: list[UUID] | None = Field(default=None, max_length=20)
    conditions: dict[str, Any] | None = None
    row_version: int | None = None


class RouteOut(BaseModel):
    id: UUID
    name: str
    scope: RouteScope
    primary_model_id: UUID
    fallback_model_ids: list[UUID]
    conditions: dict[str, Any]
    row_version: int


# ------------------------------------------------------------------ invoke
class ChatMessageIn(BaseModel):
    model_config = Strict
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class InvokeIn(BaseModel):
    model_config = Strict
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=200)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int


class InvokeOut(BaseModel):
    output: str
    usage: UsageOut
    latency_ms: int
    provider_request_id: str | None = None
