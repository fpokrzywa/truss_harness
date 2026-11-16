"""Helpers for the P4 build tests: spec builders and API shortcuts."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests._support.p2util import login, new_user, post, system_role, tenant_id
from tests._support.p3util import A

PASSWORD = "a-long-password-1"


def tenant_scope(owner: Session, slug: str = "demo-a") -> dict[str, str]:
    return {"type": "tenant", "id": str(tenant_id(owner, slug))}


def make_user(
    client: TestClient,
    owner: Session,
    _admin_h: dict[str, str],
    email: str,
    role_key: str,
    passwords: dict[str, str],
    org: str = "demo-a",
) -> None:
    """Create a member holding a system role and register its password in `passwords`. Logs in as
    the org admin itself (and leaves the new user logged in: log back in afterwards)."""
    admin_h = login(client, passwords, f"admin@{org}.local")
    new_user(client, admin_h, org, email, PASSWORD, [str(system_role(owner, role_key))])
    passwords[email] = PASSWORD


def as_user(client: TestClient, passwords: dict[str, str], email: str) -> dict[str, str]:
    return login(client, passwords, email)


def set_tenant_setting(owner: Session, slug: str, key: str, value: Any) -> None:
    import json

    owner.execute(
        text("UPDATE shifde.tenants SET settings = settings || CAST(:v AS jsonb) WHERE slug = :s"),
        {"v": json.dumps({key: value}), "s": slug},
    )
    owner.commit()


def art(
    client: TestClient,
    h: dict[str, str],
    kind: str,
    key: str,
    spec: dict[str, Any] | None,
    scope: dict[str, str],
    *,
    visibility: str = "scope",
    org: str = A,
    status: int = 201,
    name: str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "key": key,
        "name": name or key.replace("-", " ").title(),
        "scope": scope,
        "visibility": visibility,
    }
    if spec is not None:
        body["spec"] = spec
    return post(client, f"{org}/{kind}", h, body, status)


def publish(
    client: TestClient,
    h: dict[str, str],
    kind: str,
    artifact_id: str,
    note: str = "initial",
    org: str = A,
    status: int = 200,
) -> Any:
    return post(client, f"{org}/{kind}/{artifact_id}/publish", h, {"change_note": note}, status)


def edit(
    client: TestClient,
    h: dict[str, str],
    kind: str,
    artifact_id: str,
    spec: dict[str, Any],
    row_version: int = 0,
    org: str = A,
    status: int = 200,
) -> Any:
    r = client.put(
        f"{org}/{kind}/{artifact_id}/draft",
        json={"spec": spec, "row_version": row_version},
        headers=h,
    )
    assert r.status_code == status, f"{r.status_code} {r.text}"
    return r.json()


def codes(response_json: dict[str, Any]) -> set[str]:
    """Validation issue codes of a 422 problem+json or of a validate response."""
    return {str(e.get("type") or e.get("code")) for e in response_json.get("errors", [])}


def prompt_spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "sections": {
            "system": "You are a support assistant.",
            "context": "Customer message: {{message}}",
            "constraints": "Answer in {{language}}.",
            "output_format": "Plain text.",
        },
        "variables": [
            {"name": "message", "type": "string", "required": True, "trusted": False},
            {
                "name": "language",
                "type": "string",
                "required": False,
                "default": "English",
                "trusted": True,
            },
        ],
        "examples": [{"inputs": {"message": "hi"}, "output": "hello"}],
        "composed_of": [],
        "evaluation_criteria": [],
    }
    spec.update(over)
    return spec


def skill_spec(text_md: str = "Always greet the customer.", **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "instructions_md": text_md,
        "required_inputs": [],
        "dependencies": [],
        "applicable_tools": [],
        "validation": {"max_instruction_chars": 2000},
    }
    spec.update(over)
    return spec


def builtin_tool_spec(function_key: str = "util.echo", **over: Any) -> dict[str, Any]:
    from shifde.build.executors import BUILTIN_FUNCTIONS

    fn = BUILTIN_FUNCTIONS[function_key]
    spec: dict[str, Any] = {
        "executor": "builtin.function",
        "binding": {"function_key": function_key},
        "input_schema": fn.input_schema,
        "output_schema": fn.output_schema,
        "side_effect_class": "none",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    spec.update(over)
    return spec


def agent_spec(prompt_ref: str, model_id: str, **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "prompt_version_id": prompt_ref,
        "model": {"model_id": model_id, "fallback_model_ids": []},
        "tool_version_ids": [],
        "skill_version_ids": [],
        "memory": {"mode": "none", "max_turns": 0},
        "limits": {
            "max_iterations": 5,
            "max_tool_calls": 10,
            "max_tokens": 4000,
            "deadline_s": 60,
        },
        "execution_permissions": {
            "allowed_principal_types": ["user"],
            "required_permission": "agent:execute",
        },
        "approval_rules": {"require_approval_for": ["write_non_idempotent", "destructive"]},
        "variables_map": {"message": "{{input}}"},
    }
    spec.update(over)
    return spec
