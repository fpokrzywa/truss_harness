"""Assist DTOs (SP7 design section 11): settings, the catalog, the project's specialists,
requests and proposals."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal  # Any: proposal JSON documents
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Strict = ConfigDict(extra="forbid")
SpecialistStatus = Literal["installing", "enabled", "disabled"]
RequestStatus = Literal["queued", "classifying", "handing_over", "routed", "refused", "failed"]
ContextKind = Literal["solution", "release", "target", "plan", "deployment", "evaluation_run"]
RouteMethod = Literal["explicit", "rule", "model"]


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


# ====================================================================== requests
class ContextIn(BaseModel):
    """The object a request is about; it must be in the request's project (design 5.1)."""

    model_config = Strict
    kind: ContextKind
    id: UUID


class AssistRequestIn(BaseModel):
    """`POST .../assist/requests`: the person's text, their pick of specialist and a context."""

    model_config = Strict
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    specialist: str | None = None
    context: ContextIn | None = None


class RunRefOut(BaseModel):
    """The request's run, read live under the caller's `run:read`."""

    id: UUID
    status: str
    error_code: str | None


class ProposalOut(BaseModel):
    """A specialist's proposal (design section 6). `payload` is null unless the caller holds,
    on the request's project, the read permissions of what it names; `result_ref` is null unless
    the caller can read the object the accept created."""

    id: UUID
    run_id: UUID
    kind: str
    payload: dict[str, Any] | None
    status: str
    status_reason: str | None
    result_ref: dict[str, Any] | None
    decided_by: UUID | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


class ProposalDecision(BaseModel):
    """`POST .../assist/proposals/{id}/accept` and `/dismiss`: an empty body."""

    model_config = Strict


class AssistRequestSummary(BaseModel):
    id: UUID
    workspace_id: UUID
    project_id: UUID
    status: RequestStatus
    status_reason: str | None
    requested_specialist: str | None
    specialist_key: str | None
    route_method: RouteMethod | None
    run_id: UUID | None
    requested_by: UUID
    created_at: datetime
    updated_at: datetime | None


class AssistRequestOut(AssistRequestSummary):
    """A request with its routing decision, its run and its proposals; never its text.
    `candidates` is recomputed from the stored request (empty once the text is scrubbed);
    `missing_permissions` is set only on the answer to the request that was refused
    `specialist_not_permitted`."""

    rule_id: str | None
    router_version: str | None
    candidates: list[str]
    missing_permissions: list[str] | None = None
    context: ContextIn | None
    run: RunRefOut | None
    proposals: list[ProposalOut]
