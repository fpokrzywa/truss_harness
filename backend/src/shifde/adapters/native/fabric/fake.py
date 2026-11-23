"""A stateful in-process Fabric REST server for tests and demos (no real workspace exists).

`FakeFabricWorkspace.transport` is an `httpx.MockTransport`, and `client_factory` hands out a
`FabricClient` over it with a placeholder token and `platform = "fake"`, so every receipt produced
over the fake says so. Items are kept per workspace id, definitions per item id; Notebook writes
answer 202 with an operation id and an untrusted `Location`, a Lakehouse create answers 201. A
`RunNotebook` job "runs" the notebook: it ends `Completed` with the digest its first cell exits
with as `exitValue`, or `Failed` when that cell is missing or malformed. Fault switches are off
by default; the fakes prove nothing about the real platform (ported from the FDE SDK
`tests/adapters/fabric_fake.py`, 1fd902b).
"""

from __future__ import annotations

import base64
import copy
import json
import re
import threading
from typing import Any  # Any: Fabric REST JSON
from uuid import uuid4

import httpx

from shifde.adapters.native.contracts import Target
from shifde.adapters.native.fabric.clients import FabricClient
from shifde.adapters.native.fabric.source import COLLECTIONS

HOST = "api.fabric.microsoft.com"
_EXIT = re.compile(r'^mssparkutils\.notebook\.exit\("([a-f0-9]{64})"\)$')
_TYPES = {collection: item_type for item_type, collection in COLLECTIONS.items()}
_LRO_TYPES = {"Notebook"}


class FakeFabricWorkspace:
    def __init__(self, *, hang_timeout: float = 60.0) -> None:
        self.hang_timeout = hang_timeout
        self.workspaces: set[str] = set()
        self.capacity: dict[str, bool] = {}
        self.items: dict[str, dict[str, dict[str, Any]]] = {}
        self.definitions: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, str, Any]] = []
        self.fail_after_accept: str | None = None
        self.lro_failed = False
        self.job_failed = False
        self.hang = False
        self._released = threading.Event()
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------- setup and inspection
    def add_workspace(self, workspace_id: str | None = None, *, capacity: bool = True) -> str:
        workspace = workspace_id or str(uuid4())
        self.workspaces.add(workspace)
        self.capacity[workspace] = capacity
        self.items.setdefault(workspace, {})
        return workspace

    def seed_foreign_item(self, workspace_id: str, item_type: str, display_name: str) -> str:
        """An item this target does not own (the `foreign_item` fault)."""
        identity = str(uuid4())
        self.items.setdefault(workspace_id, {})[identity] = {
            "id": identity,
            "workspaceId": workspace_id,
            "type": item_type,
            "displayName": display_name,
            "description": "",
        }
        return identity

    def client_factory(self, target: Target) -> FabricClient:
        client = FabricClient(target, lambda: "fake-token", transport=self.transport)
        client.platform = "fake"
        return client

    def release(self) -> None:
        """Unblock a `hang`ing write; it still raises `httpx.ReadTimeout` (unconfirmed)."""
        self._released.set()

    @property
    def writes(self) -> list[tuple[str, str, str, Any]]:
        """The recorded requests that change native state."""
        return [
            r
            for r in self.requests
            if r[0] in ("POST", "PATCH") and not r[2].endswith("/getDefinition")
        ]

    # ------------------------------------------------------------------- the REST server
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.host != HOST:
            raise AssertionError("request left the Fabric API origin")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) <= len("Bearer "):
            raise AssertionError("request carries no bearer token")
        path, method = request.url.path, request.method
        content = request.read()
        body = json.loads(content) if content else None
        self.requests.append((method, request.url.host, path, copy.deepcopy(body)))
        tokens = path.strip("/").split("/")
        if tokens[0] != "v1" or len(tokens) < 2:
            return httpx.Response(404)
        if tokens[1] == "operations" and method == "GET" and len(tokens) in (3, 4):
            return self._operation(tokens)
        if tokens[1] != "workspaces" or len(tokens) < 3 or tokens[2] not in self.workspaces:
            return httpx.Response(404)
        workspace = tokens[2]
        items = self.items[workspace]
        if len(tokens) == 3 and method == "GET":
            capacity = "fake-capacity" if self.capacity.get(workspace) else None
            return httpx.Response(200, json={"id": workspace, "capacityId": capacity})
        collection = tokens[3]
        if collection == "items":
            return self._items(request, method, tokens, workspace, items)
        item_type = _TYPES.get(collection)
        if item_type is None:
            return httpx.Response(404)
        if len(tokens) == 4 and method == "POST":
            return self._create(request, body or {}, workspace, item_type, items)
        identity = tokens[4]
        item = items.get(identity)
        if item is None or item["type"] != item_type:
            return httpx.Response(404)
        if len(tokens) == 5 and method == "GET":
            return httpx.Response(200, json=copy.deepcopy(item))
        if len(tokens) == 5 and method == "PATCH":
            for key in ("displayName", "description"):
                if key in (body or {}):
                    item[key] = (body or {})[key]
            return self._accept(request, item_type, "metadata", copy.deepcopy(item))
        if len(tokens) == 6 and method == "POST" and tokens[5] == "getDefinition":
            definition = self.definitions.get(identity)
            if definition is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"definition": copy.deepcopy(definition)})
        if len(tokens) == 6 and method == "POST" and tokens[5] == "updateDefinition":
            self.definitions[identity] = copy.deepcopy((body or {})["definition"])
            return self._accept(request, item_type, "definition", {})
        return httpx.Response(404)

    def _operation(self, tokens: list[str]) -> httpx.Response:
        operation = self.operations.get(tokens[2])
        if operation is None:
            return httpx.Response(404)
        if self.lro_failed:
            return httpx.Response(200, json={"status": "Failed"}, headers={"Retry-After": "0"})
        if len(tokens) == 4:
            return httpx.Response(200, json=copy.deepcopy(operation["result"]))
        return httpx.Response(200, json={"status": "Succeeded"}, headers={"Retry-After": "0"})

    def _items(
        self,
        request: httpx.Request,
        method: str,
        tokens: list[str],
        workspace: str,
        items: dict[str, dict[str, Any]],
    ) -> httpx.Response:
        if len(tokens) == 4 and method == "GET":
            return httpx.Response(200, json={"value": [copy.deepcopy(i) for i in items.values()]})
        if len(tokens) < 7 or tokens[4] not in items or tokens[5] != "jobs":
            return httpx.Response(404)
        identity = tokens[4]
        if method == "POST" and tokens[6:] == ["RunNotebook", "instances"]:
            job = str(uuid4())
            self.jobs[job] = {"id": job, "workspace": workspace, "item": identity}
            self.jobs[job].update(self._run(identity))
            self._fault(request, "fabric-verification-submit")
            return httpx.Response(
                202,
                headers={
                    "Location": f"https://{HOST}/v1/workspaces/{workspace}/items/{identity}"
                    f"/jobs/instances/{job}",
                    "Retry-After": "0",
                },
            )
        if method == "GET" and len(tokens) == 8 and tokens[6] == "instances":
            job_state = self.jobs.get(tokens[7])
            if job_state is None or job_state["workspace"] != workspace:
                return httpx.Response(404)
            return httpx.Response(
                200, json={k: v for k, v in job_state.items() if k not in ("workspace", "item")}
            )
        return httpx.Response(404)

    def _run(self, identity: str) -> dict[str, Any]:
        """The ended job of the stored notebook: its first cell's exit digest, or `Failed`."""
        if self.job_failed:
            return {"status": "Failed"}
        try:
            parts = self.definitions[identity]["parts"]
            payload = next(p["payload"] for p in parts if p["path"] == "notebook-content.ipynb")
            cell = json.loads(base64.b64decode(payload))["cells"][0]
            source = cell["source"]
            match = _EXIT.fullmatch("".join(source) if isinstance(source, list) else source)
            if match is None or cell["cell_type"] != "code":
                return {"status": "Failed"}
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            return {"status": "Failed"}
        return {"status": "Completed", "exitValue": match.group(1)}

    def _create(
        self,
        request: httpx.Request,
        body: dict[str, Any],
        workspace: str,
        item_type: str,
        items: dict[str, dict[str, Any]],
    ) -> httpx.Response:
        if body.get("creationPayload", {}).get("enableSchemas") is False:
            return httpx.Response(400)
        if any(i["displayName"] == body.get("displayName") for i in items.values()):
            return httpx.Response(409)
        identity = str(uuid4())
        items[identity] = {
            "id": identity,
            "workspaceId": workspace,
            "type": item_type,
            "displayName": body.get("displayName"),
            "description": body.get("description", ""),
        }
        if body.get("definition") is not None:
            self.definitions[identity] = copy.deepcopy(body["definition"])
        return self._accept(request, item_type, "metadata", copy.deepcopy(items[identity]), True)

    def _accept(
        self,
        request: httpx.Request,
        item_type: str,
        phase: str,
        result: dict[str, Any],
        create: bool = False,
    ) -> httpx.Response:
        """The answer to an applied write (after any fault for its step)."""
        self._fault(request, f"{item_type.lower()}-{phase}-submit")
        if item_type not in _LRO_TYPES:
            return httpx.Response(201 if create else 200, json=result)
        operation = str(uuid4())
        self.operations[operation] = {"result": result}
        return httpx.Response(
            202,
            headers={
                "x-ms-operation-id": operation,
                "Location": "https://untrusted.example/redirect",
                "Retry-After": "0",
            },
        )

    def _fault(self, request: httpx.Request, step: str) -> None:
        if self.fail_after_accept == step:
            raise httpx.ReadTimeout("connection lost after native acceptance", request=request)
        if self.hang:
            self._released.wait(self.hang_timeout)
            raise httpx.ReadTimeout("native write accepted but never confirmed", request=request)
