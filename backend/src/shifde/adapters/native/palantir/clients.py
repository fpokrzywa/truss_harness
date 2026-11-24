"""Bounded Foundry REST client (Marketplace v3 preview, read-only Ontology v2), synchronous.

Every authenticated request goes to `target.host`; the only other request is the unauthenticated
frontend asset fetch. No redirects, ambient proxy settings, retries or unbounded responses. The
client secret and the token never appear in an error, a `repr` or a receipt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal  # Any: decoded Foundry JSON bodies
from urllib.parse import quote

import httpx

from shifde.adapters.native.contracts import FoundryMarketplaceSpec, Target, digest, utcnow
from shifde.adapters.native.palantir.source import Bindings, object_type, rid

TIMEOUT = httpx.Timeout(30, connect=5)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOKEN_PATH = "/multipass/api/oauth2/token"
INSTALLATIONS = "/api/v3/platform/marketplaceInstallations"
JOBS = "/api/v3/platform/marketplaceInstallationJobs"


@dataclass(frozen=True)
class FoundryCredentials:
    """An OAuth2 client-credentials pair; `repr` hides the secret."""

    client_id: str
    client_secret: str = field(repr=False)


class PalantirClient:
    def __init__(
        self,
        target: Target,
        *,
        credential_provider: Callable[[str], FoundryCredentials],
        transport: httpx.BaseTransport | None = None,
        platform: Literal["palantir", "fake"] = "palantir",
        deadline: datetime | None = None,
    ) -> None:
        self.target, self.credential_provider, self.transport = (
            target,
            credential_provider,
            transport,
        )
        self.platform: Literal["palantir", "fake"] = platform
        self.deadline = deadline
        self._token: str | None = None

    def __repr__(self) -> str:
        return f"PalantirClient(host={self.target.host!r}, platform={self.platform!r})"

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
    ) -> bytes:
        if self.deadline and utcnow() >= self.deadline:
            raise TimeoutError("Foundry execution deadline exceeded")
        with (
            httpx.Client(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=TIMEOUT,
            ) as client,
            client.stream(
                method, url, headers=headers, params=params, json=body, data=form
            ) as response,
        ):
            if response.status_code not in (200, 201):
                # The body may hold native metadata; never surface it.
                raise RuntimeError("Foundry request failed; inspect native audit logs")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("Foundry response exceeded budget")
            return bytes(data)

    @staticmethod
    def _object(data: bytes) -> dict[str, Any]:
        try:
            value = json.loads(data)
        except ValueError:
            raise ValueError("invalid Foundry response") from None
        if not isinstance(value, dict):
            raise ValueError("invalid Foundry response")
        return value

    def _bearer(self) -> str:
        """One client-credentials token per client instance, minted on first use."""
        if self._token is None:
            credentials = self.credential_provider(self.target.host)
            try:
                body = self._object(
                    self._send(
                        "POST",
                        self.target.host + TOKEN_PATH,
                        form={
                            "grant_type": "client_credentials",
                            "client_id": credentials.client_id,
                            "client_secret": credentials.client_secret,
                        },
                    )
                )
            except (RuntimeError, ValueError):
                raise PermissionError("Foundry native credential unavailable") from None
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise PermissionError("Foundry native credential unavailable")
            self._token = token
        return self._token

    def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        body: dict[str, Any] | None = None,
        *,
        preview: bool,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/api/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("invalid Foundry API path")
        if preview and self.target.bindings.get("allow_preview_apis") != "true":
            raise PermissionError("Foundry preview APIs are not allowed for this target")
        query = {**(params or {}), **({"preview": "true"} if preview else {})}
        return self._object(
            self._send(
                method,
                self.target.host + path,
                headers={"Authorization": "Bearer " + self._bearer()},
                params=query or None,
                body=body,
            )
        )

    # ------------------------------------------------------------------ Marketplace (preview)
    def create_installation(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", INSTALLATIONS, body, preview=True)

    def get_installation(self, installation: str) -> dict[str, Any]:
        identity = quote(rid(installation, "block-set-installation"), safe="")
        return self.request("GET", f"{INSTALLATIONS}/{identity}", preview=True)

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", JOBS, payload, preview=True)

    def get_job(self, job: str) -> dict[str, Any]:
        return self.request(
            "GET", f"{JOBS}/{quote(rid(job, 'installation-job'), safe='')}", preview=True
        )

    # ------------------------------------------------------------------ Ontology (read-only)
    def _ontology_path(self, bindings: Bindings) -> str:
        return f"/api/v2/ontologies/{quote(rid(bindings.ontology_rid, 'ontology'), safe='')}"

    def schema(self, bindings: Bindings) -> str:
        """The digest of the bound object type, after checking the ontology's identity."""
        ontology = self._ontology_path(bindings)
        name = object_type(bindings.object_type)
        if self.request("GET", ontology, preview=False).get("rid") != bindings.ontology_rid:
            raise ValueError("Foundry ontology identity changed")
        schema = self.request(
            "GET", f"{ontology}/objectTypes/{quote(name, safe='')}", preview=False
        )
        if schema.get("apiName") != name:
            raise ValueError("Foundry object type identity changed")
        return digest(schema)

    def read_one_object(self, bindings: Bindings) -> bool:
        ontology = self._ontology_path(bindings)
        name = object_type(bindings.object_type)
        objects = self.request(
            "GET",
            f"{ontology}/objects/{quote(name, safe='')}",
            preview=False,
            params={"pageSize": "1"},
        )
        if not isinstance(objects.get("data"), list) or "totalCount" not in objects:
            raise ValueError("Foundry object read response invalid")
        return True

    # ------------------------------------------------------------------ frontend asset
    def frontend_asset_sha256(self, bindings: Bindings, spec: FoundryMarketplaceSpec) -> str:
        # A separate, unauthenticated request: the bearer never crosses to the frontend origin.
        asset = self._send(
            "GET", bindings.frontend_origin + "/" + quote(spec.frontend_asset_path, safe="/")
        )
        return hashlib.sha256(asset).hexdigest()
