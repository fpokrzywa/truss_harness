"""API keys (SECURITY_MODEL 2.4): `shf_<env>_<prefix8>_<secret43>`.

Only the 8-character prefix (lookup) and HMAC-SHA256(pepper, full key) are stored. The 43-character
base62 secret carries ~256 bits, so a fast keyed hash is appropriate. Bearer authentication never
reads cookies and needs no CSRF token (there is no ambient credential to forge).

Port notes (identity part 2b, DECISIONS B-915..B-919):
* pepper rotation (findings Item 4): each key records the id of the pepper it was hashed with;
  new keys use `settings.api_key_pepper_id`, keys of a retired pepper verify while it is listed in
  `settings.api_key_retired_peppers`;
* a key is minted only for a principal of the key's own client, its prefix is server-generated
  and retried on a prefix collision only; every other refusal is one generic error (identity
  review 1 item 3);
* a rejected credential is audited (findings Item 5c): a key whose secret did not verify gets an
  installation-level row (no client is proven), a verified key that is revoked, expired or whose
  principal is inactive gets a row in the key's own client.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext, ResourceScope
from shifde.core.errors import (
    Conflict,
    CredentialExpired,
    CredentialRevoked,
    InvalidCredentials,
    UnsupportedAuthScheme,
)
from shifde.core.ids import uuid7
from shifde.core.redaction import register_secret
from shifde.core.settings import Settings
from shifde.identity import repository
from shifde.identity.models import ApiKey
from shifde.identity.service import AuthResult

ALPHABET = string.ascii_letters + string.digits
PREFIX_LEN = 8
SECRET_LEN = 43
MINT_ATTEMPTS = 5
LAST_USED_INTERVAL_SECONDS = 60
PREFIX_CONSTRAINT = "uq_api_keys__prefix"
REJECTED_ACTION = "identity.api_key.rejected"
MINT_REFUSED = "the API key could not be created"
_KEY_RE = re.compile(r"^shf_(dev|test|prod)_([0-9A-Za-z]{8})_([0-9A-Za-z]{43})$")
_DUMMY_HASH = hashlib.sha256(b"shifde-api-key-timing-equalisation").digest()


def _rand(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def hash_key(pepper: bytes, full_key: str) -> bytes:
    return hmac.new(pepper, full_key.encode(), hashlib.sha256).digest()


def resource_scope_to_json(scope: ResourceScope | None) -> dict[str, list[str]] | None:
    if scope is None:
        return None
    return {
        "workspace_ids": sorted(str(i) for i in scope.workspace_ids),
        "project_ids": sorted(str(i) for i in scope.project_ids),
    }


def resource_scope_from_json(raw: dict[str, list[str]] | None) -> ResourceScope | None:
    if raw is None:
        return None
    return ResourceScope(
        frozenset(UUID(x) for x in raw.get("workspace_ids", [])),
        frozenset(UUID(x) for x in raw.get("project_ids", [])),
    )


def _principal_in_client(
    session: Session, tenant_id: UUID, principal_type: str, principal_id: UUID
) -> bool:
    """`api_keys.principal_id` has no foreign key (user or service account): the principal must
    be an active member or service account of the key's own client (carry_forward, part 1)."""
    if principal_type == "service_account":
        sa = repository.get_service_account(session, tenant_id, principal_id)
        return sa is not None and sa.status == "active"
    if principal_type == "user":
        membership = repository.get_membership(session, tenant_id, principal_id)
        return membership is not None and membership.status == "active"
    return False


def _constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def mint(
    session: Session,
    settings: Settings,
    *,
    tenant_id: UUID,
    name: str,
    principal_type: str,
    principal_id: UUID,
    scopes: list[str],
    resource_scope: ResourceScope | None,
    expires_at: datetime,
    created_by: UUID | None,
    rotated_from_id: UUID | None = None,
) -> tuple[ApiKey, str]:
    """Create a key row in the caller's client; returns (row, full secret). The secret exists
    only in the return value. Refusals (principal not of this client, a constraint) are one
    generic `Conflict`; only a prefix collision is retried."""
    if not _principal_in_client(session, tenant_id, principal_type, principal_id):
        raise Conflict(MINT_REFUSED)
    for _ in range(MINT_ATTEMPTS):
        prefix = _rand(PREFIX_LEN)
        full = f"shf_{settings.key_env_label}_{prefix}_{_rand(SECRET_LEN)}"
        key_id = uuid7()
        try:
            with session.begin_nested():
                repository.insert_api_key(
                    session,
                    {
                        "id": key_id,
                        "tenant_id": tenant_id,
                        "name": name,
                        "principal_type": principal_type,
                        "principal_id": principal_id,
                        "prefix": prefix,
                        "key_hash": hash_key(settings.pepper, full),
                        "pepper_id": settings.api_key_pepper_id,
                        "scopes": sorted(set(scopes)),
                        "resource_scope": resource_scope_to_json(resource_scope),
                        "expires_at": expires_at,
                        "created_by": created_by,
                        "created_at": utcnow(),
                        "rotated_from_id": rotated_from_id,
                    },
                )
        except IntegrityError as exc:
            if _constraint(exc) == PREFIX_CONSTRAINT:
                continue  # astronomically rare, but the column is unique: draw another prefix
            raise Conflict(MINT_REFUSED) from None
        register_secret(full)
        row = repository.get_api_key(session, tenant_id, key_id)
        if row is None:  # pragma: no cover - the insert above is visible in this transaction
            raise Conflict(MINT_REFUSED)
        return row, full
    raise Conflict(MINT_REFUSED)


def _reject_unproven(session: Session, ctx: RequestContext) -> None:
    """No client is proven (malformed, unknown, wrong secret, retired pepper): one
    installation-level row, filed with no client context (B-917)."""
    db.set_principal(session, None)
    db.set_tenant(session, None)
    audit.record(
        session,
        ctx,
        action=REJECTED_ACTION,
        outcome="failure",
        tenant_id=None,
        auth_method="api_key",
        details={"reason": "invalid"},
    )
    session.commit()


def _reject_in_client(
    session: Session, ctx: RequestContext, row: repository.ResolvedKey, reason: str
) -> None:
    """The secret verified, so the key's own client is proven: the row goes to that client."""
    db.set_principal(session, None)
    db.set_tenant(session, row.tenant_id)
    try:
        audit.record(
            session,
            ctx,
            action=REJECTED_ACTION,
            outcome="failure",
            tenant_id=row.tenant_id,
            auth_method="api_key",
            target_type="api_key",
            target_id=row.key_id,
            details={"reason": reason},
        )
        session.commit()
    finally:
        db.set_tenant(session, None)


def authenticate_bearer(
    session: Session,
    settings: Settings,
    ctx: RequestContext,
    *,
    authorization: str,
) -> AuthResult:
    """Authenticate `Authorization: Bearer shf_...`.

    Errors: Basic or any other scheme -> 401 unsupported_auth_scheme; malformed/unknown/wrong
    secret -> 401 invalid_credentials; revoked -> 401 credential_revoked; expired -> 401
    credential_expired (the last two only after the secret verified, so they are no oracle).
    Every rejection after the scheme check writes an audit row (findings Item 5c)."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise UnsupportedAuthScheme("only Bearer API keys are supported")
    token = token.strip()
    peppers = settings.pepper_ring
    match = _KEY_RE.match(token)
    if match is None or match.group(1) != settings.key_env_label:
        hmac.compare_digest(_DUMMY_HASH, hash_key(settings.pepper, token))
        _reject_unproven(session, ctx)
        raise InvalidCredentials("invalid credentials")
    row = repository.resolve_api_key(session, match.group(2))
    pepper = peppers.get(row.pepper_id) if row is not None else None
    expected = row.key_hash if row is not None and pepper is not None else _DUMMY_HASH
    ok = hmac.compare_digest(expected, hash_key(pepper or settings.pepper, token))
    if row is None or pepper is None or not ok:
        _reject_unproven(session, ctx)
        raise InvalidCredentials("invalid credentials")
    now = utcnow()
    if row.revoked_at is not None:
        _reject_in_client(session, ctx, row, "revoked")
        raise CredentialRevoked("this API key has been revoked")
    if now >= row.expires_at:
        _reject_in_client(session, ctx, row, "expired")
        raise CredentialExpired("this API key has expired")
    if not row.principal_active:
        _reject_in_client(session, ctx, row, "principal_inactive")
        raise CredentialRevoked("the principal for this API key is not active")

    name = row.principal_name or "service-account"
    principal = Principal(
        user_id=row.principal_id,
        email=row.principal_email or f"sa:{name}",
        display_name=name,
        is_platform_admin=False,  # API keys never carry platform-admin power
        principal_type=row.principal_type,
        credential="api_key",
        tenant_id=row.tenant_id,
        api_key_id=row.key_id,
        key_scopes=frozenset(row.scopes),
        resource_scope=resource_scope_from_json(row.resource_scope),
    )
    db.set_principal(session, principal.principal_id)
    db.set_tenant(session, row.tenant_id)
    if repository.touch_api_key(session, row.key_id, now, LAST_USED_INTERVAL_SECONDS):
        session.commit()  # throttled: at most one write per key per minute
    ctx.principal = principal
    ctx.tenant_id = row.tenant_id
    return AuthResult(principal, row.key_id, "", now + timedelta(minutes=1))
