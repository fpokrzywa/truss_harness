"""FastAPI application factory.

SETH 3ba0e06 `app.py` with the changes recorded in DECISIONS B-2230..B-2236: the 11 routers that
exist on main (runtime's is APP-2, installation's public branding router is not ported, B-2221),
the OIDC routes (unmounted by B-2232, mounted by B-2263), the limiters and the body cap
carry_forward assigns to the app, no uvicorn `ProxyHeadersMiddleware` (the client address is
`resolve_client_ip`, B-201).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from shifde import __version__
from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.admin import api as admin_api
from shifde.build import api as build_api
from shifde.connections import api as connections_api
from shifde.core.bodylimit import MaxBodySizeMiddleware
from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.features import FeatureRegistry
from shifde.core.problem import PROBLEM_RESPONSES, install_error_handlers
from shifde.core.ratelimit import SlidingWindowLimiter
from shifde.core.settings import Settings, get_settings
from shifde.core.ssrf import OutboundHttp, Resolver, default_resolver
from shifde.governance import api as governance_api
from shifde.identity import api as auth_api
from shifde.identity.dependencies import require_auth
from shifde.identity.oidc import OidcClient
from shifde.identity.passwords import PasswordService
from shifde.installation import api as installation_api
from shifde.models_admin import api as models_api
from shifde.observability import api as health_api
from shifde.observability import tracing
from shifde.observability.logging import configure_logging
from shifde.observability.middleware import RequestIdMiddleware
from shifde.ontology import api as ontology_api
from shifde.tenancy import api as tenancy_api
from shifde.vendors import api as vendors_api


def create_app(
    settings: Settings | None = None,
    *,
    http_transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    egress_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Build the app. `http_transport`/`resolver` let tests run IdP flows in-process; `resolver`
    is also the DNS resolver of the connection EgressGuard."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    tracing.configure_from_settings(settings, "shifde-api")
    db = Database(
        settings.database_url,
        connect_timeout=settings.db_connect_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        db.dispose()

    app = FastAPI(
        title="SHIFDE API",
        version=__version__,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
        dependencies=[Depends(reject_basic_auth), Depends(check_origin)],
        responses=PROBLEM_RESPONSES,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.login_ip_limiter = SlidingWindowLimiter(settings.login_rate_per_minute)
    app.state.login_account_limiter = SlidingWindowLimiter(
        settings.login_rate_per_account_per_minute
    )
    app.state.api_key_failure_limiter = SlidingWindowLimiter(
        settings.api_key_failures_per_minute, message="too many failed API key attempts"
    )
    app.state.oidc_limiter = SlidingWindowLimiter(
        settings.oidc_rate_per_minute, message="too many sign-in attempts; try again later"
    )
    app.state.passwords = PasswordService(fast=settings.env == "test")
    app.state.oidc = OidcClient(
        settings,
        OutboundHttp(
            allow_dev_loopback=settings.is_development,
            resolver=resolver or default_resolver,
            transport=http_transport,
            timeout=settings.oidc_http_timeout_seconds,
        ),
    )
    app.state.integration = IntegrationRuntime(
        settings=settings,
        registry=AdapterRegistry.load(),
        resolver=resolver or default_resolver,
        inner=egress_transport,
    )
    install_error_handlers(app)
    # innermost first: the cap answers inside CORS and the request id, so a 413 carries both
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)  # outermost: every response gets X-Request-ID

    features = FeatureRegistry()
    app.state.features = features
    app.include_router(health_api.router)

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        """Liveness without database access (kept from the skeleton: the startup-role test and
        process supervisors use it; B-2231)."""
        return {"status": "ok"}

    # every `/api/auth` route, the OpenID Connect sign-in included (B-2263; was B-2232)
    app.include_router(auth_api.router)
    # Deny-by-default: every /api/v1 route requires an authenticated session.
    guard = [Depends(require_auth)]
    app.include_router(tenancy_api.router, dependencies=guard)
    features.register("tenancy")
    app.include_router(installation_api.router, dependencies=guard)
    app.include_router(admin_api.router, dependencies=guard)
    features.register("security")
    for router in (vendors_api.router, connections_api.router, models_api.router):
        app.include_router(router, dependencies=guard)
    features.register("integrate")
    app.include_router(build_api.router, dependencies=guard)
    features.register("build")
    # `operate` and `pipelines` are registered with runtime's router (APP-2), not before
    app.include_router(ontology_api.router, dependencies=guard)
    features.register("ontology")
    app.include_router(governance_api.router, dependencies=guard)
    features.register("governance")
    return app
