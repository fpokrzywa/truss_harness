"""Shortcuts of the MCP access tests: an MCP-enabled app, API keys, the settings route, raw
JSON-RPC `tools/call` requests to `/mcp`, published agents and audit rows."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p3util import fake_model_stack
from tests._support.p4util import agent_spec, art, prompt_spec, publish

ORG_A, ORG_B = "demo-a", "demo-b"
A, B = f"/api/v1/orgs/{ORG_A}", f"/api/v1/orgs/{ORG_B}"
ADMIN_A, VIEWER_A, ADMIN_B = "admin@demo-a.local", "viewer@demo-a.local", "admin@demo-b.local"
# the tools resolve a project and assemble a run as the routes do: the workspace must be
# readable, and a run's agent pins a prompt the key must be able to read; `assist_ask` also needs
# what its specialist (`cost`) and the router model call need
TOOL_SCOPES = [
    "mcp:access",
    "workspace:read",
    "project:read",
    "prompt:read",
    "agent:read",
    "agent:execute",
    "run:read",
    "approval:read",
    "packaging:solution:read",
    "delivery:deployment:read",
    "evaluation:run:read",
    "delivery:target:read",
    "delivery:plan:read",
    "tool:read",
    "model:read",
    "model:invoke",
    "usage:read",
    "budget:read",
    "assist:request:read",
    "assist:request:write",
]
V1_TOOLS = {
    "whoami",
    "list_projects",
    "list_agents",
    "start_run",
    "get_run",
    "list_releases",
    "get_release",
    "list_deployments",
    "get_deployment",
    "board_get",
    "assist_ask",
    "assist_get",
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def mcp_settings(settings: Settings, **over: Any) -> Settings:
    return settings.model_copy(
        update={"mcp_enabled": True, "mcp_allowed_hosts": ["testserver"], **over}
    )


@contextmanager
def mcp_client(settings: Settings) -> Iterator[TestClient]:
    """The full app over this test's database; no default `Origin` header (`/mcp` refuses any)."""
    app = create_app(settings)
    try:
        with TestClient(app, base_url="https://testserver") as c:
            yield c
    finally:
        app.state.db.dispose()


def sign_in(c: TestClient, world: World, email: str) -> dict[str, str]:
    c.cookies.clear()
    r = c.post("/api/auth/local/login", json={"email": email, "password": world.passwords[email]})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def user_id(c: TestClient) -> str:
    out: str = c.get("/api/auth/session").json()["user"]["id"]
    return out


def make_key(
    c: TestClient,
    world: World,
    email: str,
    base: str,
    *,
    scopes: list[str] | None = None,
    resource_scope: dict[str, list[str]] | None = None,
    name: str = "mcp key",
) -> tuple[str, str]:
    """A personal key of `email` in the organization at `base`: (id, secret)."""
    h = sign_in(c, world, email)
    body: dict[str, Any] = {
        "name": name,
        "principal_type": "user",
        "principal_id": user_id(c),
        "scopes": scopes or TOOL_SCOPES,
        "expires_in_days": 30,
    }
    if resource_scope is not None:
        body["resource_scope"] = resource_scope
    r = c.post(f"{base}/api-keys", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["api_key"]["id"], r.json()["secret"]


def set_mcp(c: TestClient, world: World, email: str, base: str, **over: Any) -> dict[str, Any]:
    """PUT the organization's MCP settings (enabled with the defaults unless overridden)."""
    h = sign_in(c, world, email)
    body = {"enabled": True, "rate_per_minute": 60, "max_cost_per_call": 1, "allowed_tools": None}
    r = c.put(f"{base}/mcp/settings", json={**body, **over}, headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def rpc(
    c: TestClient,
    key: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    auth = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={**ACCEPT, **auth, **(headers or {})},
    )


def call(
    c: TestClient, key: str, tool: str, args: dict[str, Any] | None = None
) -> tuple[bool, dict[str, Any]]:
    """`tools/call`: (is_error, the JSON document of the text content)."""
    r = rpc(c, key, "tools/call", {"name": tool, "arguments": args or {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    body: dict[str, Any] = json.loads(result["content"][0]["text"])
    return bool(result.get("isError")), body


def ok(c: TestClient, key: str, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    is_error, body = call(c, key, tool, args)
    assert not is_error, body
    return body


def refused(c: TestClient, key: str, tool: str, args: dict[str, Any] | None = None) -> str:
    """The error code of a tool call that must fail."""
    is_error, body = call(c, key, tool, args)
    assert is_error, body
    code: str = body["error"]
    return code


def tenant(owner: Session, slug: str) -> UUID:
    return UUID(
        str(
            owner.execute(
                text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": slug}
            ).scalar_one()
        )
    )


def project(owner: Session, org: str, slug: str = "sample") -> UUID:
    return UUID(
        str(
            owner.execute(
                text(
                    "SELECT p.id FROM shifde.projects p JOIN shifde.tenants t ON t.id = p.tenant_id "
                    "WHERE t.slug = :o AND p.slug = :p"
                ),
                {"o": org, "p": slug},
            ).scalar_one()
        )
    )


def agent(
    c: TestClient,
    h: dict[str, str],
    base: str,
    project_id: UUID,
    key: str = "runner",
    *,
    model: str | None = None,
) -> dict[str, str]:
    """A published agent of `project_id` over `model`, or a new fake model: {agent, version,
    model}."""
    if model is None:
        model = fake_model_stack(c, h, org=base, conn_name=f"fake-{key}")["model"]
    scope = {"type": "project", "id": str(project_id)}
    prompt = art(c, h, "prompts", f"{key}-prompt", prompt_spec(), scope, org=base)
    publish(c, h, "prompts", prompt["id"], org=base)
    spec = agent_spec(f"latest:{prompt['id']}", model)
    spec["execution_permissions"] = {
        "allowed_principal_types": ["user", "service_account"],
        "required_permission": "agent:execute",
    }
    created = art(c, h, "agents", key, spec, scope, org=base)
    version = publish(c, h, "agents", created["id"], org=base)
    return {"agent": created["id"], "version": version["id"], "model": model}


def audit_rows(owner: Session, tenant_id: UUID | None = None) -> list[dict[str, Any]]:
    """Every audit row (of one client when given), oldest first."""
    q = "SELECT tenant_id, action, outcome, details FROM shifde.audit_events"
    params: dict[str, Any] = {}
    if tenant_id is not None:
        q += " WHERE tenant_id = :t"
        params["t"] = tenant_id
    owner.rollback()
    return [
        dict(r) for r in owner.execute(text(q + " ORDER BY occurred_at, id"), params).mappings()
    ]


def mcp_rows(owner: Session, tenant_id: UUID, action: str) -> list[dict[str, Any]]:
    return [r for r in audit_rows(owner, tenant_id) if r["action"] == action]
