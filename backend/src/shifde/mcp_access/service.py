"""The organization's MCP settings (`GET|PUT /api/v1/orgs/{org}/mcp/settings`, design 3.6)."""

from __future__ import annotations

from typing import Any  # Any: audit details

from shifde.audit import service as audit
from shifde.core.settings import Settings
from shifde.mcp_access import store
from shifde.mcp_access.schemas import McpSettingsOut, McpSettingsPut
from shifde.tenancy.scope import TenantScope


def _out(row: store.SettingsRow, settings: Settings) -> McpSettingsOut:
    return McpSettingsOut(
        enabled=row.enabled,
        rate_per_minute=row.rate_per_minute,
        max_cost_per_call=float(row.max_cost_per_call),
        allowed_tools=list(row.allowed_tools) if row.allowed_tools is not None else None,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        endpoint_url=str(settings.mcp_public_url) if settings.mcp_public_url else None,
    )


def _values(row: store.SettingsRow) -> dict[str, Any]:
    return {
        "enabled": row.enabled,
        "rate_per_minute": row.rate_per_minute,
        "max_cost_per_call": format(row.max_cost_per_call, "f"),
        "allowed_tools": list(row.allowed_tools) if row.allowed_tools is not None else None,
    }


def get_settings(scope: TenantScope, settings: Settings) -> McpSettingsOut:
    scope.pdp.require("mcp:settings:read", scope.tenant_ref)
    row = store.read(scope.session, scope.tenant_id) or store.DEFAULTS
    scope.session.rollback()
    return _out(row, settings)


def put_settings(scope: TenantScope, settings: Settings, body: McpSettingsPut) -> McpSettingsOut:
    """Upsert the row and audit `mcp.settings.updated` with the old and new values."""
    scope.pdp.require("mcp:settings:write", scope.tenant_ref)
    session = scope.session
    old = store.read(session, scope.tenant_id) or store.DEFAULTS
    tools: list[str] | None = (
        list(dict.fromkeys(body.allowed_tools)) if body.allowed_tools is not None else None
    )
    store.upsert(
        session,
        scope.tenant_id,
        enabled=body.enabled,
        rate_per_minute=body.rate_per_minute,
        max_cost_per_call=body.max_cost_per_call,
        allowed_tools=tools,
        updated_by=scope.principal.principal_id,
    )
    new = store.read(session, scope.tenant_id)
    assert new is not None  # written above in this transaction
    audit.record(
        session,
        scope.ctx,
        action="mcp.settings.updated",
        tenant_id=scope.tenant_id,
        target_type="mcp_settings",
        target_id=scope.tenant_id,
        details={"old": _values(old), "new": _values(new)},
    )
    session.commit()
    return _out(new, settings)
