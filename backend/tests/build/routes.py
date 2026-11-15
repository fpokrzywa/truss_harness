"""build part 4b: the build routes over a minimal FastAPI app (B-1020 pattern).

The app is the connections and models_admin route harness (identity harness, integration runtime)
plus `build.api.router` behind `require_auth`; the `app` port mounts the router (B-501). People and
clients are the tenancy route-test world. SETH's `p4util` shortcuts keep SETH's names (`art`,
`edit`, `publish`, the spec builders); only the way a user is signed in differs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, specs, TestClient options

from fastapi import Depends
from fastapi.testclient import TestClient

from shifde.adapters.runtime import IntegrationRuntime
from shifde.build import api
from shifde.build.executors import BUILTIN_FUNCTIONS
from shifde.connections import api as connections_api
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from shifde.models_admin import api as models_api
from tests.connections.routes import REGISTRY, post
from tests.identity import harness


@contextmanager
def build_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    app.state.integration = IntegrationRuntime(settings=settings, registry=REGISTRY)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(connections_api.router, dependencies=guards)
    app.include_router(models_api.router, dependencies=guards)
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def scope_of(tenant: Any) -> dict[str, str]:  # Any: a UUID
    return {"type": "tenant", "id": str(tenant)}


def art(
    client: TestClient,
    h: dict[str, str],
    base: str,
    kind: str,
    key: str,
    spec: dict[str, Any] | None,
    scope: dict[str, str],
    *,
    visibility: str = "scope",
    status: int = 201,
) -> Any:  # Any: a JSON body
    body: dict[str, Any] = {
        "key": key,
        "name": key.replace("-", " ").title(),
        "scope": scope,
        "visibility": visibility,
    }
    if spec is not None:
        body["spec"] = spec
    return post(client, f"{base}/{kind}", h, body, status)


def publish(
    client: TestClient,
    h: dict[str, str],
    base: str,
    kind: str,
    artifact_id: str,
    note: str = "initial",
    status: int = 200,
) -> Any:  # Any: a JSON body
    return post(client, f"{base}/{kind}/{artifact_id}/publish", h, {"change_note": note}, status)


def edit(
    client: TestClient,
    h: dict[str, str],
    base: str,
    kind: str,
    artifact_id: str,
    spec: dict[str, Any],
    row_version: int = 0,
    status: int = 200,
) -> Any:  # Any: a JSON body
    r = client.put(
        f"{base}/{kind}/{artifact_id}/draft",
        json={"spec": spec, "row_version": row_version},
        headers=h,
    )
    assert r.status_code == status, f"{r.status_code} {r.text}"
    return r.json()


def prompt_spec(**over: Any) -> dict[str, Any]:  # Any: spec JSON
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


def builtin_tool_spec(function_key: str = "util.echo", **over: Any) -> dict[str, Any]:
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


ECHO_OUT = {
    "type": "object",
    "required": ["status", "body"],
    "properties": {"status": {"type": "integer"}, "body": {"type": "object"}},
}


def rest_spec(cid: str, method: str = "GET", side: str = "read", **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "executor": "rest.http",
        "binding": {
            "connection_id": cid,
            "method": method,
            "path_template": "/echo",
            "query_template": {"name": "{name}"},
        },
        "input_schema": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        },
        "output_schema": ECHO_OUT,
        "side_effect_class": side,
        "required_permissions": ["connection:use"],
        "invocation_policy": {"max_calls_per_run": 3},
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    spec.update(over)
    return spec
