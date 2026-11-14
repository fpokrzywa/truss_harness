"""Connection, test, discovery and egress-policy DTOs (contract P3 section 4, B1/B2/B4/B6)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal  # Any: connection config / adapter payload JSON
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Strict = ConfigDict(extra="forbid")
ConnectionType = Literal["rest", "mcp", "database", "data_platform", "model_api"]
AuthMethod = Literal[
    "none", "api_key", "bearer", "oauth2_client_credentials", "basic", "key_pair", "mtls"
]
AccessMode = Literal["read", "read_write"]


class ScopeSlugs(BaseModel):
    model_config = Strict
    workspace: str | None = None
    project: str | None = None


class ConnectionCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    scope: ScopeSlugs = Field(default_factory=ScopeSlugs)
    connection_type: ConnectionType
    adapter_key: str = Field(min_length=1, max_length=100)
    vendor_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    auth_method: AuthMethod = "none"
    secret_id: UUID | None = None
    egress_policy_id: UUID | None = None
    access_mode: AccessMode = "read"


class ConnectionPatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    vendor_id: UUID | None = None
    config: dict[str, Any] | None = None
    auth_method: AuthMethod | None = None
    secret_id: UUID | None = None
    egress_policy_id: UUID | None = None
    access_mode: AccessMode | None = None
    row_version: int | None = None


class ConnectionStatusChange(BaseModel):
    model_config = Strict
    status: Literal["active", "disabled", "retired"]
    row_version: int | None = None


class ScopeOut(BaseModel):
    workspace: str | None = None
    project: str | None = None


class ConnectionOut(BaseModel):
    id: UUID
    name: str
    scope: ScopeOut
    connection_type: str
    adapter_key: str
    vendor_id: UUID | None
    config: dict[str, Any]
    auth_method: str
    secret_id: UUID | None
    secret_set: bool
    egress_policy_id: UUID | None
    access_mode: str
    status: str
    health_status: str
    health: str  # alias of health_status (the UI reads `health`)
    last_tested_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime | None


class CheckOut(BaseModel):
    name: str
    status: Literal["pass", "fail", "skipped"]
    detail: str = ""


class TestResultOut(BaseModel):
    __test__ = False  # not a pytest class
    id: UUID
    connection_id: UUID
    outcome: Literal["pass", "fail"]
    checks: list[CheckOut]
    error_class: str | None
    duration_ms: int
    started_at: datetime
    finished_at: datetime
    created_at: datetime


class DiscoveryOut(BaseModel):
    id: UUID
    kind: Literal["openapi", "mcp_tools", "schema"]
    discovered_at: datetime
    payload: dict[str, Any]


class InvokeOut(BaseModel):
    result: Any  # Any: adapter-specific JSON
    truncated: bool
    duration_ms: int


# ------------------------------------------------------------------ egress policies (B2)
class EgressRules(BaseModel):
    model_config = Strict
    allowed_schemes: list[str] | None = Field(default=None, max_length=10)
    allowed_ports: list[int] | None = Field(default=None, max_length=100)
    allowed_hosts: list[str] | None = Field(default=None, max_length=500)
    allowed_cidrs: list[str] | None = Field(default=None, max_length=500)
    allow_private: bool | None = None
    allow_redirects: bool | None = None
    max_response_bytes: int | None = Field(default=None, ge=1, le=1_000_000_000)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    row_version: int | None = None


class EgressPolicyCreate(EgressRules):
    name: str = Field(min_length=1, max_length=200)
    is_default: bool = False


class EgressPolicyPatch(EgressRules):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_default: bool | None = None


class EgressPolicyOut(BaseModel):
    id: UUID
    name: str
    is_default: bool
    allowed_schemes: list[str]
    allowed_ports: list[int]
    allowed_hosts: list[str]
    allowed_cidrs: list[str]
    allow_private: bool
    allow_redirects: bool
    max_response_bytes: int
    timeout_seconds: float
    row_version: int


# ------------------------------------------------------------------ invoke request bodies
class RestInvokeIn(BaseModel):
    model_config = Strict
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    path: str = Field(min_length=1, max_length=2000)
    query: dict[str, str | int | float | bool | None] | None = None
    body: Any = None  # Any: arbitrary JSON request body
    headers: dict[str, str] | None = None
    write: bool = False

    @field_validator("method", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class McpInvokeIn(BaseModel):
    model_config = Strict
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)  # Any: tool arguments
    write: bool = False


class SqlInvokeIn(BaseModel):
    model_config = Strict
    sql: str = Field(min_length=1, max_length=100_000)
    params: list[Any] | dict[str, Any] | None = None  # Any: bound parameter values
    write: bool = False


INVOKE_MODELS: dict[str, type[BaseModel]] = {
    "rest": RestInvokeIn,
    "mcp": McpInvokeIn,
    "database": SqlInvokeIn,
    "data_platform": SqlInvokeIn,
}
