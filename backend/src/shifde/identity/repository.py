"""SQL for users, memberships, credentials and sessions.

Pre-tenant lookups go through the SECURITY DEFINER functions (DATA_MODEL section 3.4); everything
else runs under RLS with the transaction's `app.*` context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.orm import Session

from shifde.identity.models import (
    ApiKey,
    AuthSession,
    IdpConfig,
    LocalCredential,
    Membership,
    ServiceAccount,
    SetupToken,
    User,
    UserIdentity,
)


@dataclass(frozen=True)
class LoginRow:
    user_id: UUID
    password_hash: str
    failed_attempts: int
    locked_until: datetime | None
    user_status: str


@dataclass(frozen=True)
class SessionRow:
    session_id: UUID
    user_id: UUID
    csrf_hash: bytes
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    email: str
    display_name: str
    is_platform_admin: bool
    user_status: str
    active_tenant_id: UUID | None


@dataclass(frozen=True)
class MembershipRow:
    tenant_id: UUID
    slug: str
    name: str
    status: str
    role_names: list[str]


@dataclass(frozen=True)
class MemberRow:
    membership_id: UUID
    user_id: UUID
    email: str
    display_name: str
    status: str
    role_names: list[str]


def resolve_local_login(session: Session, email: str) -> LoginRow | None:
    row = (
        session.execute(
            text("SELECT * FROM shifde.shifde_resolve_local_login(:email)"), {"email": email}
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return LoginRow(
        user_id=row["user_id"],
        password_hash=row["password_hash"],
        failed_attempts=row["failed_attempts"],
        locked_until=row["locked_until"],
        user_status=row["user_status"],
    )


def resolve_session(session: Session, session_hash: bytes) -> SessionRow | None:
    row = (
        session.execute(
            text("SELECT * FROM shifde.shifde_resolve_session(:h)"), {"h": session_hash}
        )
        .mappings()
        .first()
    )
    return SessionRow(**row) if row else None


def my_memberships(session: Session) -> list[MembershipRow]:
    rows = session.execute(text("SELECT * FROM shifde.shifde_my_memberships()")).mappings().all()
    return [
        MembershipRow(
            tenant_id=r["tenant_id"],
            slug=r["slug"],
            name=r["name"],
            status=r["status"],
            role_names=list(r["role_names"] or []),
        )
        for r in rows
    ]


def my_membership_tenants(session: Session) -> list[UUID]:
    """Tenant ids of the acting person's own active memberships (needs the principal context;
    narrow definer `shifde_my_membership_tenants`, identity_0002)."""
    return list(
        session.execute(text("SELECT * FROM shifde.shifde_my_membership_tenants()")).scalars()
    )


@dataclass(frozen=True)
class SourceLock:
    failed_attempts: int
    locked_until: datetime | None


def lock_login_source(session: Session, user_id: UUID, source_hash: bytes) -> SourceLock | None:
    """`SELECT ... FOR UPDATE` on the person's failed-login counter for one source (B-911; needs
    the principal context: `login_lockouts` is own_row)."""
    row = session.execute(
        text(
            "SELECT failed_attempts, locked_until FROM shifde.login_lockouts "
            "WHERE user_id = :u AND source_hash = :s FOR UPDATE"
        ),
        {"u": user_id, "s": source_hash},
    ).first()
    return SourceLock(row[0], row[1]) if row else None


def record_source_failure(
    session: Session,
    user_id: UUID,
    source_hash: bytes,
    *,
    now: datetime,
    threshold: int,
    base_seconds: int,
    max_seconds: int,
) -> None:
    """Count one failure for (person, source) and set an exponential lock at the threshold, in one
    statement (same rule as `record_login_failure`, keyed per source)."""
    session.execute(
        text(
            "INSERT INTO shifde.login_lockouts AS l "
            "(user_id, source_hash, failed_attempts, locked_until, updated_at) "
            "VALUES (:user_id, :source, 1, CASE WHEN 1 >= :threshold THEN "
            ":now + make_interval(secs => CAST(:base AS double precision)) END, :now) "
            "ON CONFLICT ON CONSTRAINT pk_login_lockouts DO UPDATE SET "
            "failed_attempts = l.failed_attempts + 1, updated_at = :now, "
            "locked_until = CASE "
            "WHEN l.locked_until IS NOT NULL AND l.locked_until > :now THEN l.locked_until "
            "WHEN l.failed_attempts + 1 >= :threshold THEN :now + make_interval(secs => LEAST("
            "CAST(:base AS double precision) * "
            "power(2, LEAST(l.failed_attempts + 1 - :threshold, 30)), "
            "CAST(:max AS double precision))) "
            "ELSE l.locked_until END"
        ),
        {
            "now": now,
            "threshold": threshold,
            "base": base_seconds,
            "max": max_seconds,
            "user_id": user_id,
            "source": source_hash,
        },
    )


def account_recent_failures(
    session: Session, user_id: UUID, since: datetime, *, per_source_cap: int
) -> int:
    """Failures counted for the person across every source whose counter moved since `since`
    (the account-wide ceiling, identity review 2 item 3; needs the principal context). Each
    source counts at most `per_source_cap` (its own lock threshold, isolation review 3 item 1):
    one address that keeps failing while locked cannot reach the ceiling alone."""
    return int(
        session.execute(
            text(
                "SELECT COALESCE(SUM(LEAST(failed_attempts, :cap)), 0) "
                "FROM shifde.login_lockouts WHERE user_id = :u AND updated_at > :since"
            ),
            {"u": user_id, "since": since, "cap": per_source_cap},
        ).scalar_one()
    )


def reset_login_source(session: Session, user_id: UUID, source_hash: bytes, now: datetime) -> None:
    session.execute(
        text(
            "UPDATE shifde.login_lockouts SET failed_attempts = 0, locked_until = NULL, "
            "updated_at = :now WHERE user_id = :u AND source_hash = :s"
        ),
        {"u": user_id, "s": source_hash, "now": now},
    )


def lock_credentials(session: Session, user_id: UUID) -> LoginRow | None:
    """`SELECT ... FOR UPDATE` on the user's credential row (needs principal context set)."""
    row = session.execute(
        select(
            LocalCredential.password_hash,
            LocalCredential.failed_attempts,
            LocalCredential.locked_until,
        )
        .where(LocalCredential.user_id == user_id)
        .with_for_update()
    ).first()
    if row is None:
        return None
    return LoginRow(user_id, row[0], row[1], row[2], "active")


def record_login_failure(
    session: Session,
    user_id: UUID,
    *,
    now: datetime,
    threshold: int,
    base_seconds: int,
    max_seconds: int,
) -> None:
    """Atomically count a failure and (once the threshold is reached) set an exponential lock.

    A single UPDATE decides from the row's *current* counter, so concurrent failures cannot
    race on a stale value. An already-active lock is never shortened or extended."""
    session.execute(
        text(
            "UPDATE shifde.local_credentials SET "
            "failed_attempts = failed_attempts + 1, "
            "locked_until = CASE "
            "WHEN locked_until IS NOT NULL AND locked_until > :now THEN locked_until "
            "WHEN failed_attempts + 1 >= :threshold THEN :now + make_interval(secs => LEAST("
            "CAST(:base AS double precision) * "
            "power(2, LEAST(failed_attempts + 1 - :threshold, 30)), "
            "CAST(:max AS double precision))) "
            "ELSE locked_until END "
            "WHERE user_id = :user_id"
        ),
        {
            "now": now,
            "threshold": threshold,
            "base": base_seconds,
            "max": max_seconds,
            "user_id": user_id,
        },
    )


def reset_login_state(session: Session, user_id: UUID) -> None:
    session.execute(
        update(LocalCredential)
        .where(LocalCredential.user_id == user_id)
        .values(failed_attempts=0, locked_until=None)
    )


def touch_last_login(session: Session, user_id: UUID, now: datetime) -> None:
    session.execute(update(User).where(User.id == user_id).values(last_login_at=now))


def insert_session(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(AuthSession).values(**values))


def revoke_session_by_id(session: Session, session_id: UUID, now: datetime) -> None:
    session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_session_by_hash(session: Session, session_hash: bytes) -> None:
    """Revoke whichever session owns this cookie hash (narrow SECURITY DEFINER function)."""
    session.execute(text("SELECT shifde.shifde_revoke_session(:h)"), {"h": session_hash})


def touch_session(
    session: Session, session_id: UUID, *, now: datetime, idle_expires_at: datetime
) -> None:
    session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id)
        .values(last_seen_at=now, idle_expires_at=idle_expires_at)
    )


# ------------------------------------------------------------------ members (tenant context)
def list_members(
    session: Session, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[MemberRow]:
    stmt = (
        select(User.id, User.primary_email, User.display_name, Membership.status, Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant_id)
        .order_by(Membership.id)
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(Membership.id > after)
    rows = session.execute(stmt).all()
    roles = _role_names_by_user(session, tenant_id, [r[0] for r in rows])
    return [
        MemberRow(
            membership_id=r[4],
            user_id=r[0],
            email=r[1],
            display_name=r[2],
            status=r[3],
            role_names=roles.get(r[0], []),
        )
        for r in rows
    ]


def _role_names_by_user(
    session: Session, tenant_id: UUID, user_ids: list[UUID]
) -> dict[UUID, list[str]]:
    if not user_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT rb.subject_id, array_agg(DISTINCT r.name ORDER BY r.name) "
            "FROM shifde.role_bindings rb JOIN shifde.roles r ON r.id = rb.role_id "
            "WHERE rb.tenant_id = :t AND rb.subject_type = 'user' AND rb.subject_id = ANY(:ids) "
            "AND (rb.expires_at IS NULL OR rb.expires_at > now()) GROUP BY rb.subject_id"
        ),
        {"t": tenant_id, "ids": user_ids},
    ).all()
    return {r[0]: list(r[1]) for r in rows}


def count_members(session: Session, tenant_id: UUID) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(Membership).where(Membership.tenant_id == tenant_id)
        )
        or 0
    )


def insert_user_basic(
    session: Session, user_id: UUID, email: str, display_name: str, now: datetime
) -> None:
    """Create a user with only the columns the app role may INSERT (never is_platform_admin)."""
    session.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name, status, created_at) "
            "VALUES (:id, :email, :name, 'active', :now)"
        ),
        {"id": user_id, "email": email, "name": display_name, "now": now},
    )


def is_member(session: Session, tenant_id: UUID, user_id: UUID) -> bool:
    return get_membership(session, tenant_id, user_id) is not None


def get_user(session: Session, user_id: UUID) -> User | None:
    return session.get(User, user_id)


def find_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.primary_email == email))


def user_labels(session: Session, user_ids: list[UUID]) -> dict[UUID, str]:
    """{user id: display name} for the visible users among `user_ids` (RLS decides visibility)."""
    if not user_ids:
        return {}
    rows = session.execute(select(User.id, User.display_name).where(User.id.in_(user_ids))).all()
    return {r[0]: r[1] for r in rows}


def insert_membership(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(Membership).values(**values))


# ------------------------------------------------------------------ maintenance (owner / CLI)
def insert_user(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(User).values(**values))


def upsert_credential(session: Session, user_id: UUID, password_hash: str, now: datetime) -> None:
    existing = session.get(LocalCredential, user_id)
    if existing is None:
        session.execute(
            insert(LocalCredential).values(
                user_id=user_id,
                password_hash=password_hash,
                password_changed_at=now,
                failed_attempts=0,
            )
        )
    else:
        session.execute(
            update(LocalCredential)
            .where(LocalCredential.user_id == user_id)
            .values(
                password_hash=password_hash,
                password_changed_at=now,
                failed_attempts=0,
                locked_until=None,
            )
        )


def create_invite_token(
    session: Session, user_id: UUID, token_hash: bytes, expires_at: datetime
) -> None:
    """Mint an invitation through the narrow definer (tenant member, never a platform admin);
    the definer records the acting principal itself."""
    session.execute(
        text("SELECT shifde.shifde_create_invite_token(:u, :h, :e)"),
        {"u": user_id, "h": token_hash, "e": expires_at},
    )


def insert_setup_token(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(SetupToken).values(**values))


class OwnerOnly(RuntimeError):
    """An owner-only maintenance query was run where row-level security applies."""


def platform_admin_exists(session: Session) -> bool:
    """Owner / CLI only (identity review 1 item 5): under `shifde_app` row-level security hides
    other people's rows, so the count would fail open (False); refuse instead."""
    if session.scalar(text("SELECT pg_catalog.row_security_active('shifde.users')")):
        raise OwnerOnly("platform_admin_exists needs the schema owner's maintenance session")
    return bool(
        session.scalar(select(func.count()).select_from(User).where(User.is_platform_admin))
    )


# ------------------------------------------------------------------ members (admin)
@dataclass(frozen=True)
class AdminMemberRow:
    membership_id: UUID
    user_id: UUID
    email: str
    display_name: str
    status: str
    row_version: int


def list_members_admin(
    session: Session, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[AdminMemberRow]:
    stmt = (
        select(
            Membership.id,
            User.id,
            User.primary_email,
            User.display_name,
            Membership.status,
            Membership.row_version,
        )
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant_id)
        .order_by(Membership.id)
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(Membership.id > after)
    return [AdminMemberRow(*r) for r in session.execute(stmt)]


def get_membership(session: Session, tenant_id: UUID, user_id: UUID) -> Membership | None:
    return session.scalar(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
    )


def update_membership_status(
    session: Session, tenant_id: UUID, user_id: UUID, expected_version: int, status: str
) -> Membership | None:
    stmt = (
        update(Membership)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == user_id,
            Membership.row_version == expected_version,
        )
        .values(status=status, row_version=Membership.row_version + 1)
        .returning(Membership)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


def revoke_user_sessions(session: Session, user_id: UUID) -> int:
    return int(
        session.execute(
            text("SELECT shifde.shifde_revoke_user_sessions(:u)"), {"u": user_id}
        ).scalar_one()
    )


def redeem_setup_token(
    session: Session, token_hash: bytes, password_hash: str | None, link_seconds: int
) -> tuple[UUID, str] | None:
    row = session.execute(
        text("SELECT * FROM shifde.shifde_redeem_setup_token(:h, :p, :s)"),
        {"h": token_hash, "p": password_hash, "s": link_seconds},
    ).first()
    return (row[0], row[1]) if row else None


@dataclass(frozen=True)
class IdentityRow:
    user_id: UUID
    user_status: str
    linked: bool


def resolve_identity(
    session: Session, issuer: str, subject: str, email: str | None, idp_id: UUID
) -> IdentityRow | None:
    row = session.execute(
        text("SELECT * FROM shifde.shifde_resolve_identity(:i, :s, :e, :c)"),
        {"i": issuer, "s": subject, "e": email, "c": idp_id},
    ).first()
    return IdentityRow(row[0], row[1], row[2]) if row else None


def insert_identity(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(UserIdentity).values(**values))


# ------------------------------------------------------------------ service accounts
def insert_service_account(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(ServiceAccount).values(**values))


def get_service_account(session: Session, tenant_id: UUID, sa_id: UUID) -> ServiceAccount | None:
    return session.scalar(
        select(ServiceAccount).where(
            ServiceAccount.tenant_id == tenant_id, ServiceAccount.id == sa_id
        )
    )


def list_service_accounts(
    session: Session, *, tenant_id: UUID, after: UUID | None, limit: int
) -> list[ServiceAccount]:
    stmt = select(ServiceAccount).where(ServiceAccount.tenant_id == tenant_id)
    if after is not None:
        stmt = stmt.where(ServiceAccount.id > after)
    return list(session.scalars(stmt.order_by(ServiceAccount.id).limit(limit)))


def update_service_account(
    session: Session,
    tenant_id: UUID,
    sa_id: UUID,
    expected_version: int | None,
    values: dict[str, Any],
) -> ServiceAccount | None:
    stmt = update(ServiceAccount).where(
        ServiceAccount.tenant_id == tenant_id, ServiceAccount.id == sa_id
    )
    if expected_version is not None:
        stmt = stmt.where(ServiceAccount.row_version == expected_version)
    stmt = (
        stmt.values(**values, row_version=ServiceAccount.row_version + 1)
        .returning(ServiceAccount)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


# ------------------------------------------------------------------ API keys
@dataclass(frozen=True)
class ResolvedKey:
    key_id: UUID
    tenant_id: UUID
    principal_type: str
    principal_id: UUID
    key_hash: bytes
    pepper_id: str
    scopes: list[str]
    resource_scope: dict[str, list[str]] | None
    expires_at: datetime
    revoked_at: datetime | None
    principal_active: bool
    principal_name: str
    principal_email: str


def resolve_api_key(session: Session, prefix: str) -> ResolvedKey | None:
    row = (
        session.execute(text("SELECT * FROM shifde.shifde_resolve_api_key(:p)"), {"p": prefix})
        .mappings()
        .first()
    )
    return ResolvedKey(**row) if row else None


def insert_api_key(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(ApiKey).values(**values))


def get_api_key(session: Session, tenant_id: UUID, key_id: UUID) -> ApiKey | None:
    return session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.id == key_id))


def list_api_keys(
    session: Session,
    *,
    tenant_id: UUID,
    principal_type: str | None,
    principal_id: UUID | None,
    after: UUID | None,
    limit: int,
) -> list[ApiKey]:
    stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
    if principal_type:
        stmt = stmt.where(ApiKey.principal_type == principal_type)
    if principal_id:
        stmt = stmt.where(ApiKey.principal_id == principal_id)
    if after is not None:
        stmt = stmt.where(ApiKey.id > after)
    return list(session.scalars(stmt.order_by(ApiKey.id).limit(limit)))


def touch_api_key(
    session: Session, key_id: UUID, now: datetime, min_interval_seconds: int, *, tenant_id: UUID
) -> bool:
    """Throttled last-used update: at most once per interval per key. Keyed by the key's client
    as well as row-level security (admin review 3 item 4, B-1646)."""
    res = session.execute(
        text(
            "UPDATE shifde.api_keys SET last_used_at = :now WHERE id = :id AND tenant_id = :t "
            "AND (last_used_at IS NULL OR last_used_at < :now - make_interval(secs => :s))"
        ),
        {"now": now, "id": key_id, "t": tenant_id, "s": min_interval_seconds},
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]  # DML returns a CursorResult


def revoke_api_key(
    session: Session, key_id: UUID, by: UUID | None, now: datetime, *, tenant_id: UUID
) -> bool:
    res = session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=by)
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]  # DML returns a CursorResult


def revoke_keys_of_principal(
    session: Session,
    tenant_id: UUID,
    principal_type: str,
    principal_id: UUID,
    by: UUID | None,
    now: datetime,
) -> int:
    res = session.execute(
        update(ApiKey)
        .where(
            ApiKey.tenant_id == tenant_id,
            ApiKey.principal_type == principal_type,
            ApiKey.principal_id == principal_id,
            ApiKey.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=by)
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]  # DML returns a CursorResult


def shorten_api_key_expiry(
    session: Session, key_id: UUID, expires_at: datetime, *, tenant_id: UUID
) -> None:
    session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id, ApiKey.expires_at > expires_at)
        .values(expires_at=expires_at)
    )


# ------------------------------------------------------------------ identity providers
@dataclass(frozen=True)
class ResolvedIdp:
    id: UUID
    tenant_id: UUID | None
    display_name: str
    issuer: str
    discovery_url: str
    client_id: str
    client_secret_ref_id: UUID | None
    scopes: list[str]
    claim_mappings: dict[str, str]
    jit: str
    group_sync_mode: str


def resolve_idp(session: Session, idp_id: UUID) -> ResolvedIdp | None:
    row = (
        session.execute(text("SELECT * FROM shifde.shifde_resolve_idp(:i)"), {"i": idp_id})
        .mappings()
        .first()
    )
    return ResolvedIdp(**row) if row else None


def list_public_idps(session: Session) -> list[IdpConfig]:
    """Active installation-wide IdPs (tenant_id NULL), readable without a tenant context."""
    return list(
        session.scalars(
            select(IdpConfig)
            .where(IdpConfig.tenant_id.is_(None), IdpConfig.status == "active")
            .order_by(IdpConfig.display_name)
        )
    )


def insert_idp(session: Session, values: dict[str, Any]) -> None:
    session.execute(insert(IdpConfig).values(**values))


def get_idp(session: Session, tenant_id: UUID | None, idp_id: UUID) -> IdpConfig | None:
    stmt = select(IdpConfig).where(IdpConfig.id == idp_id)
    stmt = stmt.where(
        IdpConfig.tenant_id.is_(None) if tenant_id is None else IdpConfig.tenant_id == tenant_id
    )
    return session.scalar(stmt)


def list_idps(
    session: Session, *, tenant_id: UUID | None, after: UUID | None, limit: int
) -> list[IdpConfig]:
    stmt = select(IdpConfig).where(
        IdpConfig.tenant_id.is_(None) if tenant_id is None else IdpConfig.tenant_id == tenant_id
    )
    if after is not None:
        stmt = stmt.where(IdpConfig.id > after)
    return list(session.scalars(stmt.order_by(IdpConfig.id).limit(limit)))


def update_idp(
    session: Session, idp_id: UUID, expected_version: int, values: dict[str, Any]
) -> IdpConfig | None:
    stmt = (
        update(IdpConfig)
        .where(IdpConfig.id == idp_id, IdpConfig.row_version == expected_version)
        .values(**values, row_version=IdpConfig.row_version + 1)
        .returning(IdpConfig)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    return session.scalars(stmt).first()


# ------------------------------------------------------------------ OIDC flow state
@dataclass(frozen=True)
class OidcRequest:
    id: UUID
    tenant_id: UUID | None
    binding_hash: bytes
    nonce_hash: bytes
    next_path: str


def begin_oidc_request(
    session: Session,
    *,
    request_id: UUID,
    idp_id: UUID,
    state_hash: bytes,
    binding_hash: bytes,
    nonce_hash: bytes,
    next_path: str,
    ttl_seconds: int,
) -> bool:
    """Store the hashed flow state of an active IdP under the IdP's own client (definer,
    B-922: `oidc_auth_requests` is closed to the application role)."""
    return bool(
        session.execute(
            text(
                "SELECT shifde.shifde_begin_oidc_request(:id, :idp, :state, :binding, :nonce, "
                ":next, :ttl)"
            ),
            {
                "id": request_id,
                "idp": idp_id,
                "state": state_hash,
                "binding": binding_hash,
                "nonce": nonce_hash,
                "next": next_path,
                "ttl": ttl_seconds,
            },
        ).scalar_one()
    )


def consume_oidc_request(session: Session, state_hash: bytes, idp_id: UUID) -> OidcRequest | None:
    """Atomically mark the request used (single use) and return it if valid and unexpired."""
    row = session.execute(
        text("SELECT * FROM shifde.shifde_consume_oidc_request(:h, :i)"),
        {"h": state_hash, "i": idp_id},
    ).first()
    return OidcRequest(row[0], row[1], bytes(row[2]), bytes(row[3]), row[4]) if row else None


def jit_create_user(
    session: Session,
    *,
    user_id: UUID,
    idp_id: UUID,
    issuer: str,
    subject: str,
    email: str,
    display_name: str,
) -> bool:
    """Just-in-time provisioning through an installation IdP (definer, B-923): the person and
    the identity are created together, or nothing (False) is."""
    return bool(
        session.execute(
            text("SELECT shifde.shifde_jit_create_user(:u, :i, :iss, :sub, :e, :n)"),
            {
                "u": user_id,
                "i": idp_id,
                "iss": issuer,
                "sub": subject,
                "e": email,
                "n": display_name,
            },
        ).scalar_one()
    )
