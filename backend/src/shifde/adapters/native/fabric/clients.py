"""Bounded Fabric REST and Entra token clients. Native mutation requests are never replayed.

Egress is limited to the Fabric REST origin and the Entra token endpoint; redirects are never
followed, the process environment's proxy settings are ignored and a long-running operation's
`Location` header is never requested (FDE SDK `shifde_fabric/clients.py`, 1fd902b).
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
    """Entra client-credentials tokens for the Fabric API, cached in memory until near expiry."""

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
        with httpx.Client(
            timeout=TIMEOUT, follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            response = client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret(),
                    "scope": SCOPE,
                },
            )
        if response.status_code != 200 or len(response.content) > 64 * 1024:
            raise FabricAuthError
        try:
            body = response.json()
        except ValueError:
            raise FabricAuthError from None
        token = body.get("access_token") if isinstance(body, dict) else None
        lifetime = body.get("expires_in") if isinstance(body, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or isinstance(lifetime, bool)
            or not isinstance(lifetime, int | float)
            or lifetime <= 0
        ):
            raise FabricAuthError
        return token, float(lifetime)


class FabricClient:
    """Fabric REST calls on `ORIGIN` with a bearer token from `token_provider`."""

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
    ) -> dict[str, Any]:
        """One request; only reads are retried (on 429, honouring `Retry-After` up to 3 times)."""
        if not path.startswith("/v1/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("invalid canonical Fabric path")
        for attempt in range(4):
            if utcnow() >= self.deadline:
                raise TimeoutError("Fabric operation deadline exceeded")
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
            decoded = json.loads(data) if data else {}
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
