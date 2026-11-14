"""ORM mappings for tenancy tables (schema `shifde`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any  # Any: jsonb settings documents are arbitrary JSON
from uuid import UUID

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shifde.core.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None]


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None]


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None]


class InstallationSettings(Base):
    __tablename__ = "installation_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branding: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    feature_flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    auth_modes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    default_limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
