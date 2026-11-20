"""Shortcuts of the packaging tests: an organization's build stack (an active fake model and its
connection, a published prompt and a published agent pinning it, in project `main/sample`), a
solution and a version body."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies

from fastapi.testclient import TestClient

from tests._support.p3util import A, fake_model_stack
from tests._support.p4util import agent_spec, art, edit, prompt_spec, publish

PROJECT = "/workspaces/main/projects/sample"


def solutions(org: str = A) -> str:
    return f"{org}{PROJECT}/solutions"


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
    )


def solution(client: TestClient, h: dict[str, str], name: str, org: str = A) -> Any:
    r = client.post(
        solutions(org), headers=h, json={"name": name, "target_kind": "databricks.app/v1"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def version_body(
    entrypoint: str,
    *,
    kind: str = "agent",
    connection: str | None = None,
    min_accuracy: str = "0.9",
) -> dict[str, Any]:
    return {
        "entrypoint_kind": kind,
        "entrypoint_version_id": entrypoint,
        "data_reads": (
            [{"connection_id": connection, "tables": ["main.sales.orders"]}] if connection else []
        ),
        "evaluation": {
            "dataset_id": "01900000-0000-7000-8000-00000000d5e7",
            "min_accuracy": min_accuracy,
            "max_p95_latency_ms": 2000,
            "max_total_cost_usd": "1.50",
        },
    }


def new_version(
    client: TestClient, h: dict[str, str], solution_id: str, body: dict[str, Any], org: str = A
) -> Any:
    r = client.post(f"{solutions(org)}/{solution_id}/versions", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()
