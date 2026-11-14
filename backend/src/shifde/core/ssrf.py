"""Outbound HTTP for the API process (IdP discovery/JWKS/token), on top of the EgressGuard.

https only (plain http solely to loopback when SHIFDE_ENV=development). Every resolved address must
be public (loopback additionally in development); DNS is resolved once and the connection is pinned
to the checked address (closes the P2 DNS-rebinding limit). Redirects are never followed and
responses are size-capped.
"""

from __future__ import annotations

import json
from typing import Any  # Any: decoded JSON documents
from urllib.parse import urlsplit

import httpx

from shifde.core.egress import (
    LOOPBACK_NETWORKS,
    EgressGuard,
    EgressPolicy,
    EgressViolation,
    Resolver,
    default_resolver,
)

__all__ = [
    "LOOPBACK_HOSTS",
    "MAX_RESPONSE_BYTES",
    "EgressViolation",
    "OutboundHttp",
    "Resolver",
    "check_scheme",
    "check_url",
    "default_resolver",
]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_RESPONSE_BYTES = 1_000_000


def _policy(allow_dev_loopback: bool, timeout: float) -> EgressPolicy:
    return EgressPolicy(
        allowed_schemes=frozenset({"https", "http"} if allow_dev_loopback else {"https"}),
        allowed_cidrs=LOOPBACK_NETWORKS if allow_dev_loopback else (),
        max_response_bytes=MAX_RESPONSE_BYTES,
        timeout_seconds=timeout,
    )


def _check_http_scheme(url: str, *, allow_dev_loopback: bool) -> None:
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host:
        raise EgressViolation("only absolute http(s) URLs are allowed", "scheme")
    if parts.username or parts.password:
        raise EgressViolation("credentials in URLs are not allowed", "scheme")
    if parts.scheme == "http" and not (allow_dev_loopback and host in LOOPBACK_HOSTS):
        raise EgressViolation("https is required", "scheme")


def check_url(url: str, *, allow_dev_loopback: bool, resolver: Resolver = default_resolver) -> None:
    _check_http_scheme(url, allow_dev_loopback=allow_dev_loopback)
    EgressGuard([_policy(allow_dev_loopback, 5.0)], resolver=resolver).check_url(url)


def check_scheme(url: str, *, allow_dev_loopback: bool) -> None:
    """Configuration-time validation (no DNS): https, or http to loopback in development."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password:
        raise EgressViolation("a plain absolute http(s) URL is required")
    if parts.scheme == "http" and not (allow_dev_loopback and host in LOOPBACK_HOSTS):
        raise EgressViolation("https is required")


class OutboundHttp:
    def __init__(
        self,
        *,
        allow_dev_loopback: bool,
        resolver: Resolver = default_resolver,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._allow_dev_loopback = allow_dev_loopback
        self._guard = EgressGuard(
            [_policy(allow_dev_loopback, timeout)], resolver=resolver, inner=transport
        )

    def check(self, url: str) -> None:
        """Validate a URL against the outbound policy without fetching it."""
        _check_http_scheme(url, allow_dev_loopback=self._allow_dev_loopback)
        self._guard.check_url(url)

    def _request(
        self, method: str, url: str, *, data: dict[str, str] | None, auth: tuple[str, str] | None
    ) -> dict[str, Any]:
        _check_http_scheme(url, allow_dev_loopback=self._allow_dev_loopback)
        body = b""
        try:
            with (
                self._guard.client() as client,
                client.stream(
                    method, url, data=data, auth=auth, headers={"Accept": "application/json"}
                ) as response,
            ):
                if response.status_code != 200:
                    raise EgressViolation(f"unexpected status {response.status_code}")
                for chunk in response.iter_bytes():
                    body += chunk
        except httpx.HTTPError as exc:
            raise EgressViolation(f"request failed: {exc.__class__.__name__}") from exc
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise EgressViolation("response is not JSON") from exc
        if not isinstance(doc, dict):
            raise EgressViolation("response is not a JSON object")
        return doc

    def get_json(self, url: str) -> dict[str, Any]:
        return self._request("GET", url, data=None, auth=None)

    def post_form(
        self, url: str, data: dict[str, str], auth: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        return self._request("POST", url, data=data, auth=auth)
