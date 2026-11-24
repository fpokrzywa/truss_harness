"""Shared assist routing fixtures (SP7 design 5.3, 12): the `FakeProvider` scripts that answer the
router model and the specialist runs, and the helpers the routing tests share.

The fake model answers the last user message; the router's user message is
`json.dumps({"text": ..., "candidates": [...]})`, so a marker in the request text picks the
router's reply. The markers hold no rule word, so they never change the rule routing. `ROUTER_SCRIPT`
comes first; `SPECIALIST_SCRIPT` answers everything else (a specialist run) with a fixed text.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies and row values
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.assist import service as assist
from shifde.core.ids import uuid7
from tests._support.conntests import execute_job
from tests._support.p2util import tenant_id
from tests._support.p3util import A, fake_model_stack

PICKS_COST = "[[assist-router-k1]]"  # the router answers `cost`
PICKS_NULL = "[[assist-router-k2]]"  # the router answers null
PICKS_DEPLOYMENT = "[[assist-router-k3]]"  # `deployment`: outside the candidates cost/evaluation
PICKS_INVALID = "[[assist-router-k4]]"  # the router answers text that is not JSON
SPECIALIST_ANSWER = "Here is what the project's records show."

ROUTER_SCRIPT: list[dict[str, str]] = [
    {"match": PICKS_COST, "reply": '{"specialist": "cost", "reason": "a spend question"}'},
    {"match": PICKS_NULL, "reply": '{"specialist": null, "reason": "no specialist fits"}'},
    {"match": PICKS_DEPLOYMENT, "reply": '{"specialist": "deployment", "reason": "a release"}'},
    {"match": PICKS_INVALID, "reply": "I think the cost specialist fits best."},
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
