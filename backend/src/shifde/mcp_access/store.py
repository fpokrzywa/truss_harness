"""`mcp_client_settings` rows (mcp_0001), read and written under the caller's client context
(row-level security keeps every statement in that client)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class SettingsRow:
    enabled: bool
    rate_per_minute: int
    max_cost_per_call: Decimal
    allowed_tools: tuple[str, ...] | None
    updated_by: UUID | None
    updated_at: datetime | None


DEFAULTS = SettingsRow(False, 60, Decimal("1.0000"), None, None, None)

_SELECT = text(
    "SELECT enabled, rate_per_minute, max_cost_per_call, allowed_tools, updated_by, updated_at "
    "FROM shifde.mcp_client_settings WHERE tenant_id = :t"
)
_UPSERT = text(
    "INSERT INTO shifde.mcp_client_settings (tenant_id, enabled, rate_per_minute, "
    "max_cost_per_call, allowed_tools, updated_by, updated_at) VALUES (:t, :e, :r, :m, :a, :u, "
    "now()) ON CONFLICT (tenant_id) DO UPDATE SET enabled = excluded.enabled, "
    "rate_per_minute = excluded.rate_per_minute, max_cost_per_call = excluded.max_cost_per_call, "
    "allowed_tools = excluded.allowed_tools, updated_by = excluded.updated_by, "
    "updated_at = excluded.updated_at"
)


def read(session: Session, tenant_id: UUID) -> SettingsRow | None:
    """The client's row, or None when it has none."""
    row = session.execute(_SELECT, {"t": tenant_id}).first()
    if row is None:
        return None
    tools = row.allowed_tools
    return SettingsRow(
        enabled=bool(row.enabled),
        rate_per_minute=int(row.rate_per_minute),
        max_cost_per_call=Decimal(row.max_cost_per_call),
        allowed_tools=tuple(tools) if tools is not None else None,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def upsert(
    session: Session,
    tenant_id: UUID,
    *,
    enabled: bool,
    rate_per_minute: int,
    max_cost_per_call: Decimal,
    allowed_tools: list[str] | None,
    updated_by: UUID,
) -> None:
    session.execute(
        _UPSERT,
        {
            "t": tenant_id,
            "e": enabled,
            "r": rate_per_minute,
            "m": max_cost_per_call,
            "a": allowed_tools,
            "u": updated_by,
        },
    )
