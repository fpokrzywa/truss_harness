"""Shortcuts of the render, parity and generated-app tests (SP5-5): a `/2` manifest and a
`Resolved` derived from `SAMPLE_DRAFT` (input schemas as accept makes them), the rendered files
written to a directory and their modules imported, a real `databricks.sdk.WorkspaceClient` whose
`requests` traffic goes through a test-only transport adapter to a `FakeDatabricksWorkspace` (no
socket is opened), and SETH's own answer of a tool on the same fake (`_answer` of
`tests/adapters/dataplatforms/test_databricks_appgen_fixtures.py`)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: JSON documents
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
import requests
from requests.structures import CaseInsensitiveDict

from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace
from shifde.appgen.render import GENERATOR_VERSION
from shifde.appgen.service import AGENT_LIMITS, PARAMETER_SCHEMAS
from shifde.build.tool_runtime import param_type
from shifde.packaging.canonical import digest
from tests._support.appgen_fixtures import SAMPLE_DRAFT, SAMPLE_TABLES
from tests.adapters.dataplatforms.helpers import HOST, PAT, WAREHOUSE, conn, ctx_for

ENDPOINT = "agent-endpoint"
SPEC_HASH = "0" * 64


def input_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:  # Any: JSON
    """The input schema accept's `tool_spec` gives these draft parameters."""
    return {
        "type": "object",
        "properties": {p["name"]: dict(PARAMETER_SCHEMAS[p["type"]]) for p in parameters},
        "required": [p["name"] for p in parameters if p["required"]],
        "additionalProperties": False,
    }


def resolved_of(draft: dict[str, Any], *, endpoint: str = ENDPOINT) -> dict[str, Any]:  # Any
    agent = draft["agent"]
    return {
        "agent": {
            "version_id": str(uuid5(NAMESPACE_URL, f"agent:{agent['name']}")),
            "spec_hash": SPEC_HASH,
            "name": agent["name"],
            "instructions": agent["instructions"],
            "limits": dict(AGENT_LIMITS),
        },
        "tools": [
            {
                "version_id": str(uuid5(NAMESPACE_URL, f"tool:{t['name']}")),
                "spec_hash": SPEC_HASH,
                "name": t["name"],
                "description": t["description"],
                "sql": t["sql"],
                "parameters": t["parameters"],
                "input_schema": input_schema(t["parameters"]),
                "max_rows": t["max_rows"],
            }
            for t in sorted(draft["tools"], key=lambda t: t["name"])
        ],
        "model": {"serving_endpoint": endpoint},
        "screens": draft["screens"],
    }


def manifest_of(draft: dict[str, Any]) -> dict[str, Any]:  # Any: a `/2` release manifest
    return {
        "schema_version": "shifde.release/2",
        "app": {"generator_version": GENERATOR_VERSION},
        "screens": draft["screens"],
        "data_reads": [
            {
                "connection_id": "00000000-0000-0000-0000-00000000c001",
                "tables": [t.upper() if i == 0 else t for i, t in enumerate(SAMPLE_TABLES)]
                + [SAMPLE_TABLES[1]],
            }
        ],
    }


SAMPLE_MANIFEST = manifest_of(SAMPLE_DRAFT)
SAMPLE_DIGEST = digest(SAMPLE_MANIFEST)
SAMPLE_RESOLVED = resolved_of(SAMPLE_DRAFT)


def write_files(directory: Path, files: dict[str, bytes]) -> Path:
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return directory


def load_module(path: Path, name: str) -> ModuleType:
    """Import `path` as module `name`, with its directory first on `sys.path` while it loads (the
    generated `app.py` imports its sibling `agent_loop`)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parent))
    sys.modules.pop("agent_loop", None)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
        sys.modules.pop("agent_loop", None)
    return module


class FakeAdapter(requests.adapters.BaseAdapter):
    """A `requests` transport adapter answering from a `FakeDatabricksWorkspace` in process."""

    def __init__(self, fake: FakeDatabricksWorkspace) -> None:
        super().__init__()
        self.fake = fake

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # Any
        body = request.body or b""
        url = str(request.url)
        headers = {**dict(request.headers), "host": httpx.URL(url).host}
        answer = self.fake.handle(
            httpx.Request(
                str(request.method),
                url,
                headers=headers,
                content=body.encode() if isinstance(body, str) else body,
            )
        )
        response = requests.Response()
        response.status_code = answer.status_code
        response._content = answer.content
        response.headers = CaseInsensitiveDict(answer.headers)
        response.url = url
        response.request = request
        response.encoding = "utf-8"
        return response

    def close(self) -> None:
        pass


def _no_socket(*args: Any, **kwargs: Any) -> None:  # Any: socket arguments
    raise AssertionError("the generated-app tests open no socket")


def workspace_client(fake: FakeDatabricksWorkspace, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real `WorkspaceClient` on `fake`: every `requests.Session` of the SDK (the host metadata
    probe included) gets the fake's adapter, and a socket connect fails the test."""
    from databricks.sdk import WorkspaceClient

    adapter = FakeAdapter(fake)
    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)
    monkeypatch.setattr(socket.socket, "connect", _no_socket)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(Path(__file__).with_name("no-such.cfg")))
    return WorkspaceClient(host=f"https://{HOST}", token=PAT, auth_type="pat")


def fake_workspace(
    serving: list[dict[str, str]], faults: set[str] | None = None
) -> FakeDatabricksWorkspace:
    return FakeDatabricksWorkspace(
        host=HOST,
        token=PAT,
        warehouse_id=WAREHOUSE,
        sample_data=True,
        serving={ENDPOINT: serving},
        faults=faults or set(),
    )


def seth_answer(tool: dict[str, Any], arguments: dict[str, Any]) -> Any:  # Any: JSON
    """The tool output as SETH's `tool_runtime` returns it, from the sample-data workspace."""
    fake = FakeDatabricksWorkspace(host=HOST, token=PAT, warehouse_id=WAREHOUSE, sample_data=True)
    schemas = input_schema(tool["parameters"])["properties"]
    types = {p["name"]: param_type(schemas[p["name"]]) for p in tool["parameters"]}
    session = DatabricksAdapter().open(ctx_for(fake), conn())
    try:
        result = session.invoke(
            {
                "sql": tool["sql"],
                "params": {p["name"]: arguments.get(p["name"]) for p in tool["parameters"]},
                "param_types": types,
                "max_rows": tool["max_rows"],
            }
        )
    finally:
        session.close()
    return {**result.result, **({"truncated": True} if result.truncated else {})}
