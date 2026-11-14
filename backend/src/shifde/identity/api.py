"""Auth endpoints: /api/auth/* (config, local login, session, logout, setup-token redemption,
OpenID Connect sign-in).

Not mounted here: the `app` port includes `router` with `reject_basic_auth` and `check_origin`
and sets `app.state.oidc` (`identity.oidc.OidcClient`) and `app.state.oidc_limiter`. The OpenID
Connect routes (`/oidc/{idp_id}/login`, `/oidc/{idp_id}/callback`) are identity part 3a. Port
notes (part 2b, B-918): a failed login discards the
request's Session (identity review 2 item 2); limiter keys group an IPv6 address by its /64.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from shifde.core.deps import RequestCtxDep, SessionDep, SettingsDep
from shifde.core.errors import AppError
from shifde.core.features import FeatureRegistry
from shifde.core.ratelimit import SlidingWindowLimiter, ip_limit_applies, source_key
from shifde.core.settings import Settings
from shifde.identity import oidc, service
from shifde.identity.dependencies import PasswordsDep, SessionAuthDep, enforce_login_limits
from shifde.identity.schemas import (
    AuthConfigOut,
    LoginRequest,
    MembershipOut,
    OidcProviderOut,
    OrgRefOut,
    PendingLinkOut,
    RedeemRequest,
    SessionOut,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _features(request: Request) -> list[str]:
    registry: FeatureRegistry = request.app.state.features
    return registry.keys()


def _session_out(
    auth: service.AuthResult, memberships: list[service.MembershipRow], features: list[str]
) -> SessionOut:
    p = auth.principal
    return SessionOut(
        user=UserOut(
            id=p.user_id,
            email=p.email,
            display_name=p.display_name,
            is_platform_admin=p.is_platform_admin,
        ),
        csrf_token=auth.csrf_token,
        memberships=[
            MembershipOut(
                org=OrgRefOut(id=m.tenant_id, slug=m.slug, name=m.name, status=m.status),
                roles=m.role_names,
            )
            for m in memberships
            if p.tenant_id in (None, m.tenant_id)  # tenant-IdP sessions see only their tenant
        ],
        expires_at=auth.expires_at,
        features=features,
    )


def _set_cookie(response: Response, settings: Settings, token: str, max_age: int) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _discard(session: Session) -> None:
    """After a failed sign-in nothing of the attempt stays on the request's Session (review 2
    item 2): the service has cleared the person and client context; this ends the transaction
    and forgets every loaded row."""
    session.close()


@router.get("/config", response_model=AuthConfigOut)
def auth_config(settings: SettingsDep, session: SessionDep) -> AuthConfigOut:
    """Installation-wide active IdPs (tenant IdPs are not enumerable before login)."""
    providers = [
        OidcProviderOut(
            id=str(i.id),
            display_name=i.display_name,
            login_url=f"/api/auth/oidc/{i.id}/login",
        )
        for i in service.list_public_idps(session)
    ]
    return AuthConfigOut(local_enabled=settings.auth_local_enabled, oidc_providers=providers)


@router.post("/local/login", response_model=SessionOut)
def local_login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    passwords: PasswordsDep,
    ctx: RequestCtxDep,
) -> SessionOut:
    enforce_login_limits(request, settings, ctx, body.email)
    try:
        result = service.login_local(
            session,
            settings,
            passwords,
            ctx,
            email=body.email,
            password=body.password,
            previous_token=request.cookies.get(settings.session_cookie_name),
        )
    except AppError:
        _discard(session)
        raise
    memberships = service.my_memberships(session)
    _set_cookie(response, settings, result.session_token, result.max_age_seconds)
    return _session_out(result.auth, memberships, _features(request))


@router.get("/session", response_model=SessionOut)
def get_session_info(request: Request, auth: SessionAuthDep, session: SessionDep) -> SessionOut:
    return _session_out(auth, service.my_memberships(session), _features(request))


@router.post("/logout", status_code=204)
def logout(
    auth: SessionAuthDep,
    session: SessionDep,
    settings: SettingsDep,
    ctx: RequestCtxDep,
) -> Response:
    service.logout(session, ctx, auth)
    response = Response(status_code=204)
    _clear_cookie(response, settings)
    return response


def _client_limit(request: Request, settings: Settings, client: str | None) -> None:
    if ip_limit_applies(client, settings.trusted_proxies):
        limiter: SlidingWindowLimiter = request.app.state.login_ip_limiter
        limiter.check(source_key(client) or "unknown")


@router.post("/setup/redeem", response_model=SessionOut | PendingLinkOut)
def redeem_setup_token(
    body: RedeemRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    passwords: PasswordsDep,
    ctx: RequestCtxDep,
) -> SessionOut | PendingLinkOut:
    """Redeem a one-time setup token: set a password (local auth) or arm OIDC linking."""
    _client_limit(request, settings, ctx.client_ip)
    try:
        result = service.redeem_setup_token(
            session,
            settings,
            passwords,
            ctx,
            token=body.token,
            password=body.password,
            previous_token=request.cookies.get(settings.session_cookie_name),
        )
    except AppError:
        _discard(session)
        raise
    if result is None:
        return PendingLinkOut(status="pending_oidc_link")
    memberships = service.my_memberships(session)
    _set_cookie(response, settings, result.session_token, result.max_age_seconds)
    return _session_out(result.auth, memberships, _features(request))


# ---------------------------------------------------------------- OpenID Connect
def _oidc_client(request: Request) -> oidc.OidcClient:
    client: oidc.OidcClient = request.app.state.oidc
    return client


def _oidc_limit(request: Request, settings: Settings, client: str | None, step: str) -> None:
    """Findings Item 5b: sign-in starts and callbacks per client address (`source_key`), skipped
    for loopback and trusted proxies like the login limiter; a full window answers 429 before any
    database work (B-926)."""
    if ip_limit_applies(client, settings.trusted_proxies):
        limiter: SlidingWindowLimiter = request.app.state.oidc_limiter
        limiter.check(f"{step}:{source_key(client) or 'unknown'}")


def _failure(settings: Settings) -> RedirectResponse:
    response = RedirectResponse(oidc.FAILURE_PATH, status_code=302)
    response.delete_cookie(settings.oidc_cookie_name, path="/", secure=settings.cookie_secure)
    return response


@router.get("/oidc/{idp_id}/login")
def oidc_login(
    idp_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    ctx: RequestCtxDep,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> Response:
    """Start the authorization-code + PKCE flow: 302 to the IdP. An unknown, disabled or
    unsupported IdP answers the same generic redirect as any other failure."""
    _oidc_limit(request, settings, ctx.client_ip, "login")
    try:
        url, binding = oidc.begin_login(
            session, settings, _oidc_client(request), idp_id=UUID(idp_id), next_path=next_path
        )
    except (ValueError, oidc.OidcFailure):
        _discard(session)
        return _failure(settings)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        settings.oidc_cookie_name,
        binding,
        max_age=settings.oidc_state_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/oidc/{idp_id}/callback")
def oidc_callback(
    idp_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    ctx: RequestCtxDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Finish the flow; every failure is a generic redirect (detail only in audit/log)."""
    _oidc_limit(request, settings, ctx.client_ip, "callback")
    try:
        uid = UUID(idp_id)
    except ValueError:
        return _failure(settings)
    if error or not code or not state:
        oidc.record_failure(session, ctx, uid, None, "idp_error_or_missing_parameters")
        return _failure(settings)
    try:
        result = oidc.complete_login(
            session,
            settings,
            _oidc_client(request),
            ctx,
            idp_id=uid,
            code=code,
            state=state,
            binding=request.cookies.get(settings.oidc_cookie_name),
            previous_token=request.cookies.get(settings.session_cookie_name),
        )
    except oidc.OidcFailure:
        _discard(session)
        return _failure(settings)
    response = RedirectResponse(result.next_path, status_code=302)
    _set_cookie(response, settings, result.login.session_token, result.login.max_age_seconds)
    response.delete_cookie(settings.oidc_cookie_name, path="/", secure=settings.cookie_secure)
    return response
