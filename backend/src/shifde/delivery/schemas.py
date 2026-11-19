"""Deployment target and executor DTOs (design sections 3, 5 and 9 items 2, 3, 12 and 17;
DECISIONS B-2501, B-2664).

`TargetOut` never carries the claim id or the address key: the claim is the organization's
private bookkeeping of the one-platform rule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Strict = ConfigDict(extra="forbid")
Environment = Literal["dev", "test", "prod"]
CREDENTIAL_REF = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"


class TargetBindings(BaseModel):
    model_config = Strict
    warehouse_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")


class TargetCreate(BaseModel):
    model_config = Strict
    workspace: str
    project: str | None = None
    name: str = Field(min_length=1, max_length=200)
    platform_kind: Literal["databricks"] = "databricks"
    address: str = Field(min_length=1, max_length=2048)
    environment: Environment
    bindings: TargetBindings
    credential_ref: str = Field(pattern=CREDENTIAL_REF)


class TargetOut(BaseModel):
    id: UUID
    name: str
    workspace: str | None
    project: str | None
    platform_kind: str
    address: str
    environment: str
    bindings: TargetBindings
    credential_ref: str
    status: str
    revision: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime | None


class ExecutorCreate(BaseModel):
    model_config = Strict
    name: str = Field(min_length=1, max_length=200)


class ExecutorPatch(BaseModel):
    model_config = Strict
    status: Literal["active", "disabled"]


class DeliveryExecutorOut(BaseModel):
    id: UUID
    name: str
    status: str
    last_seen_at: datetime | None
    version: str | None
    created_at: datetime
    updated_at: datetime | None


class CredentialOut(BaseModel):
    """Never the secret or its hash: the secret exists only in `CredentialIssued`."""

    id: UUID
    executor_id: UUID
    prefix: str
    status: Literal["live", "revoked", "expired"]
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class CredentialIssued(BaseModel):
    """Issue and rotate: the full `shx_` token, shown once."""

    credential: CredentialOut
    secret: str


class ExecutorWhoami(BaseModel):
    executor_id: UUID
    name: str
    status: str
    version: str | None
    last_seen_at: datetime | None
