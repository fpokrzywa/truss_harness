"""Development seed data (runs as the DB owner; callers must gate on SHIFDE_ENV=development).

SETH 3ba0e06 `cli/seeding.py` with the changes of DECISIONS B-2233: writes that the schema allows
only to the platform administrator (adding a member, binding the client's owner) run in that
context, and `with_groups=True` also creates one SHI-wide group per organization with one staff
person, bound to that organization only (groups are platform-level since identity_0005, B-928)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.authz import repository as authz_repo
from shifde.authz import service as authz
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.ids import uuid7
from shifde.identity import service as identity
from shifde.identity.passwords import PasswordService
from shifde.tenancy import repository as tenancy_repo
from shifde.tenancy.models import Tenant


@dataclass(frozen=True)
class _SeedUser:
    email: str
    display_name: str
    is_platform_admin: bool = False


@dataclass(frozen=True)
class _SeedMember:
    email: str
    role_key: str


@dataclass(frozen=True)
class _SeedOrg:
    slug: str
    name: str
    members: tuple[_SeedMember, ...]


USERS = (
    _SeedUser("platform@shifde.local", "Platform Admin", True),
    _SeedUser("admin@demo-a.local", "Demo A Admin"),
    _SeedUser("viewer@demo-a.local", "Demo A Viewer"),
    _SeedUser("admin@demo-b.local", "Demo B Admin"),
)
ORGS = (
    _SeedOrg(
        "demo-a",
        "Demo Organization A",
        (
            _SeedMember("admin@demo-a.local", "tenant_administrator"),
            _SeedMember("viewer@demo-a.local", "viewer"),
        ),
    ),
    _SeedOrg(
        "demo-b",
        "Demo Organization B",
        (_SeedMember("admin@demo-b.local", "tenant_administrator"),),
    ),
)


# One staff person per organization, member of that organization's group only.
STAFF_USERS = (
    _SeedUser("staff@demo-a.local", "Demo A Staff"),
    _SeedUser("staff@demo-b.local", "Demo B Staff"),
)


def _staff_email(slug: str) -> str:
    return f"staff@{slug}.local"


def _seed_org_group(
    session: Session, *, tenant_id: UUID, slug: str, staff_id: UUID, now: datetime
) -> None:
    """A platform group `staff-<slug>` with the organization's staff person, bound at
    organization scope to the `viewer` role in that organization and in no other."""
    role_id = authz_repo.system_role_id(session, "viewer")
    if role_id is None:
        raise RuntimeError("the viewer role template is not seeded")
    group_id = uuid7()
    authz_repo.insert_group(
        session,
        {
            "id": group_id,
            "slug": f"staff-{slug}",
            "name": f"Staff of {slug}",
            "status": "active",
            "created_at": now,
        },
    )
    session.flush()
    authz_repo.add_group_members(session, group_id, [staff_id], "manual", now)
    authz_repo.insert_binding(
        session,
        {
            "id": uuid7(),
            "tenant_id": tenant_id,
            "subject_type": "group",
            "subject_id": group_id,
            "role_id": role_id,
            "scope_type": "tenant",
            "scope_id": tenant_id,
            "created_by": None,
            "created_at": now,
        },
    )


def _ensure_group(session: Session, slug: str, tenant_id: UUID, user_ids: dict[str, UUID]) -> None:
    if authz_repo.get_group_by_slug(session, f"staff-{slug}") is None:
        _seed_org_group(
            session,
            tenant_id=tenant_id,
            slug=slug,
            staff_id=user_ids[_staff_email(slug)],
            now=utcnow(),
        )


def seed_dev_data(
    session: Session,
    passwords: PasswordService,
    known: dict[str, str] | None = None,
    *,
    with_groups: bool = False,
) -> dict[str, str]:
    """Create/refresh demo orgs and users. Returns {email: password}: the one in `known` for that
    email, otherwise a freshly generated one. `with_groups` adds `STAFF_USERS` and one platform
    group per organization bound to that organization only."""
    credentials: dict[str, str] = {}
    user_ids: dict[str, UUID] = {}
    for u in (*USERS, *(STAFF_USERS if with_groups else ())):
        password = (known or {}).get(u.email) or secrets.token_urlsafe(18)
        existing = identity.find_user_id_by_email(session, u.email)
        if existing is None:
            user_ids[u.email] = identity.create_user(
                session,
                email=u.email,
                display_name=u.display_name,
                is_platform_admin=u.is_platform_admin,
                password=password,
                passwords=passwords,
            )
        else:
            identity.set_password(session, passwords, existing, password)
            user_ids[u.email] = existing
        credentials[u.email] = password
    session.flush()

    with db.platform_admin_context(session):
        _seed_orgs(session, user_ids, with_groups=with_groups)
    session.flush()
    return credentials


def _seed_orgs(session: Session, user_ids: dict[str, UUID], *, with_groups: bool) -> None:
    ctx = RequestContext(request_id="dev-seed")
    for org in ORGS:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == org.slug))
        if tenant is not None:
            if with_groups:
                _ensure_group(session, org.slug, tenant.id, user_ids)
            continue
        tenant_id = uuid7()
        now = utcnow()
        tenancy_repo.insert_tenant(
            session,
            {
                "id": tenant_id,
                "slug": org.slug,
                "name": org.name,
                "status": "active",
                "settings": {},
                "created_at": now,
            },
        )
        session.flush()
        for m in org.members:
            identity.add_member(session, tenant_id=tenant_id, user_id=user_ids[m.email])
            authz.grant_role(
                session,
                tenant_id=tenant_id,
                user_id=user_ids[m.email],
                role_key=m.role_key,
                scope_type="tenant",
                scope_id=tenant_id,
                created_by=None,
            )
        ws_id, project_id = uuid7(), uuid7()
        tenancy_repo.insert_workspace(
            session,
            {
                "id": ws_id,
                "tenant_id": tenant_id,
                "slug": "main",
                "name": "Main Workspace",
                "status": "active",
                "settings": {},
                "created_at": now,
            },
        )
        session.flush()
        tenancy_repo.insert_project(
            session,
            {
                "id": project_id,
                "tenant_id": tenant_id,
                "workspace_id": ws_id,
                "slug": "sample",
                "name": "Sample Project",
                "status": "active",
                "settings": {},
                "created_at": now,
            },
        )
        audit.record(
            session,
            ctx,
            action="tenancy.org.created",
            tenant_id=tenant_id,
            actor_label="dev-seed",
            target_type="org",
            target_id=tenant_id,
            target_label=f"org:{org.slug}",
            details={"seeded": True},
        )
        if with_groups:
            _ensure_group(session, org.slug, tenant_id, user_ids)
