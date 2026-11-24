"""Shortcuts of the render, parity and generated-app tests (SP5-5): a `/2` manifest and a
`Resolved` derived from `SAMPLE_DRAFT` (input schemas as accept makes them), the rendered files
written to a directory and their modules imported, a real `databricks.sdk.WorkspaceClient` whose
`requests` traffic goes through a test-only transport adapter to a `FakeDatabricksWorkspace` (no
socket is opened), and SETH's own answer of a tool on the same fake (`_answer` of
`tests/adapters/dataplatforms/test_databricks_appgen_fixtures.py`). SP6-5 adds the
`snowflake.native-app/v1` release of `SNOWFLAKE_SAMPLE_DRAFT` (`SNOWFLAKE_MANIFEST`, bound to
Cortex model `CORTEX_MODEL`)."""

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
from shifde.appgen.render import GENERATOR_VERSION, GENERATOR_VERSIONS
from shifde.appgen.service import AGENT_LIMITS, PARAMETER_SCHEMAS
from shifde.build.tool_runtime import param_type
from shifde.packaging.canonical import digest
from tests._support.appgen_fixtures import (
    SAMPLE_DRAFT,
    SAMPLE_TABLES,
    SNOWFLAKE_SAMPLE_DRAFT,
    SNOWFLAKE_SAMPLE_TABLES,
)
from tests.adapters.dataplatforms.helpers import HOST, PAT, WAREHOUSE, conn, ctx_for

ENDPOINT = "agent-endpoint"
CORTEX_MODEL = "mistral-large2"
SNOWFLAKE_KIND = "snowflake.native-app/v1"
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
        "target_kind": "databricks.app/v1",
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


def snowflake_manifest_of(draft: dict[str, Any]) -> dict[str, Any]:  # Any: a `/2` manifest
    """The `snowflake.native-app/v1` manifest of `draft`: its data-read tables in mixed case, one
    twice."""
    return {
        **manifest_of(draft),
        "target_kind": SNOWFLAKE_KIND,
        "app": {"generator_version": GENERATOR_VERSIONS[SNOWFLAKE_KIND]},
        "data_reads": [
            {
                "connection_id": "00000000-0000-0000-0000-00000000c002",
                "tables": [
                    t.upper() if i == 0 else t for i, t in enumerate(SNOWFLAKE_SAMPLE_TABLES)
                ]
                + [SNOWFLAKE_SAMPLE_TABLES[1]],
            }
        ],
    }


SNOWFLAKE_MANIFEST = snowflake_manifest_of(SNOWFLAKE_SAMPLE_DRAFT)
SNOWFLAKE_DIGEST = digest(SNOWFLAKE_MANIFEST)
SNOWFLAKE_RESOLVED = resolved_of(SNOWFLAKE_SAMPLE_DRAFT, endpoint=CORTEX_MODEL)


def write_files(directory: Path, files: dict[str, bytes]) -> Path:
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return directory


def _clear_siblings() -> None:
    for name in [n for n in sys.modules if n == "agent_loop" or n.startswith("backend_")]:
        sys.modules.pop(name, None)


def load_module(path: Path, name: str) -> ModuleType:
    """Import `path` as module `name`, with its directory first on `sys.path` while it loads (the
    generated `app.py` imports its siblings `agent_loop` and `backend_databricks`)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parent))
    _clear_siblings()
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
        _clear_siblings()
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
