"""A minimal FastAPI app for the identity routes (identity part 2b, B-920).

The `app` port mounts `identity.api.router`; until then these tests build the app from the router
and the dependencies, connected as `shifde_app`, with `reject_basic_auth` and `check_origin` as
the app factory will add them (SETH 3ba0e06 `app.py`). The probe routes stand in for the admin and
client routes that will sit behind `require_auth`: they read and change identity rows of the
`{org}` in the path through the repository, relying only on the context the dependencies set.

`service.my_memberships` is the real definer `shifde_my_memberships` (tenancy_0002): the
B-920 stub is gone (tenancy review 1 item 2, DECISIONS B-1009).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON response bodies of the probe routes
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.deps import SessionDep, check_origin, reject_basic_auth
from shifde.core.egress import Resolver, default_resolver
from shifde.core.errors import NotFound
from shifde.core.features import FeatureRegistry
from shifde.core.ids import uuid7
from shifde.core.problem import install_error_handlers
from shifde.core.ratelimit import SlidingWindowLimiter
from shifde.core.settings import Settings
from shifde.core.ssrf import OutboundHttp
from shifde.identity import api, repository
from shifde.identity.dependencies import AuthDep, require_auth
from shifde.identity.oidc import OidcClient
from shifde.identity.passwords import PasswordService

ORIGIN = "http://127.0.0.1:3710"
PASSWORD = "correct horse battery staple"
COOKIE = "__Host-shifde_session"

probe = APIRouter(prefix="/api/v1/probe", tags=["probe"])


@probe.get("/whoami")
def whoami(auth: AuthDep, session: SessionDep) -> dict[str, Any]:
    p = auth.principal
    current = session.execute(text("SELECT shifde.current_tenant_id()")).scalar_one()
    return {
        "principal_id": str(p.principal_id),
        "principal_type": p.principal_type,
        "credential": p.credential,
        "tenant_id": str(p.tenant_id) if p.tenant_id else None,
        "db_tenant_id": str(current) if current else None,
    }


@probe.get("/orgs/{org}/service-accounts/{sa_id}")
def get_service_account(
    org: UUID, sa_id: UUID, auth: AuthDep, session: SessionDep
) -> dict[str, str]:
    sa = repository.get_service_account(session, org, sa_id)
    if sa is None:
        raise NotFound("service account not found")
    return {"id": str(sa.id), "name": sa.name}


@probe.get("/orgs/{org}/api-keys")
def list_api_keys(org: UUID, auth: AuthDep, session: SessionDep) -> list[str]:
    rows = repository.list_api_keys(
        session, tenant_id=org, principal_type=None, principal_id=None, after=None, limit=100
    )
    return [str(r.id) for r in rows]


@probe.delete("/orgs/{org}/api-keys/{key_id}", status_code=204)
def revoke_api_key(org: UUID, key_id: UUID, auth: AuthDep, session: SessionDep) -> None:
    # the service rule (carry_forward): load the row in the caller's client before the mutator
    if repository.get_api_key(session, org, key_id) is None:
        raise NotFound("API key not found")
    repository.revoke_api_key(session, key_id, auth.principal.principal_id, utcnow(), tenant_id=org)
    session.commit()


def make_app(
    settings: Settings,
    *,
    http_transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> FastAPI:
    app = FastAPI(openapi_url="/api/openapi.json")
    install_error_handlers(app)
    db = Database(settings.database_url)
    app.state.settings = settings
    app.state.db = db
    app.state.passwords = PasswordService(fast=True)
    app.state.login_ip_limiter = SlidingWindowLimiter(settings.login_rate_per_minute)
    app.state.login_account_limiter = SlidingWindowLimiter(
        settings.login_rate_per_account_per_minute
    )
    app.state.api_key_failure_limiter = SlidingWindowLimiter(
        settings.api_key_failures_per_minute, message="too many failed API key attempts"
    )
    app.state.executor_auth_failure_limiter = SlidingWindowLimiter(
        settings.executor_auth_failures_per_minute,
        message="too many failed executor authentications",
    )
    app.state.features = FeatureRegistry()
    # SETH 3ba0e06 app.py: the OIDC client over the SSRF-guarded HTTP client (a test passes the
    # fake IdP's transport, `tests/_support/fake_oidc.py`)
    app.state.oidc = OidcClient(
        settings,
        OutboundHttp(
            allow_dev_loopback=settings.is_development,
            resolver=resolver or default_resolver,
            transport=http_transport,
            timeout=settings.oidc_http_timeout_seconds,
        ),
    )
    app.state.oidc_limiter = SlidingWindowLimiter(
        settings.oidc_rate_per_minute, message="too many sign-in attempts; try again later"
    )
    guards = [Depends(reject_basic_auth), Depends(check_origin)]
    app.include_router(api.router, dependencies=guards)
    app.include_router(probe, dependencies=[*guards, Depends(require_auth)])
    return app


@contextmanager
def client_for(
    settings: Settings,
    *,
    http_transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    **kwargs: Any,  # Any: TestClient options
) -> Iterator[TestClient]:
    app = make_app(settings, http_transport=http_transport, resolver=resolver)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def person(owner: Session, email: str, *, admin: bool = False) -> UUID:
    """A person with a local password (fast hash), committed."""
    uid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name, is_platform_admin) "
            "VALUES (:id, :e, :e, :a)"
        ),
        {"id": uid, "e": email, "a": admin},
    )
    owner.execute(
        text("INSERT INTO shifde.local_credentials (user_id, password_hash) VALUES (:u, :h)"),
        {"u": uid, "h": PasswordService(fast=True).hash(PASSWORD)},
    )
    owner.commit()
    return uid


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/auth/local/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}
