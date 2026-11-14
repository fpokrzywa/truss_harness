"""Response body of the platform installation overview (SETH 3ba0e06 shapes, B-1101..B-1106)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MigrationOut(BaseModel):
    current: str | None
    expected_head: str
    ok: bool


class OidcProviderOut(BaseModel):
    name: str
    issuer_host: str | None


class InstallationAuthOut(BaseModel):
    local_enabled: bool
    oidc_providers: list[OidcProviderOut]


class InstallationSecurityOut(BaseModel):
    session_idle_minutes: int
    session_absolute_hours: int
    api_key_max_days: int
    egress_default_allow_private: bool


class InstallationLimitsOut(BaseModel):
    max_run_seconds: int
    max_tool_calls: int
    inline_output_max_bytes: int


class WorkerOut(BaseModel):
    name: str
    last_seen_at: datetime
    status: str  # alive | stale


class InstallationOut(BaseModel):
    version: str
    env: str
    deployment_mode: str
    migration: MigrationOut
    feature_flags: dict[str, bool]
    auth: InstallationAuthOut
    security: InstallationSecurityOut
    limits: InstallationLimitsOut
    workers: list[WorkerOut] | None
