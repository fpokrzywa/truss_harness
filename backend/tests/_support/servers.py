"""Local test servers as module-scoped fixtures (REST, MCP and OpenAI-compatible fakes).

Modules that need them import the fixtures into their `conftest.py` and list them in `__all__`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shifde.adapters.testservers import McpTestServer, OpenAIFakeServer, RestTestServer


@pytest.fixture(scope="module")
def rest_server() -> Iterator[RestTestServer]:
    with RestTestServer(token="rest-secret-token-0001") as server:
        yield server


@pytest.fixture(scope="module")
def mcp_server() -> Iterator[McpTestServer]:
    with McpTestServer(token="mcp-secret-token-0002") as server:
        yield server


@pytest.fixture(scope="module")
def oai_server() -> Iterator[OpenAIFakeServer]:
    with OpenAIFakeServer(api_key="oai-secret-key-0003") as server:
        yield server
