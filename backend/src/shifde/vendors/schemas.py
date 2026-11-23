"""Vendor catalog and adapter registry DTOs (contract P3 sections 1-2, amendments B3/B5/B8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal  # Any: JSON Schema documents
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Strict = ConfigDict(extra="forbid")
KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]$"
Category = Literal["data_platform", "model_provider", "saas_api", "mcp", "other"]
VendorStatus = Literal["active", "disabled", "retired"]


class EvidenceOut(BaseModel):
    run_id: UUID
    finished_at: datetime
    suite_version: str


class CapabilityOut(BaseModel):
    capability: str
    maturity: Literal["planned", "experimental", "validated"]
    evidence: EvidenceOut | None = None


class VendorCapabilityOut(CapabilityOut):
    adapter_key: str | None = None


class AdapterOut(BaseModel):
    key: str
    kind: str
    version: str
    display_name: str
    connection_type: str | None
    capabilities: list[CapabilityOut]
    config_schema: dict[str, Any]
    secret_fields: list[str]
    auth_methods: list[str]
    auth_only_fields: dict[str, list[str]]
    destination_fields: list[str]


class ConformanceRunOut(BaseModel):
    id: UUID
    adapter_key: str
    adapter_version: str
    suite_version: str
    started_at: datetime
    finished_at: datetime
    outcome: Literal["pass", "fail"]
    validated_capabilities: list[str]
    checks: list[dict[str, Any]]
    environment: dict[str, Any]


class VendorCreate(BaseModel):
    model_config = Strict
    key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    category: Category
    description: str | None = Field(default=None, max_length=4000)
    version: str | None = Field(default=None, max_length=100)
    configuration_requirements: list[str] = Field(default_factory=list, max_length=100)
    adapter_keys: list[str] = Field(default_factory=list, max_length=50)


class VendorPatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    version: str | None = Field(default=None, max_length=100)
    configuration_requirements: list[str] | None = Field(default=None, max_length=100)
    adapter_keys: list[str] | None = Field(default=None, max_length=50)
    status: VendorStatus | None = None
    row_version: int | None = None


class VendorStatusChange(BaseModel):
    model_config = Strict
    status: VendorStatus
    row_version: int | None = None


class VendorOut(BaseModel):
    id: UUID
    tenant_id: UUID | None
    is_global: bool
    scope: Literal["global", "tenant"]
    key: str
    name: str
    category: str
    description: str | None
    version: str | None
    status: str
    configuration_requirements: list[str]
    adapter_keys: list[str]
    row_version: int
    created_at: datetime
    updated_at: datetime | None
