"""ORM mapping for the append-only audit log (schema `shifde`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any  # Any: `details` is a redacted free-form JSON document
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shifde.core.db import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID | None]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[UUID | None]
    actor_ip: Mapped[str | None] = mapped_column(Text)
    auth_method: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[UUID | None]
    project_id: Mapped[UUID | None]
    outcome: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trace_id: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    chain_seq: Mapped[int | None] = mapped_column(BigInteger)
