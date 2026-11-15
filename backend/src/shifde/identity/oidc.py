"""OpenID Connect authorization-code flow with PKCE (SECURITY_MODEL 2.1).

The API is the confidential client. Server-side state: `oidc_auth_requests` holds only SHA-256
hashes of `state`, `nonce` and a browser-binding cookie, expires quickly and is single use; it is
written and consumed only through two definers that take the client from the IdP row, so no
client context is set before the IdP has vouched for the person (DECISIONS B-922). The PKCE
verifier is not stored: it is derived as HMAC(`Settings.pkce_key`, state) (findings Item 4,
B-916), so a callback can recompute it and nobody who merely observes `state` can.

ID-token validation: algorithm allow-list (no `none`, no HMAC), signature against the IdP JWKS
(cached; refreshed once, rate-limited, on a `kid` miss), `iss` == discovery issuer == configured
issuer, `aud` contains the client id (and `azp` == client id when several audiences), `exp`,
`iat`, `nbf` with at most 60 s skew, `nonce` bound to this request, non-empty `sub`.
Identity mapping is by `(iss, sub)` only; e-mail is an attribute, never the key.

Every failure path ends in a generic redirect; the specific reason is only audited and logged.
A failure is filed in the IdP's client only once that client's IdP has signed a valid ID token;
anything earlier proves no client and is one installation-level row (B-922, as B-917).

Changes from SETH 3ba0e06 (identity part 3a): identities are created only by the definers
(`shifde_resolve_identity` link, `shifde_jit_create_user`; review 1 item 2, B-923); IdP group
sync is only from installation IdPs into platform-level groups the platform administrator mapped
to that IdP (identity part 3b, `shifde_sync_idp_groups`, B-932; a client's IdP never syncs:
check constraint, and refused here as B-925); a cached discovery document is rechecked against
the expected issuer; an unknown or disabled IdP at the callback is audited (review 4 item 6).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any  # Any: decoded JSON documents / JWT claims
from urllib.parse import urlencode
from uuid import UUID

import structlog
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.core import db
from shifde.core.context import RequestContext
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.core.ssrf import EgressViolation, OutboundHttp
from shifde.identity import repository
from shifde.identity import service as identity
from shifde.identity.repository import ResolvedIdp
from shifde.secrets import service as secrets_service

log = structlog.get_logger("shifde.oidc")

ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
SKEW_SECONDS = 60
DISCOVERY_TTL = 300.0
FAILURE_PATH = "/login?error=oidc_failed"
MAX_IDP_GROUPS = 200  # SETH 3ba0e06: group names kept per sign-in (the definer refuses more)
_SAFE_NEXT = re.compile(r"^/(?!/)[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#]*$")


class OidcFailure(Exception):
    """A login failed; `reason` is for the audit log only and never shown to the browser."""

    def __init__(self, reason: str, *, tenant_id: UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # the client the failure is filed in: set only once that client's IdP has vouched
        self.tenant_id = tenant_id


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    auth_methods: tuple[str, ...]


def safe_next_path(value: str | None) -> str:
    """Only same-origin relative paths survive; anything else becomes `/`."""
    if value and len(value) <= 512 and _SAFE_NEXT.match(value) and "\\" not in value:
        return value
    return "/"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sha(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def pkce_verifier(key: bytes, state: str) -> str:
    """`key` is `Settings.pkce_key` (never the API-key pepper, B-916)."""
    return _b64url(hmac.new(key, b"oidc-pkce-v1|" + state.encode(), hashlib.sha256).digest())


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


class OidcClient:
    """Process-wide helper: discovery + JWKS caches over the SSRF-guarded HTTP client."""

    def __init__(self, settings: Settings, http: OutboundHttp) -> None:
        self._settings = settings
        self._http = http
        self._discovery: dict[str, tuple[float, Discovery]] = {}
        self._jwks: dict[str, tuple[float, KeySet]] = {}
        self._last_forced: dict[str, float] = {}

    @property
    def http(self) -> OutboundHttp:
        return self._http

    def discovery(self, url: str, expected_issuer: str) -> Discovery:
        cached = self._discovery.get(url)
        if cached and time.monotonic() - cached[0] < DISCOVERY_TTL:
            if cached[1].issuer != expected_issuer:  # another IdP row naming the same URL
                raise OidcFailure("issuer_mismatch_discovery")
            return cached[1]
        try:
            doc = self._http.get_json(url)
            disc = Discovery(
                issuer=str(doc["issuer"]),
                authorization_endpoint=str(doc["authorization_endpoint"]),
                token_endpoint=str(doc["token_endpoint"]),
                jwks_uri=str(doc["jwks_uri"]),
                auth_methods=tuple(doc.get("token_endpoint_auth_methods_supported", ())),
            )
            if disc.issuer != expected_issuer:
                raise OidcFailure("issuer_mismatch_discovery")
            for endpoint in (disc.authorization_endpoint, disc.token_endpoint, disc.jwks_uri):
                self._http.check(endpoint)
        except (KeyError, TypeError) as exc:
            raise OidcFailure("discovery_invalid") from exc
        except EgressViolation as exc:
            raise OidcFailure(f"discovery_unreachable:{exc}") from exc
        self._discovery[url] = (time.monotonic(), disc)
        return disc

    def _fetch_jwks(self, jwks_uri: str) -> KeySet:
        try:
            # get_json returns dict[str, Any]; joserfc types the argument as its JWKS TypedDict
            keys = KeySet.import_key_set(self._http.get_json(jwks_uri))  # type: ignore[arg-type]
        except (EgressViolation, JoseError, ValueError, KeyError, TypeError) as exc:
            raise OidcFailure("jwks_unavailable") from exc
        self._jwks[jwks_uri] = (time.monotonic(), keys)
        return keys

    def signing_keys(self, jwks_uri: str, kid: str | None) -> KeySet:
        """Cached JWKS; on a `kid` miss refresh once (rate-limited) before giving up."""
        cached = self._jwks.get(jwks_uri)
        if cached is None:
            return self._fetch_jwks(jwks_uri)
        keys = cached[1]
        if kid is not None and not any(k.kid == kid for k in keys.keys):
            last = self._last_forced.get(jwks_uri, 0.0)
            if time.monotonic() - last >= self._settings.oidc_jwks_min_refresh_seconds:
                self._last_forced[jwks_uri] = time.monotonic()
                return self._fetch_jwks(jwks_uri)
        return keys

    def run_checks(self, discovery_url: str, issuer: str) -> list[tuple[str, bool, str]]:
        """Uncached connectivity checks for the IdP `test` endpoint: (name, ok, detail)."""
        checks: list[tuple[str, bool, str]] = []
        self._discovery.pop(discovery_url, None)
        try:
            disc = self.discovery(discovery_url, issuer)
        except OidcFailure as exc:
            checks.append(("discovery", False, exc.reason))
            return checks
        checks.append(("discovery", True, "discovery document fetched; issuer matches"))
        try:
            keys = self._fetch_jwks(disc.jwks_uri)
            checks.append(("jwks", True, f"{len(keys.keys)} signing key(s) available"))
        except OidcFailure as exc:
            checks.append(("jwks", False, exc.reason))
        return checks

    def validate_id_token(
        self, raw: str, *, idp: ResolvedIdp, disc: Discovery, nonce_hash: bytes
    ) -> dict[str, Any]:
        try:
            header = json.loads(base64.urlsafe_b64decode(raw.split(".")[0] + "=="))
        except (ValueError, IndexError) as exc:
            raise OidcFailure("token_malformed") from exc
        if not isinstance(header, dict) or header.get("alg") not in ALLOWED_ALGS:
            raise OidcFailure("token_alg_rejected")
        kid = header.get("kid")
        keys = self.signing_keys(disc.jwks_uri, kid if isinstance(kid, str) else None)
        try:
            token = jwt.decode(raw, keys, algorithms=ALLOWED_ALGS)
        except (JoseError, ValueError, KeyError) as exc:
            raise OidcFailure("token_signature_invalid") from exc
        claims: dict[str, Any] = dict(token.claims)
        now = time.time()
        if claims.get("iss") != disc.issuer or claims.get("iss") != idp.issuer:
            raise OidcFailure("token_issuer_mismatch")
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else list(aud) if isinstance(aud, list) else []
        if idp.client_id not in audiences:
            raise OidcFailure("token_audience_mismatch")
        if len(audiences) > 1 and claims.get("azp") != idp.client_id:
            raise OidcFailure("token_azp_mismatch")
        exp, iat, nbf = claims.get("exp"), claims.get("iat"), claims.get("nbf")
        if not isinstance(exp, int | float) or now - SKEW_SECONDS >= exp:
            raise OidcFailure("token_expired")
        if iat is not None and (not isinstance(iat, int | float) or iat > now + SKEW_SECONDS):
            raise OidcFailure("token_iat_in_future")
        if nbf is not None and (not isinstance(nbf, int | float) or nbf > now + SKEW_SECONDS):
            raise OidcFailure("token_not_yet_valid")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(_sha(nonce), nonce_hash):
            raise OidcFailure("token_nonce_mismatch")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise OidcFailure("token_sub_invalid")
        return claims


def _redirect_uri(settings: Settings, idp_id: UUID) -> str:
    return f"{settings.oidc_base_url}/api/auth/oidc/{idp_id}/callback"


def begin_login(
    session: Session,
    settings: Settings,
    client: OidcClient,
    *,
    idp_id: UUID,
    next_path: str | None,
) -> tuple[str, str]:
    """Create the single-use state and return (authorize redirect URL, binding cookie value)."""
    idp = repository.resolve_idp(session, idp_id)
    if idp is None:
        raise OidcFailure("idp_not_found")
    _require_supported(idp)
    disc = client.discovery(idp.discovery_url, idp.issuer)
    state, nonce, binding = (secrets.token_urlsafe(32) for _ in range(3))
    # no client context: the definer takes the client from the IdP row (B-922)
    stored = repository.begin_oidc_request(
        session,
        request_id=uuid7(),
        idp_id=idp.id,
        state_hash=_sha(state),
        binding_hash=_sha(binding),
        nonce_hash=_sha(nonce),
        next_path=safe_next_path(next_path),
        ttl_seconds=settings.oidc_state_ttl_seconds,
    )
    session.commit()
    if not stored:
        raise OidcFailure("idp_not_found")
    scopes = list(dict.fromkeys(["openid", *idp.scopes]))
    query = urlencode(
        {
            "response_type": "code",
            "client_id": idp.client_id,
            "redirect_uri": _redirect_uri(settings, idp.id),
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(pkce_verifier(settings.pkce_key, state)),
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in disc.authorization_endpoint else "?"
    return f"{disc.authorization_endpoint}{sep}{query}", binding


@dataclass(frozen=True)
class OidcLoginResult:
    login: identity.LoginResult
    next_path: str


def record_failure(
    session: Session, ctx: RequestContext, idp_id: UUID | None, tenant_id: UUID | None, reason: str
) -> None:
    """Audit a failed OIDC sign-in. `tenant_id` is a client only when that client's IdP signed
    a valid ID token (`OidcFailure.tenant_id`); otherwise the row is installation-level."""
    session.rollback()
    db.set_principal(session, None)
    db.set_tenant(session, tenant_id)
    audit.record(
        session,
        ctx,
        action="identity.login.failed",
        outcome="failure",
        tenant_id=tenant_id,
        auth_method="oidc",
        details={"reason": reason, "idp_id": str(idp_id) if idp_id else None},
    )
    session.commit()
    db.set_tenant(session, None)


def complete_login(
    session: Session,
    settings: Settings,
    client: OidcClient,
    ctx: RequestContext,
    *,
    idp_id: UUID,
    code: str,
    state: str,
    binding: str | None,
    previous_token: str | None,
) -> OidcLoginResult:
    """Validate the callback and open a session. Raises OidcFailure on any problem."""
    idp = repository.resolve_idp(session, idp_id)
    if idp is None:
        # review 4 item 6: audited like every other failure; nothing proves a client
        record_failure(session, ctx, None, None, "idp_not_found")
        raise OidcFailure("idp_not_found")
    try:
        return _complete(session, settings, client, ctx, idp, code, state, binding, previous_token)
    except OidcFailure as exc:
        record_failure(session, ctx, idp.id, exc.tenant_id, exc.reason)
        raise
    except Exception as exc:  # noqa: BLE001 - any bug must still end in the generic failure path
        log.error("oidc.callback_error", exc_info=True)
        record_failure(session, ctx, idp.id, None, "internal_error")
        raise OidcFailure("internal_error") from exc


def _complete(
    session: Session,
    settings: Settings,
    client: OidcClient,
    ctx: RequestContext,
    idp: ResolvedIdp,
    code: str,
    state: str,
    binding: str | None,
    previous_token: str | None,
) -> OidcLoginResult:
    _require_supported(idp)
    req = repository.consume_oidc_request(session, _sha(state), idp.id)
    session.commit()  # single use: the state stays consumed even if a later step fails
    if req is None or req.tenant_id != idp.tenant_id:
        raise OidcFailure("state_invalid_or_replayed")
    if not binding or not hmac.compare_digest(_sha(binding), req.binding_hash):
        raise OidcFailure("binding_mismatch")

    disc = client.discovery(idp.discovery_url, idp.issuer)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(settings, idp.id),
        "client_id": idp.client_id,
        "code_verifier": pkce_verifier(settings.pkce_key, state),
    }
    auth: tuple[str, str] | None = None
    if idp.client_secret_ref_id is not None:
        value = _client_secret(session, settings, ctx, idp)
        if (
            "client_secret_basic" in disc.auth_methods
            and "client_secret_post" not in disc.auth_methods
        ):
            auth = (idp.client_id, value.reveal())
        else:
            form["client_secret"] = value.reveal()
    try:
        tokens = client.http.post_form(disc.token_endpoint, form, auth)
    except EgressViolation as exc:
        raise OidcFailure(f"token_exchange_failed:{exc}") from exc
    raw = tokens.get("id_token")
    if not isinstance(raw, str):
        raise OidcFailure("no_id_token")
    claims = client.validate_id_token(raw, idp=idp, disc=disc, nonce_hash=req.nonce_hash)
    try:
        return _sign_in(session, settings, ctx, idp, claims, previous_token, req.next_path)
    except OidcFailure as exc:  # the IdP vouched: the failure belongs to its client
        raise OidcFailure(exc.reason, tenant_id=idp.tenant_id) from exc


def _client_secret(
    session: Session, settings: Settings, ctx: RequestContext, idp: ResolvedIdp
) -> secrets_service.SecretValue:
    """The IdP's client secret for the token exchange. The context is the IdP row's own scope
    (from the database, never from the request) and is cleared before returning (B-922)."""
    assert idp.client_secret_ref_id is not None
    provider = secrets_service.SecretProvider(session, settings.keyring, ctx)
    try:
        if idp.tenant_id is None:
            with db.platform_admin_context(session):
                value = provider.resolve_system(
                    idp.client_secret_ref_id, "oidc.token_exchange", tenant_id=None
                )
                session.commit()
        else:
            db.set_tenant(session, idp.tenant_id)
            value = provider.resolve_system(
                idp.client_secret_ref_id, "oidc.token_exchange", tenant_id=idp.tenant_id
            )
            session.commit()
    finally:
        db.set_tenant(session, None)
    return value


def _sign_in(
    session: Session,
    settings: Settings,
    ctx: RequestContext,
    idp: ResolvedIdp,
    claims: dict[str, Any],
    previous_token: str | None,
    next_path: str,
) -> OidcLoginResult:

    email_claim = idp.claim_mappings.get("email", "email")
    email_raw = claims.get(email_claim)
    email = (
        identity.normalize_email(email_raw) if isinstance(email_raw, str) and email_raw else None
    )
    # `email_verified` only vouches for the standard `email` claim; a custom mapping is untrusted.
    verified = email_claim == "email" and claims.get("email_verified") is True
    user_id = _map_identity(session, idp, claims, email if verified else None, email, verified)
    if idp.tenant_id is not None:
        _require_tenant_member(session, idp.tenant_id, user_id)
    elif idp.group_sync_mode == "sync":
        _sync_groups(session, idp, user_id, _claim_groups(claims, idp), ctx=ctx, email=email)
    db.set_tenant(session, None)
    login = identity.establish_session(
        session,
        settings,
        ctx,
        user_id=user_id,
        previous_token=previous_token,
        auth_method="oidc",
        active_tenant_id=idp.tenant_id,  # a tenant IdP session is pinned to its tenant
    )
    return OidcLoginResult(login, next_path)


def _require_supported(idp: ResolvedIdp) -> None:
    """Groups are platform-level and hold SHI staff (design 5.3): only an installation IdP syncs
    them. A client's IdP with sync on (refused by `ck_idp_configs__group_sync_installation_only`
    since identity_0005) signs nobody in (B-925, kept for client IdPs by B-932)."""
    if idp.tenant_id is not None and idp.group_sync_mode != "off":
        raise OidcFailure("group_sync_unavailable")


def _claim_groups(claims: dict[str, Any], idp: ResolvedIdp) -> list[str]:
    raw = claims.get(idp.claim_mappings.get("groups", "groups"))
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    names = [g.strip() for g in items if isinstance(g, str) and g.strip() and len(g) <= 200]
    return list(dict.fromkeys(names))[:MAX_IDP_GROUPS]


def _sync_groups(
    session: Session,
    idp: ResolvedIdp,
    user_id: UUID,
    names: list[str],
    *,
    ctx: RequestContext | None = None,
    email: str | None = None,
) -> None:
    """Make the person's `idp_sync` memberships of the platform groups mapped to this
    installation IdP equal the claim (SETH synced per client and created groups; B-932). No
    client context: the definer checks the IdP and the person's identity there. With `ctx`,
    each change is audited in every client where the group is bound (B-1624)."""
    db.set_tenant(session, None)
    try:
        changes = authz.sync_idp_group_changes(session, idp.id, user_id, names)
    except DBAPIError as exc:
        raise OidcFailure("group_sync_refused") from exc
    if ctx is not None:
        _audit_group_sync(session, ctx, user_id, email, changes)


def _audit_group_sync(
    session: Session,
    ctx: RequestContext,
    user_id: UUID,
    email: str | None,
    changes: list[authz.IdpGroupChange],
) -> None:
    """Admin review 2 item 1 (B-1624): a sync change is `identity.group.members_changed` in each
    client where the group is bound, with counts only (B-1408: the person may belong to other
    clients), and a platform administrator synced into such a group is a
    `tenancy.platform_admin.self_added` there (B-1012, B-1407), read through
    `shifde_bound_group_admin_ids` in that client's context. The context is cleared after."""
    try:
        for change in changes:
            if change.change == "kept":
                continue
            added = change.change == "added"
            for tenant_id in change.bound_tenant_ids:
                db.set_tenant(session, tenant_id)
                names = {g.id: g.name for g in authz.bound_groups(session)}
                if change.group_id not in names:  # unbound since the sync read it
                    continue
                name = names[change.group_id]
                audit.record(
                    session,
                    ctx,
                    action="identity.group.members_changed",
                    tenant_id=tenant_id,
                    actor_label="idp_sync",
                    target_type="group",
                    target_id=change.group_id,
                    target_label=f"group:{name}",
                    details={
                        "group_name": name,
                        "group_id": str(change.group_id),
                        "added_count": int(added),
                        "removed_count": int(not added),
                        "source": "idp_sync",
                    },
                )
                if added and user_id in authz.bound_group_admin_ids(session, change.group_id):
                    audit.record(
                        session,
                        ctx,
                        action="tenancy.platform_admin.self_added",
                        tenant_id=tenant_id,
                        actor_id=user_id,
                        target_type="user",
                        target_id=user_id,
                        target_label=f"user:{email}" if email else None,
                        details={
                            "group_membership": "added",
                            "through_idp_sync": True,
                            "through_group_id": str(change.group_id),
                        },
                    )
    except BaseException:
        db.forget_tenant(session)  # never raise over the original error (B-1410)
        raise
    db.set_tenant(session, None)


def _require_tenant_member(session: Session, tenant_id: UUID, user_id: UUID) -> None:
    """A tenant IdP only signs in existing, active members of its own tenant, never a platform
    admin (an IdP admin must not be able to mint sessions for installation-level principals)."""
    db.set_principal(session, user_id)
    db.set_tenant(session, tenant_id)
    user = identity.get_user(session, user_id)
    if user is None or user.is_platform_admin:
        raise OidcFailure("tenant_idp_platform_admin_refused")
    membership = identity.get_membership(session, tenant_id, user_id)
    if membership is None or membership.status != "active":
        raise OidcFailure("tenant_idp_not_an_active_member")


def _map_identity(
    session: Session,
    idp: ResolvedIdp,
    claims: dict[str, Any],
    link_email: str | None,
    email: str | None,
    verified: bool,
) -> UUID:
    row = repository.resolve_identity(session, idp.issuer, str(claims["sub"]), link_email, idp.id)
    if row is not None:
        if row.user_status != "active":
            raise OidcFailure("user_disabled")
        return row.user_id
    if idp.jit == "off" or idp.tenant_id is not None:  # tenant IdPs never provision users
        raise OidcFailure("unknown_identity_jit_off")
    if not email or not verified:
        raise OidcFailure("jit_requires_verified_email")
    name_raw = claims.get("name")
    display = (name_raw if isinstance(name_raw, str) and name_raw.strip() else email.split("@")[0])[
        :200
    ]
    user_id = uuid7()
    # the person and the identity are created together by the definer (B-923); an existing
    # e-mail is never linked (takeover protection) and answers one generic refusal
    if not repository.jit_create_user(
        session,
        user_id=user_id,
        idp_id=idp.id,
        issuer=idp.issuer,
        subject=str(claims["sub"]),
        email=email,
        display_name=display,
    ):
        raise OidcFailure("jit_email_already_registered")
    return user_id
