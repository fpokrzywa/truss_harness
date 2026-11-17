"""`shifde-admin dev-sample-pipeline`: seed the reference pipeline `customer-risk-review` (P6, build
prompt 07 end-to-end example) into a development organization.

It drives the PUBLIC API in-process (login as the org administrator, then ordinary requests), so
everything it creates went through the same validation, permission checks and audit as a
hand-made pipeline: a secret + active PostgreSQL connection to the `shifde_sample` database
(`shifde-admin dev-sample-data`), an active REST connection to the local sink
(`shifde-admin dev-rest-sink`), a deterministic fake model, a published prompt and agent, and the
published pipeline. The command is idempotent (existing objects are reused by name / key).

Credentials are read from `.devdata/` and are never printed or logged.
"""

from __future__ import annotations

import importlib
import secrets as pysecrets
from dataclasses import dataclass
from typing import Any  # Any: JSON API documents
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from shifde.adapters.testservers import RestTestServer
from shifde.core.devenv import append_dev_env, read_dev_env
from shifde.core.settings import DEV_USERS_FILE, Settings

PIPELINE_KEY = "customer-risk-review"
DEFAULT_REST_PORT = 8765
FAKE_SCRIPT = [
    {
        "match": "open_requests",
        "reply": "Risk summary: several customers have multiple open service requests; "
        "recommend a proactive outreach.",
    }
]


class SeedError(RuntimeError):
    """A readable problem with the environment (missing seed data, API refusal)."""


@dataclass(frozen=True)
class SeededPipeline:
    pipeline_id: str
    version_id: str
    connection_ids: dict[str, str]
    agent_id: str
    created: bool
    rest_url: str


def reference_pipeline_spec(pg: str, rest: str, agent_ref: str) -> dict[str, Any]:  # Any: JSON
    """db_query -> validate -> transform -> agent -> condition -> approval -> sink (connection =
    the designated side effect) + sink (run_artifact = the stored result)."""
    return {
        "schema_version": "1",
        "params": {
            "type": "object",
            "required": ["region", "threshold"],
            "properties": {
                "region": {"type": "string", "title": "Region"},
                "threshold": {"type": "integer", "title": "Open requests per customer"},
            },
            "additionalProperties": False,
        },
        "policies": {"max_parallel": 4, "deadline_seconds": 900, "on_failure": "fail_fast"},
        "nodes": [
            {
                "id": "src",
                "type": "db_query",
                "config": {
                    "connection_id": pg,
                    "sql": "SELECT customer_id, status, priority, region FROM service_requests "
                    "WHERE region = %(region)s ORDER BY id",
                    "args": {"region": "$params.region"},
                    "max_rows": 500,
                },
                "inputs": {},
            },
            {
                "id": "check",
                "type": "validate",
                "config": {"schema": {"$ref": "#/schemas/Row"}, "on_invalid": "fail"},
                "inputs": {"data": "src.rows"},
            },
            {
                "id": "prep",
                "type": "transform",
                "config": {
                    "operations": [
                        {"op": "filter", "expression": "row.status == 'open'"},
                        {"op": "select", "fields": ["customer_id", "priority"]},
                        {
                            "op": "aggregate",
                            "group_by": ["customer_id"],
                            "fn": "count",
                            "as": "open_requests",
                        },
                    ]
                },
                "inputs": {"data": "check.data"},
            },
            {
                "id": "rollup",
                "type": "transform",
                "config": {
                    "operations": [
                        {
                            "op": "aggregate",
                            "group_by": [],
                            "fn": "max",
                            "field": "open_requests",
                            "as": "max_open",
                        }
                    ]
                },
                "inputs": {"data": "prep.data"},
            },
            {
                "id": "summ",
                "type": "agent",
                "config": {"agent_version_id": agent_ref},
                "inputs": {"input": "prep.data"},
            },
            {
                "id": "risk",
                "type": "condition",
                "config": {"expression": "inputs.rollup[0].max_open > params.threshold"},
                "inputs": {"rollup": "rollup.data"},
            },
            {
                "id": "gate",
                "type": "approval",
                "config": {
                    "message": "Send the risk summary to the customer-success webhook?",
                    "expires_seconds": 86400,
                },
                "inputs": {"context": "summ.output"},
            },
            {
                "id": "deliver",
                "type": "sink",
                "config": {
                    "target": "connection",
                    "connection_id": rest,
                    "mode": "append",
                    "path_template": "/echo",
                },
                "inputs": {"document": "summ.output"},
            },
            {
                "id": "store",
                "type": "sink",
                "config": {"target": "run_artifact", "mode": "replace"},
                "inputs": {"document": "summ.output"},
            },
        ],
        "edges": [
            {"from": "src", "to": "check"},
            {"from": "check", "to": "prep"},
            {"from": "prep", "to": "rollup"},
            {"from": "prep", "to": "summ"},
            {"from": "rollup", "to": "risk"},
            {"from": "summ", "to": "risk"},
            {"from": "risk", "to": "gate", "when": "true"},
            {"from": "gate", "to": "deliver"},
            {"from": "summ", "to": "store"},
        ],
        "schemas": {
            "Row": {
                "type": "object",
                "required": ["customer_id", "status"],
                "properties": {"customer_id": {"type": "integer"}, "status": {"type": "string"}},
            }
        },
    }


class _Api:
    """A logged-in in-process API client for one organization."""

    def __init__(self, settings: Settings, org: str, email: str, password: str) -> None:
        origin = settings.allowed_origins[0]
        # development-only command: local login is enabled for THIS in-process app only
        local = settings.model_copy(
            update={
                "auth_local_enabled": True,
                "login_rate_per_account_per_minute": 0,
                "log_level": "WARNING",
            }
        )
        # the app factory is a sibling layer of the CLI: loaded dynamically (dev-only tool that
        # drives the public API in-process) instead of a static import against the layer rule
        factory = importlib.import_module("shifde.app").create_app
        self.client = TestClient(
            factory(local), base_url="https://testserver", headers={"Origin": origin}
        )
        self.base = f"/api/v1/orgs/{org}"
        r = self.client.post("/api/auth/local/login", json={"email": email, "password": password})
        if r.status_code != 200:
            raise SeedError(
                f"could not log in as {email} (HTTP {r.status_code}); "
                "is local login enabled (SHIFDE_AUTH_LOCAL_ENABLED) and `dev-seed` run?"
            )
        self.headers = {"X-CSRF-Token": r.json()["csrf_token"]}

    def close(self) -> None:
        self.client.close()

    def call(
        self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201)
    ) -> Any:  # Any
        r = self.client.request(method, f"{self.base}{path}", json=body, headers=self.headers)
        if r.status_code not in ok:
            doc: dict[str, Any] = {}
            if "json" in r.headers.get("content-type", ""):
                doc = r.json()
            problems = "; ".join(
                f"{e.get('path') or e.get('loc')}: {e.get('code') or e.get('type')} "
                f"{e.get('message') or e.get('msg')}"
                for e in (doc.get("errors") or [])[:5]
            )
            raise SeedError(
                f"{method} {path} -> HTTP {r.status_code}: {doc.get('detail', r.text[:200])} "
                f"{problems}".strip()
            )
        return r.json() if r.content else None

    def find(self, path: str, field: str, value: str) -> dict[str, Any] | None:  # Any: JSON
        data = self.call("GET", f"{path}?limit=200")
        items = data["items"] if isinstance(data, dict) else data
        return next((i for i in items if i.get(field) == value), None)


def _ensure_secret(api: _Api, name: str, value: str) -> str:
    found = api.find("/secrets", "name", name)
    if found is not None:
        return str(found["id"])
    return str(api.call("POST", "/secrets", {"name": name, "value": value})["id"])


def _ensure_active(api: _Api, name: str, body: dict[str, Any]) -> str:  # Any: JSON
    row = api.find("/connections", "name", name) or api.call("POST", "/connections", body)
    if row["status"] != "active":
        tested = api.call("POST", f"/connections/{row['id']}/test")
        if tested["outcome"] != "pass":
            raise SeedError(f"connection {name!r} failed its test: {tested.get('detail', '')}")
        fresh = api.call("GET", f"/connections/{row['id']}")
        api.call(
            "POST",
            f"/connections/{row['id']}/status",
            {"status": "active", "row_version": fresh["row_version"]},
        )
    return str(row["id"])


def _ensure_artifact(
    api: _Api,
    kind: str,
    key: str,
    spec: dict[str, Any],
    scope: dict[str, str],  # Any: JSON
) -> tuple[dict[str, Any], bool]:  # Any: JSON
    found = api.find(f"/{kind}", "key", key)
    created = found is None
    art = found or api.call(
        "POST",
        f"/{kind}",
        {"key": key, "name": key.replace("-", " ").title(), "scope": scope, "spec": spec},
    )
    if art["latest_published_version"] is None:
        if not created:  # an earlier, interrupted run left a draft: bring it to the current spec
            draft = api.call("GET", f"/{kind}/{art['id']}/draft")
            api.call(
                "PUT",
                f"/{kind}/{art['id']}/draft",
                {"spec": spec, "row_version": draft["row_version"]},
            )
        api.call("POST", f"/{kind}/{art['id']}/publish", {"change_note": "dev sample"})
        art = api.call("GET", f"/{kind}/{art['id']}")
    return art, created


def _read_password(email: str) -> str:
    if not DEV_USERS_FILE.exists():
        raise SeedError(f"{DEV_USERS_FILE} not found: run `shifde-admin dev-seed` first")
    for line in DEV_USERS_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == email:
            return parts[1]
    raise SeedError(f"no dev user {email!r} in {DEV_USERS_FILE}")


def dev_rest_token() -> str:
    """The sink's bearer token: generated once into `.devdata/dev.env` (never printed)."""
    dev = read_dev_env()
    token = dev.get("SHIFDE_DEV_REST_TOKEN")
    if not token:
        token = pysecrets.token_urlsafe(24)
        append_dev_env({"SHIFDE_DEV_REST_TOKEN": token})
    return token


def seed_sample_pipeline(
    settings: Settings,
    *,
    org: str,
    admin_email: str,
    rest_port: int = DEFAULT_REST_PORT,
) -> SeededPipeline:
    dev = read_dev_env()
    sample_url = dev.get("SHIFDE_SAMPLE_DATABASE_URL")
    if not sample_url:
        raise SeedError("the sample database is not set up: run `shifde-admin dev-sample-data`")
    pg = make_url(sample_url)
    api = _Api(settings, org, admin_email, _read_password(admin_email))
    sink = None
    try:
        tenant_id = next(
            o["id"]
            for o in api.client.get("/api/v1/orgs", headers=api.headers).json()["items"]
            if o["slug"] == org
        )
        scope = {"type": "tenant", "id": str(UUID(str(tenant_id)))}
        pg_secret = _ensure_secret(api, "dev-sample-db-password", str(pg.password))
        pg_conn = _ensure_active(
            api,
            "dev-sample-postgres",
            {
                "name": "dev-sample-postgres",
                "connection_type": "database",
                "adapter_key": "postgres",
                "scope": {},
                "access_mode": "read",
                # the dev sample reader is SELECT-only and read-only by default (B-2430, B-1507)
                "read_only_confirmed": True,
                "auth_method": "basic",
                "secret_id": pg_secret,
                "config": {
                    "host": pg.host,
                    "port": pg.port,
                    "database": pg.database,
                    "user": pg.username,
                },
            },
        )
        # the REST sink must answer once for the connection test (then it is stopped again; run
        # `shifde-admin dev-rest-sink` to receive the pipeline's delivery)
        token = dev_rest_token()
        rest_secret = _ensure_secret(api, "dev-rest-sink-token", token)
        if api.find("/connections", "name", "dev-rest-sink") is None:
            sink = RestTestServer(token=token, port=rest_port).start()
        rest_conn = _ensure_active(
            api,
            "dev-rest-sink",
            {
                "name": "dev-rest-sink",
                "connection_type": "rest",
                "adapter_key": "rest.http",
                "scope": {},
                "access_mode": "read_write",
                "auth_method": "bearer",
                "secret_id": rest_secret,
                "config": {
                    "base_url": f"http://127.0.0.1:{rest_port}",
                    "health_path": "/health",
                },
            },
        )
        model_id = _ensure_fake_model(api)
        prompt, _ = _ensure_artifact(api, "prompts", "customer-risk-prompt", _prompt_spec(), scope)
        agent, _ = _ensure_artifact(
            api, "agents", "customer-risk-agent", _agent_spec(prompt["id"], model_id), scope
        )
        spec = reference_pipeline_spec(pg_conn, rest_conn, f"latest:{agent['id']}")
        pipeline, created = _ensure_artifact(api, "pipelines", PIPELINE_KEY, spec, scope)
        version = api.call("GET", f"/pipelines/{pipeline['id']}")["latest_published_version"]
        return SeededPipeline(
            pipeline_id=str(pipeline["id"]),
            version_id=str(version["id"]),
            connection_ids={"sample_postgres": pg_conn, "rest_sink": rest_conn},
            agent_id=str(agent["id"]),
            created=created,
            rest_url=f"http://127.0.0.1:{rest_port}",
        )
    finally:
        if sink is not None:
            sink.stop()
        api.close()


def _ensure_fake_model(api: _Api) -> str:
    conn = _ensure_active(
        api,
        "dev-fake-model",
        {
            "name": "dev-fake-model",
            "connection_type": "model_api",
            "adapter_key": "fake",
            "scope": {},
            "auth_method": "none",
            "access_mode": "read",
            "config": {"script": FAKE_SCRIPT},
        },
    )
    provider = api.find("/model-providers", "name", "dev-fake-provider") or api.call(
        "POST",
        "/model-providers",
        {"name": "dev-fake-provider", "adapter_key": "fake", "connection_id": conn},
    )
    model = api.find("/models", "model_key", "dev-fake-chat") or api.call(
        "POST",
        "/models",
        {
            "provider_id": provider["id"],
            "model_key": "dev-fake-chat",
            "display_name": "Dev fake chat (deterministic)",
            "capabilities": ["chat"],
            "context_window": 8192,
            "max_output_tokens": 1024,
            "lifecycle": "active",
            "rate_limits": {},
        },
    )
    return str(model["id"])


def _prompt_spec() -> dict[str, Any]:  # Any: JSON
    return {
        "sections": {
            "system": "You are a customer-success analyst. Summarize the risk in the data you get.",
            "context": "Open-request counts per customer: {{message}}",
            "constraints": "Be concise.",
            "output_format": "Plain text.",
        },
        "variables": [{"name": "message", "type": "string", "required": True, "trusted": False}],
        "examples": [],
        "composed_of": [],
        "evaluation_criteria": [],
    }


def _agent_spec(prompt_id: str, model_id: str) -> dict[str, Any]:  # Any: JSON
    return {
        "prompt_version_id": f"latest:{prompt_id}",
        "model": {"model_id": model_id, "fallback_model_ids": []},
        "tool_version_ids": [],
        "skill_version_ids": [],
        "memory": {"mode": "none", "max_turns": 0},
        "limits": {"max_iterations": 3, "max_tool_calls": 1, "max_tokens": 4000, "deadline_s": 120},
        "execution_permissions": {
            "allowed_principal_types": ["user", "service_account"],
            "required_permission": "agent:execute",
        },
        "approval_rules": {"require_approval_for": ["write_non_idempotent", "destructive"]},
        "variables_map": {"message": "{{input}}"},
    }
