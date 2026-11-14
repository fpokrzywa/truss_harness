"""Worker principal re-derivation (SECURITY_MODEL 3.3).

``execution_principal(session, run_ref)`` rebuilds the initiating principal from the database at
claim time and at every gate: current user/membership/service-account status, API-key revocation
and expiry, and the key masks. Jobs carry no permissions. The tenant context is set to the tenant
named by the run, so a run that names another tenant principal or key resolves nothing and is
denied. There is no ambient/system principal here: platform-admin power is never propagated.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, ResourceScope
from shifde.core.errors import Forbidden


class RunRef(Protocol):
    """What the (future) `runs` row must expose: who started it, in which tenant, with which key."""

    @property
    def tenant_id(self) -> UUID: ...
    @property
    def initiated_by_principal_id(self) -> UUID: ...
    @property
    def principal_type(self) -> str: ...
    @property
    def api_key_id(self) -> UUID | None: ...


class ExecutionDenied(Forbidden):
    code = "authz_denied"


def execution_principal(session: Session, run_ref: RunRef) -> Principal:
    tenant_id = run_ref.tenant_id
    pid = run_ref.initiated_by_principal_id
    db.set_tenant(session, tenant_id)
    db.set_principal(session, pid)
    if run_ref.principal_type == "service_account":
        sa = session.execute(
            text(
                "SELECT name, status = 'active' FROM shifde.service_accounts "
                "WHERE id = :p AND tenant_id = :t"
            ),
            {"p": pid, "t": tenant_id},
        ).first()
        if sa is None or not sa[1]:
            raise ExecutionDenied("run principal is not an active service account in this tenant")
        email, display = f"sa:{sa[0]}", str(sa[0])
    elif run_ref.principal_type == "user":
        row = session.execute(
            text(
                "SELECT u.primary_email, u.display_name, "
                "u.status = 'active' AND m.status = 'active' "
                "FROM shifde.users u JOIN shifde.memberships m "
                "ON m.user_id = u.id AND m.tenant_id = :t WHERE u.id = :p"
            ),
            {"p": pid, "t": tenant_id},
        ).first()
        if row is None or not row[2]:
            raise ExecutionDenied("run principal is not an active member of this tenant")
        email, display = str(row[0]), str(row[1])
    else:
        raise ExecutionDenied("unknown principal type")

    scopes: frozenset[str] | None = None
    mask: ResourceScope | None = None
    key_id = run_ref.api_key_id
    if key_id is not None:
        key = session.execute(
            text(
                "SELECT principal_type, principal_id, scopes, resource_scope, expires_at, "
                "revoked_at FROM shifde.api_keys WHERE id = :k AND tenant_id = :t"
            ),
            {"k": key_id, "t": tenant_id},
        ).first()
        if (
            key is None
            or key[0] != run_ref.principal_type
            or key[1] != pid
            or key[5] is not None
            or key[4] <= utcnow()
        ):
            raise ExecutionDenied("the API key that started this run is no longer valid")
        scopes = frozenset(key[2])
        rs = key[3]
        if rs is not None:
            mask = ResourceScope(
                frozenset(UUID(x) for x in rs.get("workspace_ids", [])),
                frozenset(UUID(x) for x in rs.get("project_ids", [])),
            )
    return Principal(
        user_id=pid,
        email=email,
        display_name=display,
        is_platform_admin=False,
        principal_type=run_ref.principal_type,
        credential="execution",
        tenant_id=tenant_id,
        api_key_id=key_id,
        key_scopes=scopes,
        resource_scope=mask,
    )
