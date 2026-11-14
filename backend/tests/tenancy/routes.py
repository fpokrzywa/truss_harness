"""A minimal app for the tenancy routes (part 2b), as identity part 2b did (B-920).

The `app` port mounts `tenancy.api.router` behind `require_auth` (B-501); until then the tests
build the identity test app (`tests/identity/harness.py`, connected as `shifde_app`) and add the
tenancy router with the app-level guards and authentication. `probe_scoped` stands in for a
client route of another module behind `resolve_org`: it lists the API keys of the client the
dependency resolved, relying only on the context it set (identity review 3 items 2-3).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any  # Any: TestClient options
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.egress import Resolver
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import repository as identity_repository
from shifde.identity import service as identity_service
from shifde.identity.dependencies import require_auth
from shifde.tenancy import api
from shifde.tenancy.dependencies import ScopeDep
from tests._support.tenants import bind_user
from tests.identity.harness import ORIGIN, make_app, person
from tests.tenancy.conftest import Client, World, owner_member

probe_scoped = APIRouter(prefix="/api/v1/probe-scoped", tags=["probe"])


@probe_scoped.get("/orgs/{org}/api-keys")
def list_api_keys(scope: ScopeDep) -> list[str]:
    rows = identity_repository.list_api_keys(
        scope.session,
        tenant_id=scope.tenant_id,
        principal_type=None,
        principal_id=None,
        after=None,
        limit=100,
    )
    return [str(r.id) for r in rows]


@contextmanager
def tenancy_client(
    settings: Settings,
    *,
    http_transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    **kwargs: Any,
) -> Iterator[TestClient]:
    app = make_app(settings, http_transport=http_transport, resolver=resolver)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    app.include_router(probe_scoped, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


@dataclass(frozen=True)
class People:
    """E-mails (password `PASSWORD`) of the route tests' people."""

    owner_a: str  # tenant_administrator of A (A's owner)
    viewer_a: str
    owner_b: str
    both: str  # viewer in A and in B
    both_id: UUID
    platform: str  # the platform administrator, in no client


def ws_slug(c: Client) -> str:
    return f"w-{c.ws.hex}"


def ws2_slug(c: Client) -> str:
    return f"w-{c.ws2.hex}"


def project_slug(c: Client) -> str:
    return f"p-{c.project.hex}"


def make_people(owner: Session, world: World) -> People:
    tag = uuid7().hex[-12:]

    def member(label: str, client: Client, role: str) -> tuple[str, UUID]:
        email = f"{label}-{tag}@route.test"
        uid = person(owner, email)
        owner_member(owner, client.tenant, uid)
        bind_user(owner, client.tenant, uid, role)
        owner.commit()
        return email, uid

    owner_a, _ = member("owner-a", world.a, "tenant_administrator")
    viewer_a, _ = member("viewer-a", world.a, "viewer")
    owner_b, _ = member("owner-b", world.b, "tenant_administrator")
    both, both_id = member("both", world.a, "viewer")
    owner_member(owner, world.b.tenant, both_id)
    bind_user(owner, world.b.tenant, both_id, "viewer")
    owner.commit()
    platform = f"platform-{tag}@route.test"
    person(owner, platform, admin=True)
    return People(owner_a, viewer_a, owner_b, both, both_id, platform)


def pinned_session(owner: Session, user: UUID, tenant: UUID | None) -> tuple[str, str]:
    """A session of `user` pinned to `tenant` (none if None): (cookie token, CSRF token)."""
    token = f"pin-{uuid7().hex}{uuid7().hex}"
    csrf = identity_service.derive_csrf_token(token)
    now = utcnow()
    owner.execute(
        text(
            "INSERT INTO shifde.sessions (id, session_hash, user_id, active_tenant_id, csrf_hash, "
            "idle_expires_at, absolute_expires_at) VALUES (:id, :h, :u, :t, :c, :i, :a)"
        ),
        {
            "id": uuid7(),
            "h": identity_service.hash_token(token),
            "u": user,
            "t": tenant,
            "c": identity_service.hash_token(csrf),
            "i": now + timedelta(hours=1),
            "a": now + timedelta(hours=8),
        },
    )
    owner.commit()
    return token, csrf
