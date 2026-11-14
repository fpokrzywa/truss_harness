"""Request-scoped tenancy context handed to services (no web-framework types here)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.authz.service import PDP, ResourceRef
from shifde.core.context import Principal, RequestContext


@dataclass
class TenantScope:
    """A caller's resolved organization: membership verified, `app.tenant_id` set for the
    transaction, and a PDP bound to (principal, tenant)."""

    session: Session
    ctx: RequestContext
    principal: Principal
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    tenant_status: str
    pdp: PDP

    @property
    def tenant_ref(self) -> ResourceRef:
        return ResourceRef(tenant_id=self.tenant_id)
