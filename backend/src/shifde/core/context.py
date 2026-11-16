"""Request context and principal types shared across modules."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ResourceScope:
    """API-key restriction to specific workspaces / projects (empty sets = nothing allowed)."""

    workspace_ids: frozenset[UUID] = frozenset()
    project_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True)
class Principal:
    """An authenticated, active principal: a user or a service account.

    ``user_id`` is the principal's id whatever its type (kept for P1 compatibility; see
    ``principal_id``). API-key and worker principals pin ``tenant_id`` and may carry a permission
    mask (``key_scopes``) and a resource mask (``resource_scope``): effective permission is the
    principal's grants intersected with both masks (SECURITY_MODEL 2.4)."""

    user_id: UUID
    email: str
    display_name: str
    is_platform_admin: bool = False
    principal_type: str = "user"  # user | service_account
    credential: str = "session"  # session | api_key | execution
    tenant_id: UUID | None = None
    api_key_id: UUID | None = None
    key_scopes: frozenset[str] | None = None
    resource_scope: ResourceScope | None = None

    @property
    def principal_id(self) -> UUID:
        return self.user_id


@dataclass
class RequestContext:
    request_id: str
    client_ip: str | None = None
    user_agent: str | None = None
    principal: Principal | None = None
    tenant_id: UUID | None = None
    session_id: UUID | None = None
    route: str | None = None  # `METHOD /path` of the request (audit context)
    # True when the request carried an `X-Forwarded-For` header, believed or not: the per-IP limit
    # then never treats the resolved address as an exempt loopback / proxy peer (review 2 item 1)
    forwarded: bool = False


# Mutable per-request dict shared by reference so values bound inside threadpool-run
# dependencies (which execute in a *copy* of the context) still reach the log processor.
_log_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "shifde_log_context", default=None
)


def init_log_context(request_id: str) -> contextvars.Token[dict[str, Any] | None]:
    return _log_context.set({"request_id": request_id})


def reset_log_context(token: contextvars.Token[dict[str, Any] | None]) -> None:
    _log_context.reset(token)


def bind_log_context(**values: str | None) -> None:
    current = _log_context.get()
    if current is not None:
        current.update({k: v for k, v in values.items() if v is not None})


def current_log_context() -> dict[str, Any]:  # Any: heterogeneous loggable scalars
    return dict(_log_context.get() or {})
