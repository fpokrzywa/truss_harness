"""Helpers for the P3 API tests: fixtures for the local protocol targets and API shortcuts."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from shifde.adapters.testservers import RestTestServer
from tests._support.p2util import post

A = "/api/v1/orgs/demo-a"
B = "/api/v1/orgs/demo-b"
ADMIN = "admin@demo-a.local"
VIEWER = "viewer@demo-a.local"
ADMIN_B = "admin@demo-b.local"
PLATFORM = "platform@shifde.local"
REST_TOKEN = "rest-secret-token-0001"
MCP_TOKEN = "mcp-secret-token-0002"
OAI_KEY = "oai-secret-key-0003"


def secret(client: TestClient, h: dict[str, str], name: str, value: str, org: str = A) -> str:
    return str(post(client, f"{org}/secrets", h, {"name": name, "value": value})["id"])


def connection(
    client: TestClient, h: dict[str, str], body: dict[str, Any], org: str = A, status: int = 201
) -> Any:
    base: dict[str, Any] = {"scope": {}, "auth_method": "none", "access_mode": "read", "config": {}}
    merged = {**base, **body}
    if merged.get("secret_id") and merged["access_mode"] == "read":
        merged.setdefault("read_only_confirmed", True)  # B-1507; same setup as tests/connections
    return post(client, f"{org}/connections", h, merged, status)


def activate(client: TestClient, h: dict[str, str], cid: str, org: str = A) -> dict[str, Any]:
    tested = client.post(f"{org}/connections/{cid}/test", headers=h)
    assert tested.status_code == 200 and tested.json()["outcome"] == "pass", tested.text
    row = client.get(f"{org}/connections/{cid}", headers=h).json()
    done = client.post(
        f"{org}/connections/{cid}/status",
        json={"status": "active", "row_version": row["row_version"]},
        headers=h,
    )
    assert done.status_code == 200, done.text
    out: dict[str, Any] = done.json()
    return out


def rest_body(
    server: RestTestServer, secret_id: str | None, name: str = "rest-api"
) -> dict[str, Any]:
    return {
        "name": name,
        "connection_type": "rest",
        "adapter_key": "rest.http",
        "config": {"base_url": server.base_url, "health_path": "/health"},
        "auth_method": "bearer" if secret_id else "none",
        **({"secret_id": secret_id} if secret_id else {}),
    }


def pg_body(url: str, secret_id: str, name: str = "sample-db", **extra: Any) -> dict[str, Any]:
    u = make_url(url)
    return {
        "name": name,
        "connection_type": "database",
        "adapter_key": "postgres",
        "config": {"host": u.host, "port": u.port, "database": u.database, "user": u.username},
        "auth_method": "basic",
        "secret_id": secret_id,
        **extra,
    }


def fake_model_stack(
    client: TestClient,
    h: dict[str, str],
    *,
    model_key: str = "fake-chat",
    lifecycle: str = "active",
    rate_limits: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    conn_name: str = "fake-conn",
    org: str = A,
) -> dict[str, str]:
    """Active fake connection -> provider -> model; returns their ids."""
    conn = connection(
        client,
        h,
        {
            "name": conn_name,
            "connection_type": "model_api",
            "adapter_key": "fake",
            "config": config or {},
        },
        org,
    )
    activate(client, h, conn["id"], org)
    provider = post(
        client,
        f"{org}/model-providers",
        h,
        {"name": f"prov-{conn_name}", "adapter_key": "fake", "connection_id": conn["id"]},
    )
    model = post(
        client,
        f"{org}/models",
        h,
        {
            "provider_id": provider["id"],
            "model_key": model_key,
            "display_name": model_key.title(),
            "capabilities": ["chat"],
            "context_window": 8192,
            "max_output_tokens": 1024,
            "lifecycle": lifecycle,
            "rate_limits": rate_limits or {},
        },
    )
    return {"connection": conn["id"], "provider": provider["id"], "model": model["id"]}


def invoke_body(text: str = "hello world", max_tokens: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body
