"""OAuth2 client-credentials tokens for the REST adapter (RFC 6749 section 4.4).

The client secret is the connection's bound `Credential`; `oauth2_url`, `client_id` and `scope`
are plain config. Tokens are fetched through the EgressGuard and cached in process memory under
`(tenant_id, connection_id, fingerprint)`, where the fingerprint is a SHA-256 over the secret and
the token settings, so another client, another connection or a rotated secret never reuses a
token. Every token received is registered for redaction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote_plus
from uuid import UUID

from shifde.adapters.base import AdapterFailure, ConnectionConfig, ErrorClass, ExecContext
from shifde.adapters.httputil import classify_exception
from shifde.core.redaction import register_secret

AUTH_METHOD = "oauth2_client_credentials"
EXPIRY_MARGIN_S = 30.0
DEFAULT_LIFETIME_S = 300.0
MAX_ENTRIES = 1024

clock = time.monotonic  # patched by tests

CacheKey = tuple[UUID, UUID, str]


class TokenCache:
    """Bounded in-memory map of cache key -> (access token, monotonic expiry)."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max = max_entries
        self._entries: OrderedDict[CacheKey, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if clock() >= entry[1]:
                del self._entries[key]
                return None
            return entry[0]

    def put(self, key: CacheKey, token: str, expires_at: float) -> None:
        with self._lock:
            self._entries[key] = (token, expires_at)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def drop(self, key: CacheKey) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"TokenCache({len(self._entries)} entries)"


TOKEN_CACHE = TokenCache()


@dataclass(frozen=True, repr=False)
class _Settings:
    url: str
    client_id: str
    scope: str | None


class OAuth2Tokens:
    """Access tokens for one connection in one execution context."""

    def __init__(self, ctx: ExecContext, conn: ConnectionConfig) -> None:
        cfg = conn.config
        if not cfg.get("oauth2_url"):
            raise AdapterFailure(ErrorClass.CONFIG, "oauth2_url is required for OAuth2")
        if not cfg.get("client_id"):
            raise AdapterFailure(ErrorClass.CONFIG, "client_id is required for OAuth2")
        if conn.credential is None:
            raise AdapterFailure(ErrorClass.CONFIG, "OAuth2 needs a bound client secret")
        scope = cfg.get("scope")
        self._ctx = ctx
        self._secret = conn.credential
        self._settings = _Settings(
            str(cfg["oauth2_url"]), str(cfg["client_id"]), str(scope) if scope else None
        )
        self._key: CacheKey | None = None
        if ctx.tenant_id is not None and conn.id is not None:
            material = json.dumps(
                [conn.credential.reveal(), self._settings.url, self._settings.client_id, scope]
            )
            fingerprint = hashlib.sha256(material.encode()).hexdigest()
            self._key = (ctx.tenant_id, conn.id, fingerprint)

    def __repr__(self) -> str:
        return "OAuth2Tokens(***)"

    def token(self) -> str:
        """A valid access token: the cached one, or a freshly fetched one."""
        if self._key is not None:
            cached = TOKEN_CACHE.get(self._key)
            if cached is not None:
                return cached
        return self._fetch()

    def refresh(self) -> str:
        """Drop the cached token (the API refused it) and fetch a new one."""
        if self._key is not None:
            TOKEN_CACHE.drop(self._key)
        return self._fetch()

    def _fetch(self) -> str:
        s = self._settings
        try:
            self._ctx.egress.check_url(s.url)
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            raise classify_exception(exc) from exc
        # RFC 6749 section 2.3.1: form-urlencode both parts, then HTTP Basic
        pair = f"{quote_plus(s.client_id)}:{quote_plus(self._secret.reveal())}"
        basic = base64.b64encode(pair.encode()).decode()
        form = {"grant_type": "client_credentials"}
        if s.scope:
            form["scope"] = s.scope
        try:
            with self._ctx.egress.client() as client:
                response = client.post(
                    s.url,
                    data=form,
                    headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                )
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            raise classify_exception(exc) from exc
        status = response.status_code
        if status in (400, 401, 403):
            raise AdapterFailure(
                ErrorClass.AUTH, f"the token endpoint rejected the client (HTTP {status})"
            )
        if status == 429:
            raise AdapterFailure(ErrorClass.RATE_LIMIT, "the token endpoint is rate limiting")
        if status != 200:
            raise AdapterFailure(ErrorClass.UPSTREAM, f"the token endpoint answered HTTP {status}")
        try:
            doc = response.json()
        except ValueError as exc:
            raise AdapterFailure(
                ErrorClass.UPSTREAM, "the token endpoint answer is not JSON"
            ) from exc
        token = doc.get("access_token") if isinstance(doc, dict) else None
        if not isinstance(token, str) or not token:
            raise AdapterFailure(ErrorClass.UPSTREAM, "the token endpoint sent no access token")
        register_secret(token)
        token_type = doc.get("token_type")
        if token_type is not None and str(token_type).lower() != "bearer":
            raise AdapterFailure(ErrorClass.UPSTREAM, "the token endpoint sent a non-bearer token")
        expires_in = doc.get("expires_in")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
            else DEFAULT_LIFETIME_S
        )
        if self._key is not None and lifetime > EXPIRY_MARGIN_S:
            TOKEN_CACHE.put(self._key, token, clock() + lifetime - EXPIRY_MARGIN_S)
        return token
