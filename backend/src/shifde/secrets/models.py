"""ORM mappings for secrets (schema `shifde`). Ciphertext columns never leave this module."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from shifde.core.db import Base


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID | None]
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="generic")
    provider_key: Mapped[str] = mapped_column(Text, default="db_encrypted")
    external_ref: Mapped[str | None] = mapped_column(Text)
    scope_type: Mapped[str] = mapped_column(Text, default="tenant")
    scope_id: Mapped[UUID | None]
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    kek_id: Mapped[str | None] = mapped_column(Text)
    secret_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Text, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
