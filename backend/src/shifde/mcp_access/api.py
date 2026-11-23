"""MCP settings routes (design 3.6). Included by `create_app` whether or not `/mcp` is mounted."""

from __future__ import annotations

from fastapi import APIRouter

from shifde.core.deps import SettingsDep
from shifde.mcp_access import service
from shifde.mcp_access.schemas import McpSettingsOut, McpSettingsPut
from shifde.tenancy.dependencies import ScopeDep

router = APIRouter(prefix="/api/v1")


@router.get("/orgs/{org}/mcp/settings", tags=["mcp"])
def get_mcp_settings(scope: ScopeDep, settings: SettingsDep) -> McpSettingsOut:
    return service.get_settings(scope, settings)


@router.put("/orgs/{org}/mcp/settings", tags=["mcp"])
def put_mcp_settings(
    scope: ScopeDep, settings: SettingsDep, body: McpSettingsPut
) -> McpSettingsOut:
    return service.put_settings(scope, settings, body)
