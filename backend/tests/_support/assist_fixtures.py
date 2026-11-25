"""Shared assist routing fixtures (SP7 design 5.3, 12): the `FakeProvider` scripts that answer the
router model and the specialist runs, and the helpers the routing tests share.

The fake model answers the last user message; the router's user message is
`json.dumps({"text": ..., "candidates": [...]})`, so a marker in the request text picks the
router's reply. The markers hold no rule word, so they never change the rule routing. `ROUTER_SCRIPT`
comes first; `SPECIALIST_SCRIPT` answers everything else (a specialist run) with a fixed text.
`specialist_script` adds scripted `seth.*` tool calls (SP7-4): a marker in the request text makes
the specialist call the given internal functions, and `tool_results` reads what they returned.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any  # Any: JSON bodies and row values
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.assist import catalog
from shifde.assist import service as assist
from shifde.core.ids import uuid7
from tests._support.conntests import execute_job
from tests._support.p2util import tenant_id
from tests._support.p3util import A, fake_model_stack

PICKS_COST = "[[assist-router-k1]]"  # the router answers `cost`
PICKS_NULL = "[[assist-router-k2]]"  # the router answers null
PICKS_DEPLOYMENT = "[[assist-router-k3]]"  # `deployment`: outside the candidates cost/evaluation
PICKS_INVALID = "[[assist-router-k4]]"  # the router answers text that is not JSON
PICKS_EVALUATION = "[[assist-router-k5]]"  # the router answers `evaluation`
SPECIALIST_ANSWER = "Here is what the project's records show."

ROUTER_SCRIPT: list[dict[str, str]] = [
    {"match": PICKS_COST, "reply": '{"specialist": "cost", "reason": "a spend question"}'},
    {"match": PICKS_NULL, "reply": '{"specialist": null, "reason": "no specialist fits"}'},
    {"match": PICKS_DEPLOYMENT, "reply": '{"specialist": "deployment", "reason": "a release"}'},
    {"match": PICKS_INVALID, "reply": "I think the cost specialist fits best."},
    {"match": PICKS_EVALUATION, "reply": '{"specialist": "evaluation", "reason": "a test plan"}'},
]
SPECIALIST_SCRIPT: list[dict[str, str]] = [{"match": "", "reply": SPECIALIST_ANSWER}]
PROJECT = "/workspaces/main/projects/sample"


def assist_model(client: TestClient, h: dict[str, str], org: str = A, **over: Any) -> str:
    """An active model of `org` on the fake provider answering the router and the specialists."""
    ids = fake_model_stack(
        client,
        h,
        org=org,
        config={"script": ROUTER_SCRIPT + SPECIALIST_SCRIPT},
        conn_name=f"assist-route-{uuid7().hex[-8:]}",
        **over,
    )
    return ids["model"]


def send(
    client: TestClient,
    h: dict[str, str],
    body: dict[str, Any],
    org: str = A,
    project: str = PROJECT,
) -> httpx.Response:
    """`POST .../assist/requests`."""
    return client.post(f"{org}{project}/assist/requests", headers=h, json=body)


def sent(
    client: TestClient,
    h: dict[str, str],
    body: dict[str, Any],
    status: int = 201,
    org: str = A,
    project: str = PROJECT,
) -> dict[str, Any]:
    r = send(client, h, body, org, project)
    assert r.status_code == status, r.text
    out: dict[str, Any] = r.json()
    return out


def read(client: TestClient, h: dict[str, str], request_id: str, org: str = A) -> dict[str, Any]:
    r = client.get(f"{org}/assist/requests/{request_id}", headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def request_row(owner: Session, request_id: str) -> dict[str, Any]:
    """The `assist_requests` row (schema owner)."""
    owner.rollback()
    row = owner.execute(
        text("SELECT * FROM shifde.assist_requests WHERE id = :r"), {"r": request_id}
    ).mappings().one()  # fmt: skip
    owner.rollback()
    return dict(row)


def set_status(owner: Session, request_id: str, status: str) -> None:
    """Put the request in `status` (schema owner): a crash left it there."""
    owner.rollback()
    owner.execute(
        text("UPDATE shifde.assist_requests SET status = :s WHERE id = :r"),
        {"s": status, "r": request_id},
    )
    owner.commit()


def audit_of(owner: Session, request_id: str) -> list[tuple[str, dict[str, Any]]]:
    """(action, details) of the request's audit events, oldest first (schema owner)."""
    owner.rollback()
    rows = owner.execute(
        text("SELECT action, details FROM shifde.audit_events WHERE target_id = :r "
             "ORDER BY chain_seq"),
        {"r": str(request_id)},
    ).all()  # fmt: skip
    owner.rollback()
    return [(r[0], dict(r[1] or {})) for r in rows]


def route_jobs(owner: Session, request_id: str) -> list[tuple[Any, ...]]:
    """(id, status, payload) of the request's `assist.route` jobs (schema owner)."""
    owner.rollback()
    rows = owner.execute(
        text("SELECT id, status, payload FROM shifde.jobs WHERE kind = :k "
             "AND payload->>'request_id' = :r ORDER BY id"),
        {"k": assist.ROUTE_JOB_KIND, "r": str(request_id)},
    ).all()  # fmt: skip
    owner.rollback()
    return [tuple(r) for r in rows]


def run_route_job(
    client: TestClient, owner: Session, request_id: str, org_slug: str = "demo-a"
) -> bool:
    """Run the request's queued `assist.route` job as the worker does."""
    app: Any = client.app  # Any: the FastAPI app behind the test client
    job = request_row(owner, request_id)["job_id"]
    assert job is not None
    return execute_job(
        app.state.db,
        app.state.integration,
        tenant_id(owner, org_slug),
        UUID(str(job)),
        kind=assist.ROUTE_JOB_KIND,
        runner=assist.run_route_job,
    )


# ------------------------------------------------------------------ scripted specialist tool calls
TOOLS_DONE = "Here is what the tools returned."
Calls = Sequence[tuple[str, dict[str, Any]]]  # (internal function key, arguments)


def tool_calls_reply(calls: Calls) -> str:
    """One specialist turn calling each internal function through its installed tool."""
    return json.dumps(
        {"tool_calls": [{"name": catalog.tool_artifact_key(k), "arguments": a} for k, a in calls]}
    )


def specialist_script(scripted: Mapping[str, Calls]) -> list[dict[str, str]]:
    """The specialists' fake model with scripted tool calls: a request text holding a marker of
    `scripted` makes the specialist call its tools in one turn; the tools' results (the next user
    message) get `TOOLS_DONE`; anything else gets `SPECIALIST_ANSWER`. The router's replies come
    first, so one model serves both."""
    return [
        *ROUTER_SCRIPT,
        {"match": "Tool results", "reply": TOOLS_DONE},
        *({"match": m, "reply": tool_calls_reply(c)} for m, c in scripted.items()),
        *SPECIALIST_SCRIPT,
    ]


def specialist_model(
    client: TestClient, h: dict[str, str], scripted: Mapping[str, Calls], org: str = A
) -> str:
    """An active model of `org` on the fake provider answering with `specialist_script`."""
    ids = fake_model_stack(
        client,
        h,
        org=org,
        config={"script": specialist_script(scripted)},
        conn_name=f"assist-tools-{uuid7().hex[-8:]}",
    )
    return ids["model"]


def tool_results(owner: Session, run_id: str) -> list[tuple[str, str, Any]]:
    """(tool artifact key, step status, output) of the run's tool calls in order, the last
    attempt of each (schema owner; the output is kept with `capture_content` full)."""
    owner.rollback()
    rows = owner.execute(
        text("SELECT DISTINCT ON (s.seq) s.name, s.status, a.content FROM shifde.run_steps s "
             "LEFT JOIN shifde.run_artifacts a ON a.id = s.output_ref "
             "WHERE s.run_id = :r AND s.kind = 'tool_call' ORDER BY s.seq, s.attempt DESC"),
        {"r": run_id},
    ).all()  # fmt: skip
    owner.rollback()
    return [(r[0], r[1], r[2]) for r in rows]


# ------------------------------------------------- issue #384: runs that hold a request's run key
def specialist_run(
    client: TestClient, h: dict[str, str], agent_id: str, project: str, org: str = A
) -> str:
    """An ordinary run of `agent_id` in the project `main/<project>`, started by `h`; its id."""
    r = client.post(
        f"{org}/runs",
        headers=h,
        json={
            "kind": "agent",
            "agent_id": agent_id,
            "workspace": "main",
            "project": project,
            "input": "an ordinary question",
            "variables": {"assist_request_id": str(uuid7()), "context": ""},
        },
    )
    assert r.status_code in (200, 201, 202), r.text
    return str(r.json()["id"])


def handover_hash(body: Any) -> str:  # Any: the RunCreate of the handover
    """The `request_hash` that `RunService.create` computes for `body`."""
    return hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def take_key(owner: Session, run_id: str, request_id: str, **columns: Any) -> None:
    """Give the run the key of the request's run (schema owner; no client can send it)."""
    owner.rollback()
    sets = ", ".join(f"{c} = :{c}" for c in ("idempotency_key", *columns))
    owner.execute(
        text(f"UPDATE shifde.runs SET {sets} WHERE id = :r"),
        {"idempotency_key": f"assist:{request_id}", "r": run_id, **columns},
    )
    owner.commit()


def run_rows(owner: Session) -> dict[str, dict[str, Any]]:
    """Every run's row of every client, by id (schema owner)."""
    owner.rollback()
    found = owner.execute(text("SELECT * FROM shifde.runs")).mappings().all()
    owner.rollback()
    return {str(r["id"]): dict(r) for r in found}
