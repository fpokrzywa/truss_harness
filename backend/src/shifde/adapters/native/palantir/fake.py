"""A stateful in-process Foundry stack for tests and demos (no real Foundry stack exists).

`FakeFoundryStack.transport` is an `httpx.MockTransport` answering the token endpoint, the
Marketplace v3 preview paths, the Ontology v2 reads and the frontend asset, so the real
`PalantirClient` runs against it; `stack.client_factory` is the adapter's `client_factory`.
Installations and jobs are kept per stack: a new adapter over the same stack models a restart.
Fault switches are off by default.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from typing import Any, Literal  # Any: Foundry JSON
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

from shifde.adapters.native.contracts import Target
from shifde.adapters.native.palantir.clients import (
    INSTALLATIONS,
    JOBS,
    TOKEN_PATH,
    FoundryCredentials,
    PalantirClient,
)

FIXTURE_CREDENTIALS = FoundryCredentials("fixture-client", "fixture-not-a-secret")
PUBLISHED_ASSET = b"published frontend asset"


class FakeFoundryStack:
    def __init__(
        self,
        *,
        host: str,
        frontend_origin: str,
        ontology_rid: str,
        object_type: str = "Example",
        asset: bytes = PUBLISHED_ASSET,
        hang_timeout: float = 60.0,
    ) -> None:
        self.host = Target._validate_host(host)
        self.frontend_origin = Target._validate_host(frontend_origin)
        self.ontology_rid, self.object_type = ontology_rid, object_type
        # Foundry places an installation in its parent folder's namespace.
        self.namespace_rid = f"ri.compass.main.namespace.{uuid4()}"
        self.asset, self.hang_timeout = asset, hang_timeout
        self.ontology: dict[str, Any] = {"rid": ontology_rid, "apiName": "example"}
        self.object_type_schema: dict[str, Any] = {
            "apiName": object_type,
            "primaryKey": "id",
            "properties": {"id": {"dataType": {"type": "string"}}},
        }
        self.objects: list[dict[str, Any]] = [{"__primaryKey": "1", "id": "1"}]
        self.installations: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, bool]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.tokens: set[str] = set()
        self.fail_after_accept: (
            Literal["palantir-installation-create", "palantir-job-submit"] | None
        )
        self.fail_after_accept = None
        self.job_failed = False
        self.ontology_override = False
        self.asset_mismatch = False
        self.hang = False
        self._released = threading.Event()
        self.transport = httpx.MockTransport(self.handle)

    @property
    def asset_sha256(self) -> str:
        """The SHA-256 of the published asset (what the `frontend_asset_sha256` binding pins)."""
        return hashlib.sha256(self.asset).hexdigest()

    def client_factory(self, target: Target) -> PalantirClient:
        return PalantirClient(
            target,
            credential_provider=lambda host: FIXTURE_CREDENTIALS,
            transport=self.transport,
            platform="fake",
        )

    def release(self) -> None:
        """Unblock a `hang`ing write (it still raises `ReadTimeout`: accepted, unconfirmed)."""
        self._released.set()

    # ------------------------------------------------------------------ transport
    def _view(self, installation: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(installation)
        if self.ontology_override:
            value["ontologyRid"] = self.ontology_rid
        return value

    def _accepted(self, request: httpx.Request, step: str) -> None:
        """Faults after the platform stored the object: the caller never sees the acceptance."""
        if self.fail_after_accept == step:
            raise httpx.ReadError("connection lost after native acceptance", request=request)
        if self.hang:
            self._released.wait(self.hang_timeout)
            raise httpx.ReadTimeout("native write accepted but never confirmed", request=request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        origin = f"{url.scheme}://{url.netloc}"
        method, path = request.method, request.url.path
        authorized = "authorization" in request.headers
        self.requests.append((method, path, authorized))
        if origin == self.frontend_origin and origin != self.host:
            if authorized or method != "GET":
                return httpx.Response(400)
            return httpx.Response(
                200, content=b"<html>SSO login</html>" if self.asset_mismatch else self.asset
            )
        if origin != self.host:
            return httpx.Response(404)
        if path == TOKEN_PATH and method == "POST":
            return self._token(request)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            return httpx.Response(401)
        if path.startswith("/api/v2/ontologies/") and method == "GET":
            return self._ontology(request, path)
        if path.startswith(INSTALLATIONS) or path.startswith(JOBS):
            if request.url.params.get("preview") != "true":
                return httpx.Response(400)
            return self._marketplace(request, method, path)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if (
            request.headers.get("content-type") != "application/x-www-form-urlencoded"
            or form.get("grant_type") != "client_credentials"
            or not form.get("client_id")
            or not form.get("client_secret")
        ):
            return httpx.Response(401)
        token = f"fake-token-{uuid4().hex}"
        self.tokens.add(token)
        return httpx.Response(
            200, json={"access_token": token, "token_type": "bearer", "expires_in": 3600}
        )

    def _ontology(self, request: httpx.Request, path: str) -> httpx.Response:
        base = f"/api/v2/ontologies/{self.ontology_rid}"
        if path == base:
            return httpx.Response(200, json=self.ontology)
        if path == f"{base}/objectTypes/{self.object_type}":
            return httpx.Response(200, json=self.object_type_schema)
        if path == f"{base}/objects/{self.object_type}":
            size = int(request.url.params.get("pageSize", "100"))
            return httpx.Response(
                200,
                json={"data": self.objects[:size], "totalCount": str(len(self.objects))},
            )
        return httpx.Response(404)

    def _marketplace(self, request: httpx.Request, method: str, path: str) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        if method == "POST" and path == INSTALLATIONS and isinstance(body, dict):
            self.posts.append((path, copy.deepcopy(body)))
            identity = f"ri.marketplace.main.block-set-installation.{uuid4()}"
            self.installations[identity] = {
                "rid": identity,
                "marketplaceRid": body["marketplaceRid"],
                "productId": body["productId"],
                "parentFolderRid": body["parentFolderRid"],
                "namespaceRid": self.namespace_rid,
                "installationSuffix": body["installationSuffix"],
            }
            self._accepted(request, "palantir-installation-create")
            return httpx.Response(200, json=self._view(self.installations[identity]))
        if method == "POST" and path == JOBS and isinstance(body, dict):
            self.posts.append((path, copy.deepcopy(body)))
            (target,) = body["installations"]
            installation = self.installations.get(target["installationRid"])
            if installation is None:
                return httpx.Response(404)
            identity = f"ri.marketplace.main.installation-job.{uuid4()}"
            status = "FAILED" if self.job_failed else "SUCCEEDED"
            self.jobs[identity] = {"rid": identity, "status": status}
            if status == "SUCCEEDED":
                installation["latestTargetVersion"] = {
                    "versionId": target["targetVersion"]["versionId"]
                }
            self._accepted(request, "palantir-job-submit")
            return httpx.Response(200, json=copy.deepcopy(self.jobs[identity]))
        if method == "GET" and path.startswith(INSTALLATIONS + "/"):
            found = self.installations.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=self._view(found)) if found else httpx.Response(404)
        if method == "GET" and path.startswith(JOBS + "/"):
            job = self.jobs.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=copy.deepcopy(job)) if job else httpx.Response(404)
        return httpx.Response(404)
