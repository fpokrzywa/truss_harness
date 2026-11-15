"""Request/response DTOs for the admin API (contract: SETH docs/api/PHASE_2_CONTRACT.md).

Ported from SETH 3ba0e06 `admin/schemas.py`, part 1 (IAM: permissions, users, roles, role
bindings, groups) and part 2 (service accounts, API keys; B-1601); IdPs, secrets and audit come
with part 3.
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
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
