"""MCP settings bodies and the v1 tool names."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

V1_TOOLS: tuple[str, ...] = (
    "whoami",
    "list_projects",
    "list_agents",
    "start_run",
    "get_run",
    "list_releases",
    "get_release",
    "list_deployments",
    "get_deployment",
    "board_get",
    "assist_ask",
    "assist_get",
)
# `allowed_tools` NULL: every v1 tool except the two that spend (design 10 item 14)
DEFAULT_TOOLS: tuple[str, ...] = tuple(t for t in V1_TOOLS if t not in ("start_run", "assist_ask"))
ToolName = Literal[
    "whoami",
    "list_projects",
    "list_agents",
    "start_run",
    "get_run",
    "list_releases",
    "get_release",
    "list_deployments",
    "get_deployment",
    "board_get",
    "assist_ask",
    "assist_get",
]


class McpSettingsOut(BaseModel):
    """The organization's MCP settings; a missing row reads as the defaults. `endpoint_url` is
    the installation's `SHIFDE_MCP_PUBLIC_URL` (null when unset)."""

    enabled: bool
    rate_per_minute: int
    max_cost_per_call: float
    allowed_tools: list[str] | None
    updated_by: UUID | None
    updated_at: datetime | None
    endpoint_url: str | None


class McpSettingsPut(BaseModel):
    """`PUT .../mcp/settings`: the whole settings document. `allowed_tools` null is the default
    set (every v1 tool except `start_run` and `assist_ask`), an empty list allows no tool."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool
    rate_per_minute: int = Field(ge=1, le=600)
    max_cost_per_call: Decimal = Field(gt=0, le=25, decimal_places=4)
    allowed_tools: list[ToolName] | None = Field(default=None, max_length=len(V1_TOOLS))
