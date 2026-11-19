"""An in-process OpenID provider for the OIDC tests (SETH 3ba0e06 `tests/p2util.py`, moved
unchanged in behaviour): discovery, JWKS and a token endpoint that verifies the S256 PKCE
challenge, served through an `httpx.MockTransport` (no network, no real IdP; REQ F-22 stays a
known defect until Entra in sub-project 3).
"""

from __future__ import annotations

import base64
import hashlib
import time
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
            self.jwks_calls += 1
            if self.extra_keys:
                keys = [k.as_dict(private=False) for k in (self.key, *self.extra_keys)]
                return httpx.Response(200, json={"keys": keys})
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path == "/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append(form)
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None or (
                self.require_secret and form.get("client_secret") != CLIENT_SECRET
            ):
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
        return httpx.Response(404, json={"error": "not_found"})
