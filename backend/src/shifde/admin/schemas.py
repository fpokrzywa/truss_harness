"""Request/response DTOs for the admin API (contract: SETH docs/api/PHASE_2_CONTRACT.md).

Ported from SETH 3ba0e06 `admin/schemas.py`, part 1 (IAM: permissions, users, roles, role
bindings, groups), part 2 (service accounts, API keys; B-1601), part 3a (IdPs, B-1621) and
part 3b (secrets and the audit read API, B-1641).
Changes (DECISIONS B-1401..B-1404): groups are platform-level (design 5.3). A client sees a
group it has bound only as `BoundGroupOut` (id and name, B-930); `GroupRef` in a user's entry
loses `slug`; the full group shapes (`GroupOut`, `GroupDetailOut`) are the platform
administrator's (`/platform/groups`) and gain the IdP mapping. No client-facing shape carries
`users.last_login_at` or `installation_settings.updated_by`.

No response model contains a secret value, key hash or ciphertext. Request models reject unknown
fields, so ids smuggled in bodies (e.g. a `tenant_id`) are a 422, never silently honoured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal  # Any: free-form audit details
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
Strict = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ shared
class ScopeIn(BaseModel):
    model_config = Strict
    type: Literal["tenant", "workspace", "project", "resource"]
    id: UUID
    resource_type: str | None = Field(default=None, max_length=100)


class ScopeOut(BaseModel):
    type: str
    id: UUID
    resource_type: str | None = None


class UserRef(BaseModel):
    id: UUID
    email: str
    display_name: str


# ------------------------------------------------------------------ permissions / roles
class PermissionOut(BaseModel):
    key: str
    resource_type: str
    action: str
    description: str
    is_privileged: bool


class RoleCreate(BaseModel):
    model_config = Strict
    key: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    permissions: list[str] = Field(max_length=300)


class RolePatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    permissions: list[str] | None = Field(default=None, max_length=300)
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class RoleOut(BaseModel):
    id: UUID
    key: str
    name: str
    description: str | None
    is_system: bool
    status: str
    permissions: list[str]
    row_version: int


class BindingCreate(BaseModel):
    model_config = Strict
    subject_type: Literal["user", "group", "service_account"]
    subject_id: UUID
    role_id: UUID
    scope: ScopeIn
    expires_at: datetime | None = None


class GroupBindingCreate(BaseModel):
    """A platform group bound in a client by the platform administrator (B-1602)."""

    model_config = Strict
    group_id: UUID
    role_id: UUID
    scope: ScopeIn
    expires_at: datetime | None = None


class BindingOut(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    subject_label: str | None = None
    role_id: UUID
    role_name: str
    scope: ScopeOut
    expires_at: datetime | None
    created_by: UUID | None
    created_at: datetime


class MePermissionsOut(BaseModel):
    permissions: list[str]


# ------------------------------------------------------------------ users
class GroupRef(BaseModel):
    """A group bound in this client that the person is in: id and name only (B-930)."""

    id: UUID
    name: str


class UserAdminOut(BaseModel):
    user: UserRef
    status: str
    row_version: int
    groups: list[GroupRef]
    role_bindings: list[BindingOut]


class UserCreate(BaseModel):
    model_config = Strict
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(min_length=1, max_length=200)
    role_ids: list[UUID] = Field(default_factory=list, max_length=50)


class UserCreated(BaseModel):
    user: UserAdminOut
    setup_token: str | None = None
    setup_token_expires_at: datetime | None = None


class UserPatch(BaseModel):
    model_config = Strict
    status: Literal["active", "suspended"]
    row_version: int


# ------------------------------------------------------------------ groups
class BoundGroupOut(BaseModel):
    """What a client sees of a platform group bound in it (B-930): id and name, and the
    group's bindings in this client. Never its members, slug, status or IdP mapping."""

    id: UUID
    name: str
    role_bindings: list[BindingOut] = Field(default_factory=list)


class GroupCreate(BaseModel):
    """A platform group (platform administrator only). `idp_config_id` and `external_id`
    together map an external IdP group name onto it (`source = 'idp'`, installation IdP only)."""

    model_config = Strict
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    idp_config_id: UUID | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=500)


class GroupPatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class GroupMembersPut(BaseModel):
    model_config = Strict
    user_ids: list[UUID] = Field(max_length=5000)
    row_version: int


class GroupOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None
    source: str
    idp_config_id: UUID | None
    external_id: str | None
    status: str
    row_version: int
    member_count: int
    created_at: datetime
    updated_at: datetime | None


class GroupMemberOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    source: str


class GroupDetailOut(GroupOut):
    members: list[GroupMemberOut]


# ------------------------------------------------------------------ service accounts / keys
class ServiceAccountCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ServiceAccountPatch(BaseModel):
    model_config = Strict
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class ServiceAccountOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: str
    row_version: int
    created_at: datetime
    updated_at: datetime | None


class ResourceScopeIn(BaseModel):
    model_config = Strict
    workspace_ids: list[UUID] = Field(default_factory=list, max_length=100)
    project_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ApiKeyCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    principal_type: Literal["service_account", "user"]
    principal_id: UUID
    scopes: list[str] = Field(min_length=1, max_length=300)
    resource_scope: ResourceScopeIn | None = None
    expires_in_days: int = Field(ge=1)


class ApiKeyRotate(BaseModel):
    model_config = Strict
    grace_hours: int = Field(default=24, ge=0, le=168)


class ApiKeyOut(BaseModel):
    id: UUID
    name: str
    prefix: str
    principal_type: str
    principal_id: UUID
    scopes: list[str]
    resource_scope: dict[str, list[str]] | None
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    rotated_from_id: UUID | None
    created_at: datetime
    status: Literal["active", "revoked", "expired"]


class ApiKeyCreated(BaseModel):
    api_key: ApiKeyOut
    secret: str  # shown exactly once, in this response only


# ------------------------------------------------------------------ identity providers (part 3a)
class IdpCreate(BaseModel):
    model_config = Strict
    display_name: str = Field(min_length=1, max_length=200)
    issuer: str = Field(min_length=1, max_length=2000)
    discovery_url: str = Field(min_length=1, max_length=2000)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"], max_length=30)
    claim_mappings: dict[str, str] = Field(
        default_factory=lambda: {"email": "email", "groups": "groups"}
    )
    jit: Literal["off", "create_user", "create_and_join"] = "off"
    group_sync: Literal["off", "sync"] = "off"
    status: Literal["active", "disabled"] = "active"

    @field_validator("claim_mappings")
    @classmethod
    def _claims(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_claims(value)


class IdpPatch(BaseModel):
    model_config = Strict
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    issuer: str | None = Field(default=None, min_length=1, max_length=2000)
    discovery_url: str | None = Field(default=None, min_length=1, max_length=2000)
    client_id: str | None = Field(default=None, min_length=1, max_length=500)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    scopes: list[str] | None = Field(default=None, max_length=30)
    claim_mappings: dict[str, str] | None = None
    jit: Literal["off", "create_user", "create_and_join"] | None = None
    group_sync: Literal["off", "sync"] | None = None
    status: Literal["active", "disabled"] | None = None
    row_version: int

    @field_validator("claim_mappings")
    @classmethod
    def _claims(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return None if value is None else _check_claims(value)


def _check_claims(value: dict[str, str]) -> dict[str, str]:
    if len(value) > 10 or any(len(k) > 50 or len(v) > 200 for k, v in value.items()):
        raise ValueError("claim_mappings is too large")
    return value


class IdpOut(BaseModel):
    id: UUID
    tenant_id: UUID | None
    display_name: str
    issuer: str
    discovery_url: str
    client_id: str
    client_secret_set: bool
    scopes: list[str]
    claim_mappings: dict[str, str]
    jit: str
    group_sync: str
    status: str
    row_version: int
    created_at: datetime
    updated_at: datetime | None


class IdpCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class IdpTestOut(BaseModel):
    ok: bool
    checks: list[IdpCheck]


# ------------------------------------------------------------------ secrets (part 3b)
class SecretScope(BaseModel):
    model_config = Strict
    type: Literal["tenant", "workspace", "project"]
    id: UUID | None = None


class SecretCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=16384)
    kind: Literal["generic", "api_key", "password", "token", "pem"] = "generic"
    scope: SecretScope | None = None  # omitted = organization-wide


class SecretRotate(BaseModel):
    model_config = Strict
    value: str = Field(min_length=1, max_length=16384)
    row_version: int | None = None


class SecretPatch(BaseModel):
    model_config = Strict
    status: Literal["disabled"]
    row_version: int


class SecretOut(BaseModel):
    """Metadata only: never the value, ciphertext or key material (B-803)."""

    id: UUID
    name: str
    kind: str
    scope: SecretScope | None
    status: str
    version: int
    created_at: datetime
    rotated_at: datetime | None
    last_used_at: datetime | None
    row_version: int


# ------------------------------------------------------------------ audit (part 3b)
class AuditEventOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor: str | None
    actor_id: UUID | None
    actor_type: str
    action: str
    target: str | None
    target_type: str | None
    target_id: str | None
    outcome: str
    request_id: str | None
    auth_method: str | None
    actor_ip: str | None
    workspace_id: UUID | None
    project_id: UUID | None
    details: dict[str, Any]
    chain_seq: int | None
    hash: str | None


class AuditVerifyOut(BaseModel):
    ok: bool
    checked: int
    first_broken_id: UUID | None = None
