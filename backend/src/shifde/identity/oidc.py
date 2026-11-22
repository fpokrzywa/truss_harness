"""OpenID Connect authorization-code flow with PKCE (SECURITY_MODEL 2.1).

The API is the confidential client. Server-side state: `oidc_auth_requests` holds only SHA-256
hashes of `state`, `nonce` and a browser-binding cookie, expires quickly and is single use; it is
written and consumed only through two definers that take the client from the IdP row, so no
client context is set before the IdP has vouched for the person (DECISIONS B-922). The PKCE
verifier is not stored: it is derived as HMAC(`Settings.pkce_key`, state) (findings Item 4,
B-916), so a callback can recompute it and nobody who merely observes `state` can.

ID-token validation: algorithm allow-list (no `none`, no HMAC), signature against the IdP JWKS
(cached for a bounded lifetime: the response's `Cache-Control: max-age` held between
`JWKS_MIN_TTL` and `Settings.oidc_jwks_max_cache_seconds`, else `oidc_jwks_cache_seconds`;
refetched after expiry and fail-closed if that fetch fails; within the lifetime refreshed once,
rate-limited, on a `kid` miss), `iss` == discovery issuer == configured issuer, `aud` contains
the client id (and `azp` == client id when several audiences), `exp`, `iat`, `nbf` with at most
60 s skew, `nonce` bound to this request, non-empty `sub`. The IdP test (`run_checks`) fetches
discovery and the JWKS live, validates them like sign-in does, and never reads or writes the
sign-in caches (issue #189, B-3061).
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

The `entra` profile (SP3-6, design SP3 section 8, B-2711): after the ID token is valid, `tid`
must be the IdP's Entra tenant, `ver` 2.0 and `oid` a GUID; the identity is keyed by `oid` (as
`sub`, so the mapping and JIT definers are unchanged). An e-mail counts as verified (link and
JIT) only for a proven member of the pinned directory (`acct` present and 0, no `#EXT#` UPN):
`email` when the optional claim `xms_edov` is true, else `preferred_username`; otherwise the
sign-in carries no verified e-mail, because an installation IdP links across clients by e-mail
(isolation review 036-1 item 1, B-2712). A token whose groups do not fit
(overage: `_claim_names.groups`, `hasgroups`, more than `MAX_IDP_GROUPS`) refuses the sign-in
before any sync, so memberships never follow a partial list (B-2700). Group ids are matched
lowercase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
from shifde.identity.models import User
from shifde.identity.repository import ResolvedIdp
from shifde.secrets import service as secrets_service

log = structlog.get_logger("shifde.oidc")

ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
SKEW_SECONDS = 60
DISCOVERY_TTL = 300.0
JWKS_MIN_TTL = 60.0  # floor of a JWKS lifetime, so `max-age=0` does not refetch per sign-in
FAILURE_PATH = "/login?error=oidc_failed"
OVERAGE_PATH = "/login?error=oidc_groups_overage"
GROUPS_OVERAGE = "groups_overage"
OVERAGE_MESSAGE = (
    "Your Entra account is in too many groups for SETH to read. Ask an administrator to limit "
    "the groups sent to SETH."
)
MAX_IDP_GROUPS = 200  # SETH 3ba0e06: group names kept per sign-in (the definer refuses more)
_SAFE_NEXT = re.compile(r"^/(?!/)[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#]*$")


class OidcFailure(Exception):
    """A login failed; `reason` is for the audit log only and never shown to the browser.
    `message`, when set, is the one text the login page may show for it (group overage)."""

    def __init__(
        self, reason: str, *, tenant_id: UUID | None = None, message: str | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        # the client the failure is filed in: set only once that client's IdP has vouched
        self.tenant_id = tenant_id
        self.message = message


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


def cache_max_age(cache_control: str | None) -> int | None:
    """The first `max-age` of a `Cache-Control` header, or None when absent or not all digits;
    every other directive is ignored."""
    for directive in (cache_control or "").split(","):
        name, _, value = directive.strip().partition("=")
        if name.strip().lower() == "max-age":
            value = value.strip()
            return int(value) if value.isascii() and value.isdigit() else None
    return None


class OidcClient:
    """Process-wide helper: discovery + JWKS caches over the SSRF-guarded HTTP client."""

    def __init__(
        self,
        settings: Settings,
        http: OutboundHttp,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._discovery: dict[str, tuple[float, Discovery]] = {}
        # jwks_uri -> (fetched at, lifetime in seconds, keys)
        self._jwks: dict[str, tuple[float, float, KeySet]] = {}
        self._last_forced: dict[str, float] = {}

    @property
    def http(self) -> OutboundHttp:
        return self._http

    def discovery(self, url: str, expected_issuer: str) -> Discovery:
        cached = self._discovery.get(url)
        if cached and self._clock() - cached[0] < DISCOVERY_TTL:
            if cached[1].issuer != expected_issuer:  # another IdP row naming the same URL
                raise OidcFailure("issuer_mismatch_discovery")
            return cached[1]
        disc = self._fetch_discovery(url, expected_issuer)
        self._discovery[url] = (self._clock(), disc)
        return disc

    def _fetch_discovery(self, url: str, expected_issuer: str) -> Discovery:
        """Fetch and validate a discovery document; never reads or writes `_discovery`."""
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
        return disc

    def _load_jwks(self, jwks_uri: str) -> tuple[KeySet, Mapping[str, str]]:
        """Fetch and parse a JWKS with its response headers; never touches `_jwks`."""
        try:
            doc, headers = self._http.get_json_with_headers(jwks_uri)
            # the document is dict[str, Any]; joserfc types the argument as its JWKS TypedDict
            keys = KeySet.import_key_set(doc)  # type: ignore[arg-type]
        except (EgressViolation, JoseError, ValueError, KeyError, TypeError) as exc:
            raise OidcFailure("jwks_unavailable") from exc
        return keys, headers

    def _fetch_jwks(self, jwks_uri: str) -> KeySet:
        keys, headers = self._load_jwks(jwks_uri)
        max_age = cache_max_age(headers.get("cache-control"))
        ttl = self._settings.oidc_jwks_cache_seconds if max_age is None else max_age
        lifetime = min(self._settings.oidc_jwks_max_cache_seconds, max(JWKS_MIN_TTL, ttl))
        self._jwks[jwks_uri] = (self._clock(), lifetime, keys)
        return keys

    def signing_keys(self, jwks_uri: str, kid: str | None) -> KeySet:
        """Cached JWKS, refetched once expired (an expired set is never used, even when that
        fetch fails); within its lifetime a `kid` miss refreshes once (rate-limited)."""
        cached = self._jwks.get(jwks_uri)
        if cached is None or self._clock() - cached[0] >= cached[1]:
            self._jwks.pop(jwks_uri, None)
            return self._fetch_jwks(jwks_uri)
        keys = cached[2]
        if kid is not None and not any(k.kid == kid for k in keys.keys):
            last = self._last_forced.get(jwks_uri, 0.0)
            if self._clock() - last >= self._settings.oidc_jwks_min_refresh_seconds:
                self._last_forced[jwks_uri] = self._clock()
                return self._fetch_jwks(jwks_uri)
        return keys

    def run_checks(self, discovery_url: str, issuer: str) -> list[tuple[str, bool, str]]:
        """Uncached connectivity checks for the IdP `test` endpoint: (name, ok, detail). The
        sign-in caches are neither read nor written (issue #189)."""
        checks: list[tuple[str, bool, str]] = []
        try:
            disc = self._fetch_discovery(discovery_url, issuer)
        except OidcFailure as exc:
            checks.append(("discovery", False, exc.reason))
            return checks
        checks.append(("discovery", True, "discovery document fetched; issuer matches"))
        try:
            keys, _ = self._load_jwks(disc.jwks_uri)
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
    overage = reason == GROUPS_OVERAGE  # one row, its own action (B-2711)
    audit.record(
        session,
        ctx,
        action="identity.oidc.groups_overage" if overage else "identity.login.failed",
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
        if idp.profile == "entra":
            claims = _entra_claims(idp, claims)
        return _sign_in(session, settings, ctx, idp, claims, previous_token, req.next_path)
    except OidcFailure as exc:  # the IdP vouched: the failure belongs to its client
        raise OidcFailure(exc.reason, tenant_id=idp.tenant_id, message=exc.message) from exc


def _entra_email(claims: dict[str, Any]) -> tuple[str | None, bool]:
    """(address, verified) of an Entra token (B-2712). `email` is Entra's mutable `mail`
    attribute and a guest's comes from outside the pinned directory: it is verified only with
    `xms_edov` true for a member; a member's `preferred_username` (their UPN in the directory)
    is verified otherwise. Fail closed (isolation review 036-2): only `acct` present and exactly
    0 (int or "0") proves a member, and a `#EXT#` UPN is a guest whatever `acct` says; any other
    token, `acct` absent included, carries no verified address."""
    email = _text(claims.get("email"))
    username = _text(claims.get("preferred_username"))
    upns = (username, _text(claims.get("upn")))
    acct = claims.get("acct")
    member = (type(acct) is int and acct == 0) or acct == "0"
    if not member or any(u and "#EXT#" in u.upper() for u in upns):
        return email or username, False
    if email and claims.get("xms_edov") is True:
        return email, True
    if username:
        return username, True
    return email, False


def _guid(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _entra_claims(idp: ResolvedIdp, claims: dict[str, Any]) -> dict[str, Any]:
    """The validated claims of an `entra` IdP's token with `sub` := `oid` (canonical lowercase):
    the person is keyed by their object id in the pinned tenant, never by the pairwise `sub`."""
    tid = _guid(claims.get("tid"))
    if tid is None or tid != idp.entra_tenant_id:
        raise OidcFailure("entra_tenant_mismatch")
    if claims.get("ver") != "2.0":
        raise OidcFailure("entra_version_invalid")
    oid = _guid(claims.get("oid"))
    if oid is None:
        raise OidcFailure("entra_oid_invalid")
    return {**claims, "sub": str(oid)}


def _client_secret(
    session: Session, settings: Settings, ctx: RequestContext, idp: ResolvedIdp
) -> secrets_service.SecretValue:
    """The IdP's client secret for the token exchange. The context is the IdP row's own scope
    (from the database, never from the request) and is cleared before returning (B-922). An
    installation IdP's secret is read with no client and no administrator context through
    `shifde_installation_idp_secret` (identity review 4 item 5, B-2260)."""
    assert idp.client_secret_ref_id is not None
    provider = secrets_service.SecretProvider(session, settings.keyring, ctx)
    try:
        if idp.tenant_id is None:
            db.set_tenant(session, None)
            value = provider.resolve_installation_idp(idp.id)
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

    if idp.profile == "entra":
        email_raw, verified = _entra_email(claims)
    else:
        email_claim = idp.claim_mappings.get("email", "email")
        email_raw = _text(claims.get(email_claim))
        # `email_verified` only vouches for the standard `email` claim; a custom mapping is
        # untrusted.
        verified = email_claim == "email" and claims.get("email_verified") is True
    email = identity.normalize_email(email_raw) if email_raw else None
    groups: list[str] | None = None
    if idp.tenant_id is None and idp.group_sync_mode == "sync":
        if idp.profile == "entra" and _groups_overage(claims, idp):
            # fail closed before any identity, membership or session change (B-2711)
            raise OidcFailure(GROUPS_OVERAGE, message=OVERAGE_MESSAGE)
        groups = _claim_groups(claims, idp)
    user_id = _map_identity(session, idp, claims, email if verified else None, email, verified)
    if idp.tenant_id is not None:
        _require_tenant_member(session, idp.tenant_id, user_id)
    elif groups is not None:
        _sync_groups(session, idp, user_id, groups, ctx=ctx)
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


def _text(value: Any) -> str | None:
    """A non-empty string claim, else None."""
    return value if isinstance(value, str) and value else None


def _group_names(claims: dict[str, Any], idp: ResolvedIdp) -> list[str]:
    """The distinct group names of the claim (lowercased for an `entra` IdP: object ids)."""
    raw = claims.get(idp.claim_mappings.get("groups", "groups"))
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    names = [g.strip() for g in items if isinstance(g, str) and g.strip() and len(g) <= 200]
    if idp.profile == "entra":
        names = [g.lower() for g in names]
    return list(dict.fromkeys(names))


def _claim_groups(claims: dict[str, Any], idp: ResolvedIdp) -> list[str]:
    return _group_names(claims, idp)[:MAX_IDP_GROUPS]


def _groups_overage(claims: dict[str, Any], idp: ResolvedIdp) -> bool:
    """Entra sends a pointer instead of the groups when they do not fit in the token
    (`_claim_names` / `hasgroups`); more than `MAX_IDP_GROUPS` is overage here as well, never a
    truncated list (B-2711)."""
    names = claims.get("_claim_names")
    return (
        (isinstance(names, dict) and "groups" in names)
        or bool(claims.get("hasgroups"))
        or len(_group_names(claims, idp)) > MAX_IDP_GROUPS
    )


def _sync_groups(
    session: Session,
    idp: ResolvedIdp,
    user_id: UUID,
    names: list[str],
    *,
    ctx: RequestContext | None = None,
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
        _audit_group_sync(session, ctx, user_id, changes)


def _directory_person(session: Session, user_id: UUID) -> User | None:
    """The person who has just signed in, read as themself (`users_select`: own row), so the
    audit names their directory e-mail rather than an IdP claim (B-1649)."""
    previous = db.get_context(session).principal_id
    db.set_principal(session, user_id)
    try:
        return repository.get_user(session, user_id)
    finally:
        db.set_principal(session, previous)


def _audit_group_sync(
    session: Session,
    ctx: RequestContext,
    user_id: UUID,
    changes: list[authz.IdpGroupChange],
) -> None:
    """Admin review 2 item 1 (B-1624): a sync change is `identity.group.members_changed` in each
    client where the group is bound, with counts only (B-1408: the person may belong to other
    clients), and a platform administrator synced into such a group is a
    `tenancy.platform_admin.self_added` there (B-1012, B-1407), read through
    `shifde_bound_group_admin_ids` in that client's context. The context is cleared after.
    The counts-only rows carry no actor, address or agent of the signing-in person (admin
    isolation review 4 item 1, B-1649); a self-add names the person by their directory e-mail,
    never by the IdP's claim (item 2)."""
    counts_ctx = replace(ctx, client_ip=None, user_agent=None, principal=None)
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
                    counts_ctx,
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
                    person = _directory_person(session, user_id)
                    audit.record(
                        session,
                        ctx,
                        action="tenancy.platform_admin.self_added",
                        tenant_id=tenant_id,
                        actor_id=user_id,
                        target_type="user",
                        target_id=user_id,
                        target_label=f"user:{person.primary_email}" if person else None,
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
