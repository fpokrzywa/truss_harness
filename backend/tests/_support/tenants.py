"""Client rows for test setup.

Since tenancy_0001 every client-owned table keys `tenant_id` to `shifde.tenants`, so a test that
writes rows for a fresh client id first creates that client. These helpers write as the schema
owner (the caller passes an owner connection or session); assertions about the application still
run as `shifde_app` under row-level security.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7

_INSERT = text(
    "INSERT INTO shifde.tenants (id, slug, name, settings) "
    "VALUES (:id, :slug, :name, CAST(:settings AS jsonb)) ON CONFLICT (id) DO NOTHING"
)


def slug_of(tenant_id: UUID) -> str:
    """A slug that is unique per id and matches `ck_tenants__slug`."""
    return f"t-{tenant_id.hex}"


def add_tenants(owner: Connection | Session, *tenant_ids: UUID) -> None:
    """Create a minimal client row for each id that has none (owner rights)."""
    for tenant_id in tenant_ids:
        owner.execute(
            _INSERT,
            {
                "id": tenant_id,
                "slug": slug_of(tenant_id),
                "name": slug_of(tenant_id),
                "settings": "{}",
            },
        )


def add_tenant(
    owner: Connection | Session,
    tenant_id: UUID | None = None,
    *,
    slug: str | None = None,
    settings: dict[str, Any] | None = None,  # Any: jsonb settings document
) -> UUID:
    """Create one client (owner rights) and return its id."""
    tid = tenant_id or uuid7()
    owner.execute(
        _INSERT,
        {
            "id": tid,
            "slug": slug or slug_of(tid),
            "name": slug or slug_of(tid),
            "settings": json.dumps(settings or {}),
        },
    )
    return tid


def add_workspace(
    owner: Connection | Session, tenant_id: UUID, workspace_id: UUID | None = None
) -> UUID:
    """Create a workspace of `tenant_id` (owner rights) and return its id."""
    wid = workspace_id or uuid7()
    owner.execute(
        text("INSERT INTO shifde.workspaces (id, tenant_id, slug, name) VALUES (:i, :t, :s, :s)"),
        {"i": wid, "t": tenant_id, "s": f"w-{wid.hex}"},
    )
    return wid


def add_project(
    owner: Connection | Session,
    tenant_id: UUID,
    workspace_id: UUID,
    project_id: UUID | None = None,
) -> UUID:
    """Create a project of `tenant_id` in `workspace_id` (owner rights) and return its id."""
    pid = project_id or uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
            "VALUES (:i, :t, :w, :s, :s)"
        ),
        {"i": pid, "t": tenant_id, "w": workspace_id, "s": f"p-{pid.hex}"},
    )
    return pid


def bind_user(
    owner: Connection | Session, tenant_id: UUID, user_id: UUID, role_key: str = "viewer"
) -> UUID:
    """A direct binding of a system role template at organization scope for a member of
    `tenant_id` (owner rights, `check_role_binding` runs). Since tenancy_0002 a person sees a
    client, and a session pinned to it resolves, only with a live binding there (B-1009).

    Binding `tenant_administrator` here names the client's owner, which since tenancy_0003 only
    the platform-administrator context or the owner may do (B-1016): the helper sets the
    platform-administrator context for that one insert, as `create_org` does, and restores it."""
    bid = uuid7()
    names_owner = role_key == "tenant_administrator"
    previous = ""
    if names_owner:
        previous = owner.execute(
            text("SELECT COALESCE(current_setting('app.platform_admin', true), '')")
        ).scalar_one()
        owner.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
    owner.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) SELECT :i, :t, 'user', :u, r.id, 'tenant', :t "
            "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = :k"
        ),
        {"i": bid, "t": tenant_id, "u": user_id, "k": role_key},
    )
    if names_owner:
        owner.execute(text("SELECT set_config('app.platform_admin', :v, true)"), {"v": previous})
    return bid
