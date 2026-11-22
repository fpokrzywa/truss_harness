"""An in-process OpenID provider for the OIDC tests (SETH 3ba0e06 `tests/p2util.py`, moved
unchanged in behaviour): discovery, JWKS and a token endpoint that verifies the S256 PKCE
challenge, served through an `httpx.MockTransport` (no network, no real IdP; REQ F-22 stays a
known defect until Entra in sub-project 3).

Entra mode (`FakeOidcProvider.entra(tenant)`, SP3-6): the issuer is
`https://login.microsoftonline.com/<tenant>/v2.0` and the endpoints sit under the tenant path as
Entra's do; `entra_claims_for` adds `tid`, `oid`, `ver`, `preferred_username`, GUID `groups` and
a pairwise `sub`, and `OVERAGE_CLAIM_NAMES` / `{"hasgroups": True}` are Entra's overage forms.
The host reaches the fake only through `resolver()` and the `MockTransport` of a test.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any  # Any: JWT claims and JSON documents
from urllib.parse import parse_qs, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://idp.test"
CLIENT_ID = "shifde-client"
CLIENT_SECRET = "test-client-secret-value-123"  # test fixture value, not a credential
PUBLIC_IP = "93.184.216.34"
ENTRA_HOST = "https://login.microsoftonline.com"
# Entra's group overage: the groups claim is replaced by a pointer to Microsoft Graph
OVERAGE_CLAIM_NAMES: dict[str, Any] = {
    "_claim_names": {"groups": "src1"},
    "_claim_sources": {
        "src1": {"endpoint": "https://graph.microsoft.com/v1.0/users/x/getMemberObjects"}
    },
}


def entra_issuer(tenant: str) -> str:
    return f"{ENTRA_HOST}/{tenant}/v2.0"


def resolver(host: str) -> list[str]:
    return [PUBLIC_IP]


@dataclass
class Authorization:
    state: str
    nonce: str
    challenge: str
    redirect_uri: str
    client_id: str
    scope: str
    location: str


@dataclass
class FakeOidcProvider:
    """A minimal OIDC provider: discovery, JWKS, token endpoint (S256 PKCE verified)."""

    kid: str = "key-1"
    key: RSAKey = field(
        default_factory=lambda: RSAKey.generate_key(2048, parameters={"kid": "key-1"})
    )
    codes: dict[str, tuple[Authorization, dict[str, Any]]] = field(default_factory=dict)
    token_calls: list[dict[str, str]] = field(default_factory=list)
    jwks_calls: int = 0
    discovery_issuer: str = ISSUER
    require_secret: bool = True
    issuer: str = ISSUER
    # further keys `/jwks` publishes after `key` (a rotation overlap); none by default
    extra_keys: list[RSAKey] = field(default_factory=list)
    # the `Cache-Control` header `/jwks` sends; none by default
    jwks_cache_control: str | None = None
    # Entra mode: the directory (tenant) id the issuer and the endpoint paths name
    entra_tenant: str | None = None

    @classmethod
    def entra(cls, tenant: str, **kwargs: Any) -> FakeOidcProvider:
        issuer = entra_issuer(tenant)
        return cls(entra_tenant=tenant, issuer=issuer, discovery_issuer=issuer, **kwargs)

    # ------------------------------------------------------------------ keys / tokens
    def rotate_key(self, kid: str) -> None:
        self.kid = kid
        self.key = RSAKey.generate_key(2048, parameters={"kid": kid})

    def mint(
        self,
        claims: dict[str, Any],
        *,
        key: RSAKey | None = None,
        kid: str | None = None,
        alg: str = "RS256",
    ) -> str:
        return jwt.encode({"alg": alg, "kid": kid or self.kid}, claims, key or self.key)

    def claims_for(self, auth: Authorization, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "sub-123",
            "email": "newuser@example.com",
            "email_verified": True,
            "name": "New User",
            "nonce": auth.nonce,
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def entra_claims_for(
        self, auth: Authorization, *, oid: str, groups: list[str] | None = None, **overrides: Any
    ) -> dict[str, Any]:
        """An Entra v2.0 ID token's claims of a member: `sub` is pairwise (never `oid`), no
        `email_verified` and no `xms_edov`; `overrides` set or (with None) drop any claim."""
        claims: dict[str, Any] = {
            "tid": self.entra_tenant,
            "oid": oid,
            "ver": "2.0",
            "sub": f"pairwise-{uuid.uuid4().hex}",
            "email_verified": None,
            "preferred_username": "entra.user@contoso.test",
            "acct": 0,  # a member of the directory (1: a guest)
            "groups": groups if groups is not None else [str(uuid.uuid4())],
        }
        claims.update(overrides)
        return self.claims_for(auth, **claims)

    def parse_authorization(self, location: str) -> Authorization:
        q = parse_qs(urlsplit(location).query)
        return Authorization(
            state=q["state"][0],
            nonce=q["nonce"][0],
            challenge=q["code_challenge"][0],
            redirect_uri=q["redirect_uri"][0],
            client_id=q["client_id"][0],
            scope=q["scope"][0],
            location=location,
        )

    def issue_code(
        self, auth: Authorization, claims: dict[str, Any] | str, code: str = "c1"
    ) -> str:
        """Register a code whose token response carries `claims` (a dict to sign, or a ready
        JWT)."""
        self.codes[code] = (auth, claims if isinstance(claims, dict) else {"__raw__": claims})
        return code

    # ------------------------------------------------------------------ HTTP
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # the egress guard connects to the resolved address; the Host header names the IdP
        base = f"https://{request.headers.get('host') or request.url.host}"
        if self.entra_tenant is not None:
            return self._handle_entra(request, path, base)
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovery_issuer if base == ISSUER else base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/jwks",
                    "token_endpoint_auth_methods_supported": ["client_secret_post"],
                },
            )
        if path == "/jwks":
            return self._jwks()
        if path == "/token" and request.method == "POST":
            return self._token(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _handle_entra(self, request: httpx.Request, path: str, base: str) -> httpx.Response:
        root = f"/{self.entra_tenant}"
        if base != ENTRA_HOST:
            return httpx.Response(404, json={"error": "not_found"})
        if path == f"{root}/v2.0/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": f"{base}{root}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{base}{root}/oauth2/v2.0/token",
                    "jwks_uri": f"{base}{root}/discovery/v2.0/keys",
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_post",
                        "client_secret_basic",
                    ],
                },
            )
        if path == f"{root}/discovery/v2.0/keys":
            return self._jwks()
        if path == f"{root}/oauth2/v2.0/token" and request.method == "POST":
            return self._token(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _jwks(self) -> httpx.Response:
        self.jwks_calls += 1
        headers = (
            {} if self.jwks_cache_control is None else {"Cache-Control": self.jwks_cache_control}
        )
        if self.extra_keys:
            keys = [k.as_dict(private=False) for k in (self.key, *self.extra_keys)]
            return httpx.Response(200, json={"keys": keys}, headers=headers)
        return httpx.Response(
            200, json={"keys": [self.key.as_dict(private=False)]}, headers=headers
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append(form)
        entry = self.codes.pop(form.get("code", ""), None)
        if entry is None or (self.require_secret and form.get("client_secret") != CLIENT_SECRET):
            return httpx.Response(400, json={"error": "invalid_grant"})
        auth, claims = entry
        verifier = form.get("code_verifier", "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if (
            digest.rstrip(b"=").decode() != auth.challenge
            or form["redirect_uri"] != auth.redirect_uri
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        raw = claims["__raw__"] if "__raw__" in claims else self.mint(claims)
        return httpx.Response(
            200, json={"id_token": raw, "access_token": "at", "token_type": "Bearer"}
        )
