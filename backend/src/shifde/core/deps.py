"""Shared FastAPI dependencies: settings, DB session, request context, Origin check."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from shifde.core.context import RequestContext
from shifde.core.db import Database, init_context
from shifde.core.errors import CsrfFailed, UnsupportedAuthScheme
from shifde.core.hooks import run_close_hooks
from shifde.core.ratelimit import resolve_client_ip
from shifde.core.settings import Settings

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_request_ctx(request: Request) -> RequestContext:
    request_id = getattr(request.state, "request_id", None) or "unknown"
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else None
    client = resolve_client_ip(
        peer,
        request.headers.get("x-forwarded-for"),
        settings.trusted_proxies,
        trust_loopback=settings.trust_loopback_proxy,
    )
    return RequestContext(
        request_id=request_id,
        client_ip=client,
        user_agent=request.headers.get("user-agent"),
        route=f"{request.method} {request.url.path}",
    )


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RequestCtxDep = Annotated[RequestContext, Depends(get_request_ctx)]


def get_session(request: Request, ctx: RequestCtxDep) -> Iterator[Session]:
    """One SQLAlchemy Session per request. Services own commit; anything uncommitted is rolled
    back on close. The request id is pre-loaded into the transaction context."""
    db: Database = request.app.state.db
    session = db.session()
    init_context(session, ctx.request_id)
    try:
        yield session
    finally:
        try:
            run_close_hooks(session)
        finally:
            session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def reject_basic_auth(request: Request) -> None:
    """HTTP Basic is never accepted anywhere (FR-AUTHN-007)."""
    header = request.headers.get("authorization", "")
    if header[:6].lower() == "basic ":
        raise UnsupportedAuthScheme("HTTP Basic authentication is not supported")


def check_origin(request: Request, settings: SettingsDep) -> None:
    """Browser CSRF defence in depth: a mutating request carrying an Origin must come from an
    allowed origin; a cross-site Fetch-Metadata signal is rejected when no Origin is present."""
    if request.method not in MUTATING_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.rstrip("/") not in settings.allowed_origins:
            raise CsrfFailed("origin not allowed")
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise CsrfFailed("cross-site request rejected")
