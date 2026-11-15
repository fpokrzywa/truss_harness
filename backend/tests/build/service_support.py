"""The production `ArtifactService` and its `Env` for the build tests (part 4a).

`service_for` builds the service of one person in one client on a session the test opened (as
`shifde_app` under row-level security, or as the schema owner, where only the client id the
service passes confines it). `service_env` is the service's own `Env` for a holder artifact of that
client: the stand-in `RepoEnv` and this one answer the same cross-client questions (review 2 F2).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.authz.service import new_pdp
from shifde.build.env import Env
from shifde.build.service import ArtifactService
from shifde.core.context import Principal, RequestContext
from shifde.core.db import set_principal, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.tenancy.scope import TenantScope
from tests._support.tenants import bind_user
from tests.tenancy.conftest import owner_member, owner_person

REGISTRY = AdapterRegistry.load()


def add_person(owner: Session, tenant: UUID, label: str, role_key: str | None) -> UUID:
    """A member of `tenant` with `role_key` bound at organization scope (none if None)."""
    user = owner_person(owner, label)
    owner_member(owner, tenant, user)
    if role_key is not None:
        bind_user(owner, tenant, user, role_key)
    return user


def bind_at(owner: Session, tenant: UUID, user: UUID, role_key: str, scope: str, sid: UUID) -> None:
    """A binding of a system role template at a workspace or project of `tenant`."""
    owner.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) SELECT :i, :t, 'user', :u, r.id, :st, :s "
            "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = :k"
        ),
        {"i": uuid7(), "t": tenant, "u": user, "k": role_key, "st": scope, "s": sid},
    )


def service_for(
    session: Session,
    tenant: UUID,
    user: UUID,
    settings: Settings,
    *,
    status: str = "active",
    set_context: bool = True,
) -> ArtifactService:
    """`user`'s `ArtifactService` in `tenant`, on `session`. With `set_context` the session's
    client and principal are set for the pair (as a request's are); without, the session keeps
    the context the test gave it (a test of what a mismatched context reaches)."""
    if set_context:
        set_tenant(session, tenant)
        set_principal(session, user)
    principal = Principal(user_id=user, email=f"{user.hex}@shi.test", display_name="tester")
    scope = TenantScope(
        session=session,
        ctx=RequestContext(request_id="r-build", principal=principal, tenant_id=tenant),
        principal=principal,
        tenant_id=tenant,
        tenant_slug=f"t-{tenant.hex[-12:]}",
        tenant_name="tester",
        tenant_status=status,
        pdp=new_pdp(session, principal, tenant),
    )
    return ArtifactService(scope, IntegrationRuntime(settings=settings, registry=REGISTRY))


def holder(tenant: UUID, kind: str = "agents", **over: Any) -> dict[str, Any]:
    """The artifact a validation runs for (organization scope unless overridden)."""
    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "kind": kind,
        "workspace_id": None,
        "project_id": None,
        "key": "holder",
        **over,
    }


def service_env(session: Session, tenant: UUID, user: UUID, settings: Settings) -> Env:
    """The service's `Env` for `user` in `tenant`; the session's own context is left as is."""
    return service_for(session, tenant, user, settings, set_context=False).env(holder(tenant))
