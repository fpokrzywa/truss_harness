"""Shortcuts of the packaging tests: an organization's build stack (an active fake model and its
connection, a published prompt and a published agent pinning it, and an evaluation dataset, in
project `main/sample`), a solution, a version body, a Databricks connection, environment bindings
and sealing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies

import httpx
from fastapi.testclient import TestClient

from tests._support.p3util import A, connection, fake_model_stack
from tests._support.p4util import agent_spec, art, edit, prompt_spec, publish

PROJECT = "/workspaces/main/projects/sample"
UNKNOWN_DATASET = "01900000-0000-7000-8000-00000000d5e7"
EVALUATION = {
    "min_accuracy": "0.9",
    "max_p95_latency_ms": 2000,
    "max_total_cost_usd": "1.50",
}


def solutions(org: str = A) -> str:
    return f"{org}{PROJECT}/solutions"


def datasets(org: str = A, project: str = PROJECT) -> str:
    return f"{org}{project}/evaluation-datasets"


def dataset(
    client: TestClient,
    h: dict[str, str],
    name: str = "golden",
    org: str = A,
    project: str = PROJECT,
    cases: list[dict[str, Any]] | None = None,
) -> Any:
    """A dataset of `org`'s project (one `contains` case unless `cases` is given)."""
    body = {
        "name": name,
        "cases": cases
        or [
            {
                "case_id": "c1",
                "input": {"message": "hello"},
                "assertions": [{"id": "a1", "kind": "contains", "value": "hello"}],
            }
        ],
    }
    r = client.post(datasets(org, project), headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()


@dataclass(frozen=True)
class Stack:
    org: str
    project_id: str
    connection: str
    prompt: str
    prompt_v1: str
    prompt_hash: str
    agent: str
    agent_v1: str
    agent_hash: str
    agent_draft: str
    model: str = ""
    provider: str = ""
    dataset: str = ""
    dataset_digest: str = ""


def stack(client: TestClient, h: dict[str, str], org: str = A) -> Stack:
    """`org`'s published prompt and agent (with a saved draft v2) in project `main/sample`."""
    models = fake_model_stack(client, h, org=org)
    project = client.get(f"{org}{PROJECT}", headers=h)
    assert project.status_code == 200, project.text
    scope = {"type": "project", "id": project.json()["id"]}
    prompt = art(client, h, "prompts", "greet", prompt_spec(), scope, org=org)
    prompt_v1 = publish(client, h, "prompts", prompt["id"], org=org)
    spec = agent_spec(f"latest:{prompt['id']}", models["model"])
    agent = art(client, h, "agents", "helper", spec, scope, org=org)
    agent_v1 = publish(client, h, "agents", agent["id"], org=org)
    draft = edit(client, h, "agents", agent["id"], spec, 0, org=org)
    golden = dataset(client, h, org=org)
    return Stack(
        org=org,
        project_id=project.json()["id"],
        connection=models["connection"],
        prompt=prompt["id"],
        prompt_v1=prompt_v1["id"],
        prompt_hash=prompt_v1["spec_hash"],
        agent=agent["id"],
        agent_v1=agent_v1["id"],
        agent_hash=agent_v1["spec_hash"],
        agent_draft=draft["id"],
        model=models["model"],
        provider=models["provider"],
        dataset=golden["id"],
        dataset_digest=golden["digest"],
    )


def solution(
    client: TestClient,
    h: dict[str, str],
    name: str,
    org: str = A,
    target_kind: str = "databricks.app/v1",
) -> Any:
    r = client.post(solutions(org), headers=h, json={"name": name, "target_kind": target_kind})
    assert r.status_code == 201, r.text
    return r.json()


def version_body(
    entrypoint: str,
    *,
    kind: str = "agent",
    connection: str | None = None,
    min_accuracy: str = "0.9",
    evaluation: bool = True,
    dataset: str = UNKNOWN_DATASET,
) -> dict[str, Any]:
    """A version body; its evaluation contract names `dataset` (by default an id of no dataset,
    which a version create refuses once the entrypoint and connections pass)."""
    return {
        "entrypoint_kind": kind,
        "entrypoint_version_id": entrypoint,
        "data_reads": (
            [{"connection_id": connection, "tables": ["main.sales.orders"]}] if connection else []
        ),
        "evaluation": (
            {**EVALUATION, "dataset_id": dataset, "min_accuracy": min_accuracy}
            if evaluation
            else None
        ),
    }


def new_version(
    client: TestClient, h: dict[str, str], solution_id: str, body: dict[str, Any], org: str = A
) -> Any:
    r = client.post(f"{solutions(org)}/{solution_id}/versions", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def databricks_connection(
    client: TestClient,
    h: dict[str, str],
    name: str = "lakehouse",
    org: str = A,
    scope: dict[str, str] | None = None,
) -> str:
    """A (draft) Databricks connection of `org`: organization-wide unless `scope` is given
    (`{"workspace": slug[, "project": slug]}`)."""
    body = {
        "name": name,
        "connection_type": "data_platform",
        "adapter_key": "databricks",
        "config": {"host": f"{name}.cloud.databricks.com"},
        "scope": scope or {},
    }
    return str(connection(client, h, body, org)["id"])


def bindings(
    models: dict[str, str] | None = None,
    connections: dict[str, str] | None = None,
    *,
    served: str = "served-chat",
) -> dict[str, Any]:
    """A seal body: `models` maps a model pin to a provider id, `connections` a connection pin to
    a connection id."""
    return {
        "environment_bindings": {
            "model_bindings": {
                pin: {"provider_id": provider, "model": served}
                for pin, provider in (models or {}).items()
            },
            "connection_bindings": dict(connections or {}),
        }
    }


def releases_url(solution_id: str, version_id: str, org: str = A) -> str:
    return f"{solutions(org)}/{solution_id}/versions/{version_id}/releases"


def seal(
    client: TestClient,
    h: dict[str, str],
    solution_id: str,
    version_id: str,
    body: dict[str, Any],
    org: str = A,
) -> httpx.Response:
    return client.post(releases_url(solution_id, version_id, org), headers=h, json=body)
