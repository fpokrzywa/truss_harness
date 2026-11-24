"""Assist DTOs (SP7 design section 11): settings, the catalog and the project's specialists."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Strict = ConfigDict(extra="forbid")
SpecialistStatus = Literal["installing", "enabled", "disabled"]


class AssistSettingsOut(BaseModel):
    """The organization's assist settings; a missing row reads as the defaults.
    `current_catalog_version` is the code's catalog version, the only pin install accepts."""

    assist_enabled: bool
    catalog_version: str | None
    current_catalog_version: str
    model_routing_enabled: bool
    router_model_id: UUID | None
    specialist_max_cost: float
    updated_at: datetime | None


class AssistSettingsPut(BaseModel):
    """`PUT .../assist/settings`: the routing fields and the specialist cost cap. The switch and
    the pin change only through `/assist/enable` and `/assist/disable`."""

    model_config = Strict
    model_routing_enabled: bool = False
    router_model_id: UUID | None = None
    specialist_max_cost: Decimal = Field(gt=0, max_digits=12, decimal_places=4)


class InstallIn(BaseModel):
    """`POST .../assist/install`: the model of every installed agent and the catalog keys (all six,
    in catalog order, when left out)."""

    model_config = Strict
    model_id: UUID
    keys: list[str] | None = Field(default=None, min_length=1, max_length=6)


class SpecialistOut(BaseModel):
    """One catalog entry installed in a project. `available` is true only while the organization
    has assist enabled on the current catalog version and the specialist is enabled."""

    id: UUID
    key: str
    title: str
    workspace_id: UUID
    project_id: UUID
    status: SpecialistStatus
    agent_id: UUID | None
    agent_version_id: UUID | None
    catalog_version: str
    available: bool
    installed_by: UUID
    created_at: datetime
    updated_at: datetime | None


class InstallStatus(BaseModel):
    workspace_id: UUID
    project_id: UUID
    status: SpecialistStatus
    available: bool


class CatalogEntryOut(BaseModel):
    key: str
    title: str
    router_description: str
    tools: list[str]
    required_permissions: list[str]
    proposal_kinds: list[str]
    installs: list[InstallStatus]


class AssistCatalogOut(BaseModel):
    """The code catalog with, per entry, its installs in the projects the caller may read assist
    requests of. Instructions are never shown."""

    catalog_version: str
    assist_enabled: bool
    entries: list[CatalogEntryOut]
