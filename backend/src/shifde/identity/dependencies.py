"""FastAPI dependencies that authenticate the session cookie or a bearer API key.

The app factory (the `app` port) puts these on `app.state`: `passwords` (PasswordService),
`login_ip_limiter`, `login_account_limiter` and `api_key_failure_limiter` (core
`SlidingWindowLimiter`s). Port notes (identity part 2b, B-918, B-919): limiter keys group an IPv6
address by its /64; the per-account login limit is keyed per (e-mail, source) so another address
cannot use it to lock a person out; failed bearer attempts are limited per source.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.authz.pdp import RECORDS_KEY
from shifde.core import db
from shifde.core.context import RequestContext, bind_log_context
from shifde.core.deps import RequestCtxDep, SessionDep, SettingsDep
from shifde.core.errors import (
    CredentialExpired,
    CredentialRevoked,
    InvalidCredentials,
    RateLimited,
    Unauthenticated,
)
from shifde.core.hooks import add_close_hook
from shifde.core.ratelimit import SlidingWindowLimiter, ip_limit_applies, source_key
from shifde.core.settings import Settings
from shifde.identity import apikeys, service
from shifde.identity.passwords import PasswordService

API_KEY_LIMIT_MESSAGE = "too many failed API key attempts; try again later"


def get_passwords(request: Request) -> PasswordService:
    passwords: PasswordService = request.app.state.passwords
    return passwords


PasswordsDep = Annotated[PasswordService, Depends(get_passwords)]


def enforce_login_limits(
    request: Request, settings: Settings, ctx: RequestContext, email: str
) -> None:
    """Per-IP (remote clients only) and per-account login throttles -> 429 rate_limited.

    The per-account limit counts per (normalised e-mail, source): failures sent from one address
    cannot throttle the person's own attempts from another (carry_forward victim-lockout row,
    B-919). The account-wide bound over every source is the database ceiling (B-918)."""
    source = source_key(ctx.client_ip)
    if ip_limit_applies(ctx.client_ip, settings.trusted_proxies, forwarded=ctx.forwarded):
        ip_limiter: SlidingWindowLimiter = request.app.state.login_ip_limiter
        ip_limiter.check(source or "unknown")
    account_limiter: SlidingWindowLimiter = request.app.state.login_account_limiter
    account_limiter.check(f"{service.normalize_email(email)}|{source}")


def _authenticate_bearer(
    request: Request,
    session: Session,
    settings: Settings,
    ctx: RequestContext,
    authorization: str,
) -> service.AuthResult:
    """Failed bearer attempts are limited per source (findings Item 5b). A source over its limit
    is refused before any lookup, so a correct key cannot be told apart by the answer."""
    limiter: SlidingWindowLimiter = request.app.state.api_key_failure_limiter
    applies = ip_limit_applies(ctx.client_ip, settings.trusted_proxies, forwarded=ctx.forwarded)
    key = source_key(ctx.client_ip) or "unknown"
    if applies and limiter.exceeded(key):
        raise RateLimited(API_KEY_LIMIT_MESSAGE)
    try:
        return apikeys.authenticate_bearer(session, settings, ctx, authorization=authorization)
    except (InvalidCredentials, CredentialRevoked, CredentialExpired):
        if applies:
            with suppress(RateLimited):  # this attempt has already failed with its own error
                limiter.check(key)
        raise


def require_auth(
    request: Request, session: SessionDep, settings: SettingsDep, ctx: RequestCtxDep
) -> service.AuthResult:
    """Authenticate the request: `Authorization: Bearer shf_...` (API key; no cookies are read
    and no CSRF applies) or the session cookie (401 if absent/expired/revoked, 403 on bad CSRF)."""
    authorization = request.headers.get("authorization")
    if authorization is not None:
        result = _authenticate_bearer(request, session, settings, ctx, authorization)
    else:
        result = service.authenticate(
            session,
            settings,
            ctx,
            token=request.cookies.get(settings.session_cookie_name),
            method=request.method,
            csrf_header=request.headers.get("x-csrf-token"),
        )
    bind_log_context(principal_id=str(result.principal.principal_id))
    register_denial_audit(session, ctx)
    return result


def require_session_auth(
    request: Request, session: SessionDep, settings: SettingsDep, ctx: RequestCtxDep
) -> service.AuthResult:
    """Cookie-session only (for /api/auth/session and /logout): API keys have no session."""
    if request.headers.get("authorization") is not None:
        raise Unauthenticated("a browser session is required")
    return require_auth(request, session, settings, ctx)


def register_denial_audit(session: Session, ctx: RequestContext) -> None:
    """Persist authorization denials to the audit log when the request ends (the request
    transaction has usually been rolled back by then; policy_decisions is written by the PDP)."""

    def hook(s: Session) -> None:
        records = [r for r in s.info.get(RECORDS_KEY, []) if r.decision == "deny"]
        for rec in records:
            db.set_tenant(s, rec.tenant_id)
            audit.record(
                s,
                ctx,
                action="authz.denied",
                outcome="denied",
                tenant_id=rec.tenant_id,
                target_type=rec.resource_type or "permission",
                target_id=rec.resource_id,
                details={"permission": rec.action, "reason": rec.reason, "method_path": ctx.route},
            )
        if records:
            s.commit()

    add_close_hook(session, hook)


AuthDep = Annotated[service.AuthResult, Depends(require_auth)]
SessionAuthDep = Annotated[service.AuthResult, Depends(require_session_auth)]
