"""Identity use cases: local login, server-side sessions, logout, memberships, bootstrap helpers.

Sessions (SECURITY_MODEL 2.2): the cookie carries a 256-bit random token; only its SHA-256
is stored.
The CSRF token is derived from the cookie token (so `GET /auth/session` can re-deliver it without
storing it); the server stores only its hash and compares in constant time.

Port notes (identity part 2a, DECISIONS B-909..B-914): people are platform-level (design 5.3).
A membership, a new person and an invitation are created only under the platform-administrator
context (identity review 1 item 1); a session pinned to a client never carries platform
administrator power and lapses with the membership (review 1 item 4); failed local logins are
counted per (person, source address) (SEC Minor-1); refusals that could reveal another client's
rows use one generic error (review 1 item 3).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any  # Any: column value dicts for pass-through inserts
from uuid import UUID

from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import (
    AuthDisabled,
    Conflict,
    CsrfFailed,
    Forbidden,
    InvalidCredentials,
    Unauthenticated,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.ratelimit import source_key
from shifde.core.settings import Settings
from shifde.identity import repository
from shifde.identity.models import ApiKey, IdpConfig, Membership, ServiceAccount
from shifde.identity.passwords import MIN_PASSWORD_LENGTH, PasswordService
from shifde.identity.repository import MemberRow, MembershipRow

__all__ = [
    "ApiKey",
    "AuthResult",
    "IdpConfig",
    "ServiceAccount",
    "LINK_WINDOW_SECONDS",
    "LoginResult",
    "MemberRow",
    "MembershipRow",
    "add_member",
    "authenticate",
    "count_members",
    "create_setup_token",
    "create_user",
    "derive_csrf_token",
    "find_user_id_by_email",
    "list_members",
    "login_local",
    "logout",
    "my_memberships",
    "normalize_email",
    "platform_admin_exists",
    "set_password",
]

LOCKOUT_THRESHOLD = 10
LOCKOUT_BASE = timedelta(minutes=15)
LOCKOUT_MAX = timedelta(hours=24)
TOUCH_INTERVAL = timedelta(minutes=1)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SETUP_TOKEN_TTL = timedelta(hours=1)
# Account-wide ceiling over every source (identity review 2 item 3, B-918): rotating addresses
# no longer gives unlimited guesses. Each source counts at most LOCKOUT_THRESHOLD towards it
# (isolation review 3 item 1), so reaching it takes ten distinct sources (IPv4 addresses or IPv6
# /64s) each locked by its own threshold within the window, not one address hammering.
ACCOUNT_CEILING = 100
ACCOUNT_WINDOW = timedelta(hours=1)
# The subject of an unknown account's lookups: no person has this id, so the same statements run
# and find nothing (identity review 2 item 4, B-918).
_NOBODY = UUID(int=0)


def _source_hash(client_ip: str | None) -> bytes:
    """The lockout key's source part (B-911): the resolved client address, hashed; an IPv6
    address counts as its /64 (identity review 2 item 3, B-918)."""
    return hashlib.sha256(b"shifde-login-source-v1:" + source_key(client_ip).encode()).digest()


def _require_platform_admin(session: Session, what: str) -> None:
    if not db.get_context(session).platform_admin:
        raise Forbidden(f"{what} is a platform administrator action")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def derive_csrf_token(session_token: str) -> str:
    digest = hashlib.sha256(b"shifde-csrf-v1:" + session_token.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True)
class AuthResult:
    principal: Principal
    session_id: UUID
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class LoginResult:
    auth: AuthResult
    session_token: str
    max_age_seconds: int


def my_memberships(session: Session) -> list[MembershipRow]:
    """The caller's own active memberships (requires the principal context to be set).

    Calls SETH's `shifde_my_memberships`, which reads `tenants`: it fails closed (function does
    not exist) until tenancy creates it (carry_forward)."""
    return repository.my_memberships(session)


def _audit_for_user(
    session: Session,
    ctx: RequestContext,
    *,
    user_id: UUID,
    email: str,
    action: str,
    outcome: str,
    details: Mapping[str, str],
    auth_method: str = "local",
    pinned_tenant_id: UUID | None = None,
) -> None:
    """One audit row per tenant the user belongs to (so each org's activity feed sees it);
    a single installation-level row (tenant_id NULL, written with no client context) if the user
    has no memberships. A session pinned to a client (a client's IdP) is that client's activity
    only: its row goes to that client and no other client learns of it (B-927)."""
    db.set_principal(session, user_id)
    tenant_ids = repository.my_membership_tenants(session)
    if pinned_tenant_id is not None:
        tenant_ids = [t for t in tenant_ids if t == pinned_tenant_id]
    targets: list[UUID | None] = list(tenant_ids) or [None]
    for tenant_id in targets:
        db.set_tenant(session, tenant_id)
        audit.record(
            session,
            ctx,
            action=action,
            outcome=outcome,
            tenant_id=tenant_id,
            actor_id=user_id,
            actor_label=email,
            auth_method=auth_method,
            target_type="user",
            target_id=user_id,
            target_label=f"user:{email}",
            details=details,
        )
    db.set_tenant(session, ctx.tenant_id)


def login_local(
    session: Session,
    settings: Settings,
    passwords: PasswordService,
    ctx: RequestContext,
    *,
    email: str,
    password: str,
    previous_token: str | None,
) -> LoginResult:
    """Verify credentials and open a new session (rotating any presented session).

    On any failure the person and client context are cleared before the error leaves (identity
    review 2 item 2): the submitted e-mail's person must not stay the principal of the Session."""
    if not settings.auth_local_enabled:
        raise AuthDisabled("local authentication is disabled")
    try:
        return _login_local(
            session,
            settings,
            passwords,
            ctx,
            email=email,
            password=password,
            previous_token=previous_token,
        )
    except BaseException:
        _clear_context(session)
        raise


def _clear_context(session: Session) -> None:
    """Drop the principal and client context. A refused statement still lowers the Session's
    context (B-703), so each is attempted on its own and its error does not hide the original."""
    with suppress(SQLAlchemyError):
        db.set_principal(session, None)
    with suppress(SQLAlchemyError):
        db.set_tenant(session, None)


def _login_local(
    session: Session,
    settings: Settings,
    passwords: PasswordService,
    ctx: RequestContext,
    *,
    email: str,
    password: str,
    previous_token: str | None,
) -> LoginResult:
    normalized = normalize_email(email)
    source = _source_hash(ctx.client_ip)
    row = repository.resolve_local_login(session, normalized)
    # Serialize attempts per account: take the credential row lock *before* verifying, and decide
    # `locked` from the locked rows, so parallel attempts queue up and nothing (not even a correct
    # password) is checked once the threshold has been reached. An unknown account runs the same
    # statements against an id no person has (review 2 item 4).
    subject = row.user_id if row is not None else _NOBODY
    db.set_principal(session, subject)
    creds = repository.lock_credentials(session, subject)
    # SEC Minor-1 (B-911): failures are counted per (person, source address), so failures from
    # one source do not lock the person out from another; the account-wide ceiling (B-918)
    # bounds the sum over all sources. The account-wide columns are no longer raised by login
    # (an operator may still set them; they are honoured).
    source_lock = repository.lock_login_source(session, subject, source)
    now = utcnow()  # after any lock wait
    recent = repository.account_recent_failures(
        session, subject, now - ACCOUNT_WINDOW, per_source_cap=LOCKOUT_THRESHOLD
    )
    if row is not None and creds is not None:
        row = replace(
            row,
            password_hash=creds.password_hash,
            failed_attempts=creds.failed_attempts,
            locked_until=creds.locked_until,
        )
    source_locked_until = source_lock.locked_until if source_lock else None
    locked = row is not None and (
        recent >= ACCOUNT_CEILING
        or any(
            until is not None and until > now for until in (row.locked_until, source_locked_until)
        )
    )
    active = row is not None and row.user_status == "active"
    # Always run a verify (against a dummy hash when unknown) to equalise timing. When the
    # account is locked the supplied password is not checked against the real hash.
    password_ok = passwords.verify(row.password_hash if row and not locked else None, password)

    if row is None or not password_ok or locked or not active:
        if row is not None:
            repository.record_source_failure(
                session,
                row.user_id,
                source,
                now=now,
                threshold=LOCKOUT_THRESHOLD,
                base_seconds=int(LOCKOUT_BASE.total_seconds()),
                max_seconds=int(LOCKOUT_MAX.total_seconds()),
            )
            reason = "locked" if locked else "disabled" if not active else "bad_password"
            _audit_for_user(
                session,
                ctx,
                user_id=row.user_id,
                email=normalized,
                action="identity.login.failed",
                outcome="failure",
                details={"reason": reason},
            )
        else:
            # the counterparts of the known path's counter write and membership lookup
            repository.reset_login_source(session, subject, source, now)
            repository.my_membership_tenants(session)
            db.set_principal(session, None)
            db.set_tenant(session, None)  # a pre-tenant event is filed with no client context
            audit.record(
                session,
                ctx,
                action="identity.login.failed",
                outcome="failure",
                tenant_id=None,
                auth_method="local",
                details={"reason": "unknown_account"},
            )
            db.set_tenant(session, ctx.tenant_id)  # as `_audit_for_user` restores the caller's
        session.commit()  # persist the counter and audit row; the caller then raises 401
        raise InvalidCredentials("invalid email or password")

    repository.reset_login_source(session, row.user_id, source, now)
    return establish_session(
        session,
        settings,
        ctx,
        user_id=row.user_id,
        previous_token=previous_token,
        auth_method="local",
        reset_lockout=True,
        now=now,
    )


def establish_session(
    session: Session,
    settings: Settings,
    ctx: RequestContext,
    *,
    user_id: UUID,
    previous_token: str | None,
    auth_method: str,
    reset_lockout: bool = False,
    now: datetime | None = None,
    active_tenant_id: UUID | None = None,
) -> LoginResult:
    """Open a server-side session for an already-authenticated user (local password, OIDC or
    setup-token redemption), rotating any presented session, and audit the login."""
    now = now or utcnow()
    db.set_principal(session, user_id)
    if reset_lockout:
        repository.reset_login_state(session, user_id)
    repository.touch_last_login(session, user_id, now)
    if previous_token:
        repository.revoke_session_by_hash(session, hash_token(previous_token))  # rotation

    token = secrets.token_urlsafe(32)
    csrf = derive_csrf_token(token)
    idle_expires = now + timedelta(minutes=settings.session_idle_minutes)
    absolute_expires = now + timedelta(hours=settings.session_absolute_hours)
    session_id = uuid7()
    repository.insert_session(
        session,
        {
            "id": session_id,
            "session_hash": hash_token(token),
            "user_id": user_id,
            "active_tenant_id": active_tenant_id,
            "csrf_hash": hash_token(csrf),
            "created_at": now,
            "last_seen_at": now,
            "idle_expires_at": idle_expires,
            "absolute_expires_at": absolute_expires,
            "ip": ctx.client_ip,
            "user_agent_hash": hash_token(ctx.user_agent) if ctx.user_agent else None,
        },
    )
    user = repository.get_user(session, user_id)
    if user is None:  # pragma: no cover - RLS allows reading self once principal is set
        raise Unauthenticated("session could not be established")
    principal = Principal(
        user.id,
        user.primary_email,
        user.display_name,
        # a session pinned to a client never carries platform-administrator power (review 1
        # item 4); the definer applies the same rule on every later request
        user.is_platform_admin and active_tenant_id is None,
        tenant_id=active_tenant_id,
    )
    ctx.principal = principal
    ctx.session_id = session_id
    _audit_for_user(
        session,
        ctx,
        user_id=user.id,
        email=user.primary_email,
        action="identity.login.succeeded",
        outcome="success",
        details={},
        auth_method=auth_method,
        pinned_tenant_id=active_tenant_id,
    )
    session.commit()
    auth = AuthResult(principal, session_id, csrf, min(idle_expires, absolute_expires))
    return LoginResult(auth, token, int((absolute_expires - now).total_seconds()))


def authenticate(
    session: Session,
    settings: Settings,
    ctx: RequestContext,
    *,
    token: str | None,
    method: str,
    csrf_header: str | None,
) -> AuthResult:
    """Resolve the session cookie to a principal; enforce expiry, revocation and CSRF."""
    if not token:
        raise Unauthenticated("authentication required")
    row = repository.resolve_session(session, hash_token(token))
    now = utcnow()
    if (
        row is None
        or row.revoked_at is not None
        or row.user_status != "active"
        or now >= row.idle_expires_at
        or now >= row.absolute_expires_at
    ):
        raise Unauthenticated("authentication required")
    if method.upper() not in SAFE_METHODS and not (
        csrf_header and hmac.compare_digest(hash_token(csrf_header), row.csrf_hash)
    ):
        raise CsrfFailed("missing or invalid CSRF token")

    db.set_principal(session, row.user_id)
    idle_expires = row.idle_expires_at
    if now - row.last_seen_at >= TOUCH_INTERVAL:  # sliding idle window, at most once a minute
        idle_expires = now + timedelta(minutes=settings.session_idle_minutes)
        repository.touch_session(session, row.session_id, now=now, idle_expires_at=idle_expires)
        session.commit()
    # A session opened through a tenant IdP is pinned to that tenant (Principal.tenant_id).
    # `shifde_resolve_session` returns no row for a pinned session whose membership is gone and
    # never reports administrator on a pinned session; checked again here (review 1 item 4).
    principal = Principal(
        row.user_id,
        row.email,
        row.display_name,
        row.is_platform_admin and row.active_tenant_id is None,
        tenant_id=row.active_tenant_id,
    )
    ctx.principal = principal
    ctx.session_id = row.session_id
    return AuthResult(
        principal,
        row.session_id,
        derive_csrf_token(token),
        min(idle_expires, row.absolute_expires_at),
    )


def logout(session: Session, ctx: RequestContext, auth: AuthResult) -> None:
    repository.revoke_session_by_id(session, auth.session_id, utcnow())
    _audit_for_user(
        session,
        ctx,
        user_id=auth.principal.user_id,
        email=auth.principal.email,
        action="identity.logout",
        outcome="success",
        details={},
    )
    session.commit()


# ------------------------------------------------------------------ members (tenant context)
def list_members(
    session: Session, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[MemberRow]:
    return repository.list_members(session, tenant_id=tenant_id, after=after, limit=limit)


def count_members(session: Session, tenant_id: UUID) -> int:
    return repository.count_members(session, tenant_id)


def user_labels(session: Session, user_ids: list[UUID]) -> dict[UUID, str]:
    """Display names of the (visible) users, for rendering owner / actor fields."""
    return repository.user_labels(session, user_ids)


def service_account_exists(session: Session, tenant_id: UUID, sa_id: UUID) -> bool:
    return repository.get_service_account(session, tenant_id, sa_id) is not None


def find_user_id_by_email(session: Session, email: str) -> UUID | None:
    user = repository.find_user_by_email(session, normalize_email(email))
    return user.id if user else None


def add_member(session: Session, *, tenant_id: UUID, user_id: UUID) -> None:
    """Add a person to a client. Platform administrator only (identity review 1 item 1; the
    database refuses any other context too). Every refusal, an unknown person and an existing
    membership alike, is one generic Conflict (review 1 item 3)."""
    _require_platform_admin(session, "adding a member")
    now = utcnow()
    try:
        with session.begin_nested():
            repository.insert_membership(
                session,
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "status": "active",
                    "joined_at": now,
                    "created_at": now,
                },
            )
    except (IntegrityError, DBAPIError) as exc:
        raise Conflict("the member could not be added") from exc


# ------------------------------------------------------------------ maintenance (owner / CLI)
def create_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    is_platform_admin: bool = False,
    password: str | None = None,
    passwords: PasswordService | None = None,
) -> UUID:
    """Create a user (and optionally a local credential). Runs as the DB owner (CLI/seed)."""
    if password is not None:
        if passwords is None:
            raise ValueError("passwords service required to set a password")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationFailed(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    now = utcnow()
    user_id = uuid7()
    repository.insert_user(
        session,
        {
            "id": user_id,
            "primary_email": normalize_email(email),
            "display_name": display_name,
            "status": "active",
            "is_platform_admin": is_platform_admin,
            "created_at": now,
        },
    )
    if password is not None and passwords is not None:
        repository.upsert_credential(session, user_id, passwords.hash(password), now)
    return user_id


def set_password(
    session: Session, passwords: PasswordService, user_id: UUID, password: str
) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    repository.upsert_credential(session, user_id, passwords.hash(password), utcnow())


def create_setup_token(session: Session, user_id: UUID) -> tuple[str, datetime]:
    """One-time bootstrap token (1 h); only its SHA-256 is stored, plaintext returned once.
    Owner / CLI only: `setup_tokens` grants the app role nothing (B-902)."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + SETUP_TOKEN_TTL
    repository.insert_setup_token(
        session,
        {
            "id": uuid7(),
            "user_id": user_id,
            "token_hash": hash_token(token),
            "purpose": "bootstrap",
            "expires_at": expires,
            "created_at": now,
        },
    )
    return token, expires


def platform_admin_exists(session: Session) -> bool:
    """Owner / CLI only: refuses (`repository.OwnerOnly`) where row-level security applies
    (identity review 1 item 5)."""
    return repository.platform_admin_exists(session)


# ------------------------------------------------------------------ setup-token redemption
LINK_WINDOW_SECONDS = 3600
INVITE_TOKEN_TTL = timedelta(hours=72)


def is_member(session: Session, tenant_id: UUID, user_id: UUID) -> bool:
    return repository.is_member(session, tenant_id, user_id)


def redeem_setup_token(
    session: Session,
    settings: Settings,
    passwords: PasswordService,
    ctx: RequestContext,
    *,
    token: str,
    password: str | None,
    previous_token: str | None,
) -> LoginResult | None:
    """Single-use redemption (FR-AUTHN-011). With local auth enabled a password is required and a
    session is opened (returned); otherwise the user's next verified-email OIDC login is linked
    (returns None). Invalid, used or expired tokens are indistinguishable (401)."""
    password_hash: str | None = None
    if settings.auth_local_enabled:
        if not password:
            raise ValidationFailed("a password is required")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationFailed(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        password_hash = passwords.hash(password)
    elif password:
        raise AuthDisabled("local authentication is disabled; omit the password")
    redeemed = repository.redeem_setup_token(
        session, hash_token(token), password_hash, LINK_WINDOW_SECONDS
    )
    if redeemed is None:
        session.rollback()
        db.set_tenant(session, None)  # a pre-tenant event is filed with no client context
        audit.record(
            session,
            ctx,
            action="identity.setup_token.rejected",
            outcome="failure",
            tenant_id=None,
            details={"reason": "invalid_or_expired"},
        )
        session.commit()
        raise InvalidCredentials("invalid or expired setup token")
    user_id, mode = redeemed
    db.set_principal(session, user_id)
    user = repository.get_user(session, user_id)
    if user is None:  # pragma: no cover - the definer only redeems for an existing active user
        raise InvalidCredentials("invalid or expired setup token")
    _audit_for_user(
        session,
        ctx,
        user_id=user_id,
        email=user.primary_email,
        action="identity.setup_token.redeemed",
        outcome="success",
        details={"mode": mode},
        auth_method="setup_token",
    )
    if mode == "oidc_link":
        session.commit()
        return None
    return establish_session(
        session,
        settings,
        ctx,
        user_id=user_id,
        previous_token=previous_token,
        auth_method="setup_token",
        reset_lockout=True,
    )


def create_invite_token(session: Session, user_id: UUID) -> tuple[str, datetime]:
    """Invitation setup token (72 h), shown once. Platform administrator only, for an active
    person with no credential and no identity (the definer trusts no membership row: identity
    review 1 item 1); every refusal is one generic Forbidden (review 1 item 3)."""
    _require_platform_admin(session, "issuing an invitation")
    token = secrets.token_urlsafe(32)
    expires = utcnow() + INVITE_TOKEN_TTL
    try:
        with session.begin_nested():
            repository.create_invite_token(session, user_id, hash_token(token), expires)
    except DBAPIError as exc:
        raise Forbidden("the invitation could not be issued") from exc
    return token, expires


# ------------------------------------------------------------------ members (tenant admin)
def list_members_admin(
    session: Session, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[repository.AdminMemberRow]:
    return repository.list_members_admin(session, tenant_id=tenant_id, after=after, limit=limit)


def get_membership(session: Session, tenant_id: UUID, user_id: UUID) -> Membership | None:
    return repository.get_membership(session, tenant_id, user_id)


def create_member_user(session: Session, *, tenant_id: UUID, email: str, display_name: str) -> UUID:
    """Create a person and an active membership in one client. People are platform-level
    (design 5.3): only the platform administrator creates them (`users_insert`, B-901) and adds
    memberships (review 1 item 1), so a client owner cannot (B-913). A unique violation (the
    e-mail already has an account) is a Conflict; the administrator sees every person anyway."""
    _require_platform_admin(session, "creating a person")
    user_id = uuid7()
    try:
        with session.begin_nested():
            repository.insert_user_basic(
                session, user_id, normalize_email(email), display_name, utcnow()
            )
    except IntegrityError as exc:
        raise Conflict("a user with this e-mail already exists") from exc
    add_member(session, tenant_id=tenant_id, user_id=user_id)
    session.flush()
    return user_id


def set_member_status(
    session: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    status: str,
    expected_version: int,
    actor_id: UUID,
) -> tuple[Membership, int, int]:
    """Suspend/reactivate a membership. Suspension also revokes the user's sessions and API keys
    in this tenant immediately. Returns (membership, sessions_revoked, keys_revoked)."""
    updated = repository.update_membership_status(
        session, tenant_id, user_id, expected_version, status
    )
    if updated is None:
        raise Conflict("row_version mismatch: the member was modified; reload and retry")
    sessions_revoked = keys_revoked = 0
    if status == "suspended":
        sessions_revoked = repository.revoke_user_sessions(session, user_id)
        keys_revoked = repository.revoke_keys_of_principal(
            session, tenant_id, "user", user_id, actor_id, utcnow()
        )
    return updated, sessions_revoked, keys_revoked


# ------------------------------------------------------------------ service accounts / keys / IdPs
# Thin pass-throughs so the admin layer never touches the repository directly.
AdminMemberRow = repository.AdminMemberRow
get_user = repository.get_user
get_service_account = repository.get_service_account
list_service_accounts = repository.list_service_accounts
update_service_account = repository.update_service_account
get_api_key = repository.get_api_key
list_api_keys = repository.list_api_keys
revoke_api_key = repository.revoke_api_key
revoke_keys_of_principal = repository.revoke_keys_of_principal
shorten_api_key_expiry = repository.shorten_api_key_expiry
get_idp = repository.get_idp
list_idps = repository.list_idps
list_public_idps = repository.list_public_idps
update_idp = repository.update_idp


def create_service_account(
    session: Session, *, tenant_id: UUID, name: str, description: str | None, owner_id: UUID
) -> UUID:
    sa_id = uuid7()
    try:
        with session.begin_nested():
            repository.insert_service_account(
                session,
                {
                    "id": sa_id,
                    "tenant_id": tenant_id,
                    "name": name,
                    "description": description,
                    "status": "active",
                    "owner_principal_id": owner_id,
                    "created_at": utcnow(),
                },
            )
    except IntegrityError as exc:
        raise Conflict("a service account with this name already exists") from exc
    return sa_id


def create_idp(session: Session, values: dict[str, Any]) -> UUID:  # Any: column values
    idp_id = uuid7()
    repository.insert_idp(session, {"id": idp_id, "created_at": utcnow(), **values})
    return idp_id
