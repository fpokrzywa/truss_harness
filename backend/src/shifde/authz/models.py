"""ORM mappings for authz tables (schema `shifde`)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from shifde.core.db import Base


class Permission(Base):
    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    resource_type: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    is_privileged: Mapped[bool] = mapped_column(Boolean, default=False)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID | None]
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(Text, default="active")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(primary_key=True)
    permission_key: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[UUID | None]


class RoleBinding(Base):
    __tablename__ = "role_bindings"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[UUID]
    role_id: Mapped[UUID]
    scope_type: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[UUID]
    resource_type: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[UUID] = mapped_column(primary_key=True)  # platform-level (identity_0005, B-928)
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="local")
    external_id: Mapped[str | None] = mapped_column(Text)
    idp_config_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(Text, default="active")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None]


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text, default="manual")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    principal_id: Mapped[UUID | None]
    principal_type: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    matched_binding_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)))
    policy_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), server_default="{}")
    run_id: Mapped[UUID | None]
    trace_id: Mapped[str | None] = mapped_column(Text)
