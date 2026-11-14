"""Public DTOs for authentication (contract: docs/api/PHASE_1_CONTRACT.md)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class OidcProviderOut(BaseModel):
    id: str
    display_name: str
    login_url: str


class AuthConfigOut(BaseModel):
    local_enabled: bool
    oidc_providers: list[OidcProviderOut]


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_platform_admin: bool


class OrgRefOut(BaseModel):
    id: UUID
    slug: str
    name: str
    status: str


class MembershipOut(BaseModel):
    org: OrgRefOut
    roles: list[str]


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str
    memberships: list[MembershipOut]
    expires_at: datetime
    features: list[str] = Field(default_factory=list)  # areas this backend implements


class MemberUserOut(BaseModel):
    id: UUID
    email: str
    display_name: str


class MemberOut(BaseModel):
    user: MemberUserOut
    status: str
    roles: list[str]


class RedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=10, max_length=200)
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class PendingLinkOut(BaseModel):
    status: str
