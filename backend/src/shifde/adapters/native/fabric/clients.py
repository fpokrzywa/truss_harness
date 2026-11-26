"""Bounded Fabric REST and Entra token clients. Native mutation requests are never replayed.

Egress is limited to the Fabric REST origin and the Entra token endpoint; redirects are never
followed, the process environment's proxy settings are ignored and a long-running operation's
`Location` header is never requested (FDE SDK `shifde_fabric/clients.py`, 1fd902b).

The executor's token callable is `CheckedTokens`: its `authorize` raises unless the job may still
use the target, and runs before every use of a token, a cached one included, and again before a new
one is kept. A token is used only while the credentials are those it was minted with. Every failure
of a token request is a bare `FabricAuthError`, with no cause and no context, and no error of this
module carries a secret, a token or the text of an answer.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any  # Any: decoded Fabric and Entra JSON
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from shifde.adapters.native.base import NeedsAttention
from shifde.adapters.native.contracts import Target, utcnow
from shifde.adapters.native.fabric.source import COLLECTIONS, ORIGIN

TIMEOUT = httpx.Timeout(30, connect=5)
RESPONSE_MAX = 24 * 1024 * 1024
LOGIN_ORIGIN = "https://login.microsoftonline.com"
SCOPE = "https://api.fabric.microsoft.com/.default"
TOKEN_MARGIN_SECONDS = 60
TOKEN_RESPONSE_MAX = 64 * 1024
TOKEN_SECONDS_MAX = 24 * 3600.0  # a token is kept no longer, whatever its answer says
_TOKEN = re.compile(r"[\x21-\x7e]+")  # what an Authorization header takes
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class FabricAPIError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(
            f"Fabric API request failed ({status}); inspect native request diagnostics"
        )
        self.status = status


class FabricAuthError(RuntimeError):
    """The Entra token request failed. The message never carries the secret, token or body."""

    def __init__(self) -> None:
        super().__init__("Entra token request failed")


class EntraTokenProvider:
    """Entra client-credentials tokens for the Fabric API, cached in memory until near expiry. It
    checks nothing when it answers the cached one: an executor uses it behind `CheckedTokens`."""

    def __init__(
        self,
        entra_tenant_id: str,
        client_id: str,
        client_secret: Callable[[], str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not _GUID.fullmatch(entra_tenant_id) or not _GUID.fullmatch(client_id):
            raise ValueError("Entra tenant and client ids must be GUIDs")
        self.token_url = f"{LOGIN_ORIGIN}/{entra_tenant_id}/oauth2/v2.0/token"
        self.client_id, self.client_secret, self.transport = client_id, client_secret, transport
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() >= self._expires:
                self._token, lifetime = self._fetch()
                self._expires = time.monotonic() + lifetime - TOKEN_MARGIN_SECONDS
            return self._token

    def _fetch(self) -> tuple[str, float]:
        failed = False
        try:
            data = self._send()
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):  # they hold the request or answer
            failed = True
        if failed:
            raise FabricAuthError
        try:
            body = json.loads(data)
        except (ValueError, RecursionError):  # a decode error carries the answer
            failed = True
        if failed:
            raise FabricAuthError
        token = body.get("access_token") if isinstance(body, dict) else None
        lifetime = body.get("expires_in") if isinstance(body, dict) else None
        if (
            not isinstance(token, str)
            or not _TOKEN.fullmatch(token)
            or isinstance(lifetime, bool)
            or not isinstance(lifetime, int | float)
            or not lifetime > 0  # NaN is refused too
        ):
            raise FabricAuthError
        return token, float(min(lifetime, TOKEN_SECONDS_MAX))

    def _send(self) -> bytearray:
        """The token answer's bytes; `FabricAuthError` for a non-200 or an oversized answer."""
        with (
            httpx.Client(
                timeout=TIMEOUT, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client,
            client.stream(
                "POST",
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret(),
                    "scope": SCOPE,
                },
            ) as response,
        ):
            if response.status_code != 200:
                raise FabricAuthError
            data = bytearray()
            for block in response.iter_bytes():
                data.extend(block)
                if len(data) > TOKEN_RESPONSE_MAX:
                    raise FabricAuthError
            return data


class CheckedTokens[T]:
    """Tokens for one job: `authorize` answers the credentials the job may use now, or raises. It is
    asked before every use, cached token or not, and again after the token was got; the provider of
    other credentials (`provider_of`), and one whose second check did not pass, is never used
    again."""

    def __init__(
        self, authorize: Callable[[], T], provider_of: Callable[[T], Callable[[], str]]
    ) -> None:
        self._authorize, self._provider_of = authorize, provider_of
        self._kept: tuple[T, Callable[[], str]] | None = None

    def __call__(self) -> str:
        kept, self._kept = self._kept, None
        allowed = self._authorize()
        provider = kept[1] if kept and kept[0] == allowed else self._provider_of(allowed)
        token = provider()
        if self._authorize() != allowed:
            raise FabricAuthError
        self._kept = (allowed, provider)
        return token


class FabricClient:
    """Fabric REST calls on `ORIGIN` with a bearer token from `token_provider`, asked again for
    every request: each attempt, poll turn and page, except a write's first attempt when it is given
    the token its apply `begin` checked."""

    def __init__(
        self,
        target: Target,
        token_provider: Callable[[], str],
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if target.host != ORIGIN:
            raise ValueError("unsupported Fabric API origin")
        self.token_provider, self.transport, self.sleep = token_provider, transport, sleep
        self.deadline = utcnow() + timedelta(hours=1)
        self.platform = "fabric"

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, str] | None = None,
        execution: bool = False,
        read: bool = False,
        token: str | None = None,
    ) -> dict[str, Any]:
        """One request; only reads are retried (on 429, honouring `Retry-After` up to 3 times).
        A given `token` (the one an apply's `begin` checked before the step's intent) is used for
        the first attempt instead of asking `token_provider`; any later attempt asks it again."""
        if not path.startswith("/v1/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("invalid canonical Fabric path")
        for attempt in range(4):
            if utcnow() >= self.deadline:
                raise TimeoutError("Fabric operation deadline exceeded")
            if attempt or token is None:
                token = self.token_provider()
            with (
                httpx.Client(
                    base_url=ORIGIN,
                    timeout=TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    method,
                    path,
                    json=body,
                    params=params,
                    headers={"Authorization": "Bearer " + token},
                ) as response,
            ):
                data = bytearray()
                for block in response.iter_bytes():
                    data.extend(block)
                    if len(data) > RESPONSE_MAX:
                        raise ValueError("Fabric response exceeded its bound")
            if response.status_code == 429 and read and attempt < 3:
                self.pause(response.headers.get("Retry-After", "1"))
                continue
            if response.status_code not in {200, 201, 202, 204}:
                raise FabricAPIError(response.status_code)
            operation = response.headers.get("x-ms-operation-id")
            if response.status_code == 202:
                if not operation and not execution:
                    raise NeedsAttention("Fabric accepted a request without an operation ID")
                operation = str(UUID(operation)) if operation else None
            try:
                decoded = json.loads(data) if data else {}
            except (ValueError, RecursionError):  # a decode error carries the body
                decoded = None
            if not isinstance(decoded, dict):
                raise ValueError("unexpected Fabric response body")
            return {
                "status": response.status_code,
                "body": decoded,
                "operation_id": operation,
                "retry_after": response.headers.get("Retry-After", "1"),
                "location": response.headers.get("Location", ""),
            }
        raise FabricAPIError(429)

    def pause(self, value: str) -> None:
        try:
            seconds = max(0.0, float(value))
        except ValueError:
            raise ValueError("invalid Fabric Retry-After") from None
        if not seconds < 61 or utcnow() + timedelta(seconds=seconds) >= self.deadline:
            raise TimeoutError("Fabric retry delay exceeds operation budget")
        self.sleep(seconds)

    def wait(self, accepted: dict[str, Any], *, result_required: bool = True) -> dict[str, Any]:
        """The result of an accepted request, polling only `/v1/operations/<id>` on `ORIGIN`."""
        if accepted["status"] != 202:
            body: dict[str, Any] = accepted["body"]
            return body
        identity = str(UUID(accepted["operation_id"]))
        delay = accepted.get("retry_after", "1")
        for _ in range(128):
            self.pause(delay)
            result = self.request("GET", f"/v1/operations/{identity}", read=True)
            status = result["body"].get("status")
            if status == "Succeeded":
                if not result_required:
                    return {}
                final: dict[str, Any] = self.request(
                    "GET", f"/v1/operations/{identity}/result", read=True
                )["body"]
                return final
            if status in {"Failed", "Cancelled", "Canceled"}:
                raise RuntimeError("Fabric native operation did not succeed")
            if status not in {"NotStarted", "Running", "Undefined"}:
                raise NeedsAttention("Fabric operation returned an unknown state")
            delay = result["retry_after"]
        raise TimeoutError("Fabric operation polling limit exceeded")

    def workspace(self, workspace: str) -> dict[str, Any]:
        body: dict[str, Any] = self.request("GET", f"/v1/workspaces/{workspace}", read=True)["body"]
        return body

    def items(self, workspace: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(32):
            data = self.request(
                "GET",
                f"/v1/workspaces/{workspace}/items",
                params={"continuationToken": token} if token else None,
                read=True,
            )["body"]
            result.extend(data["value"])
            token = data.get("continuationToken")
            if len(result) > 1000:
                raise ValueError("Fabric workspace inventory exceeded its bound")
            if not token:
                return result
            if token in seen:
                raise ValueError("Fabric pagination token repeated")
            seen.add(token)
        raise ValueError("Fabric pagination limit exceeded")

    def item(self, workspace: str, item_type: str, identity: str) -> dict[str, Any]:
        body: dict[str, Any] = self.request(
            "GET",
            f"/v1/workspaces/{workspace}/{COLLECTIONS[item_type]}/{UUID(identity)}",
            read=True,
        )["body"]
        return body

    def definition(self, workspace: str, item_type: str, identity: str) -> dict[str, Any]:
        result = self.request(
            "POST",
            f"/v1/workspaces/{workspace}/{COLLECTIONS[item_type]}/{UUID(identity)}/getDefinition",
            params={"format": "ipynb"} if item_type == "Notebook" else None,
            read=True,
        )
        value: dict[str, Any] = self.wait(result)["definition"]
        return value

    @staticmethod
    def job_id(location: str, workspace: str, identity: str) -> str:
        """The job instance id of an accepted `RunNotebook`, from its allow-listed `Location`."""
        parsed = urlsplit(location)
        prefix = f"/v1/workspaces/{workspace}/items/{identity}/jobs/instances/"
        job = parsed.path.removeprefix(prefix)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.fabric.microsoft.com"
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(prefix)
            or not _GUID.fullmatch(job)
        ):
            raise NeedsAttention("Fabric job receipt has an unexpected Location")
        return str(UUID(job))
