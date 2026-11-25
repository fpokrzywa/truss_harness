"""Shortcuts of the appgen tests: the app's integration runtime pointed at an in-process
`FakeDatabricksWorkspace(sample_data=True)`, a Databricks read connection made, activated and
discovered through the routes (or, after `wire_snowflake`, a Snowflake one on
`FakeSnowflakeAccount(sample_data=True)`), a drafting model on the fake provider scripted with
`DRAFT_SCRIPT`, app requests, running their `appgen.draft` job the way `shifde-worker` does, and
a recorder of the provider calls."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.base import ChatRequest, ChatResponse, ConnectionConfig, ExecContext
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace, FakeSnowflakeAccount
from shifde.adapters.model_providers import FakeProvider
from shifde.appgen import service as appgen
from shifde.core.ids import uuid7
from tests._support.appgen_fixtures import DRAFT_SCRIPT, SNOWFLAKE_SAMPLE_TABLES
from tests._support.conntests import discovery_of, execute_job
from tests._support.p2util import post, tenant_id
from tests._support.p3util import A, activate, connection, fake_model_stack

HOST = "ws1.cloud.databricks.com"
TOKEN = "dbx-appgen-test-pat-0001"  # scan-secrets: allow, any non-empty token passes the fake
TABLES = ["main.default.customers", "main.default.orders", "main.sales.invoices"]
SAMPLE = "[[appgen-sample]]"
INVALID = "[[appgen-invalid]]"
PROJECT = "/workspaces/main/projects/sample"
SF_ACCOUNT = "acct1"
SF_TOKEN = "sf-appgen-test-pat-0001"  # scan-secrets: allow, the fake's accepted token
SF_TABLES = list(SNOWFLAKE_SAMPLE_TABLES)
SF_SAMPLE = "[[appgen-sample-snowflake]]"


def wire(
    client: TestClient, serving: dict[str, list[dict[str, str]]] | None = None
) -> FakeDatabricksWorkspace:
    """Route the app's data-platform calls to a fresh sample-data workspace (answering the
    `serving` endpoints' scripts, if given)."""
    fake = FakeDatabricksWorkspace(
        host=HOST, warehouse_id="wh-0001", sample_data=True, serving=serving or {}
    )
    app: Any = client.app  # Any: the FastAPI app behind the test client
    app.state.integration = dataclasses.replace(
        app.state.integration, resolver=lambda host: ["127.0.0.1"], inner=fake.transport()
    )
    return fake


def wire_snowflake(
    client: TestClient,
    *,
    account: str = SF_ACCOUNT,
    cortex: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[FakeSnowflakeAccount, FakeDatabricksWorkspace]:
    """Route the app's data-platform calls to a fresh sample-data Snowflake account (its host,
    answering the `cortex` models' scripts, if given) and a fresh sample-data Databricks workspace
    (every other host)."""
    sf = FakeSnowflakeAccount(
        account=account, user="reader", pat=SF_TOKEN, sample_data=True, cortex=cortex or {}
    )
    dbx = FakeDatabricksWorkspace(host=HOST, warehouse_id="wh-0001", sample_data=True)

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0].lower()
        return sf.handle(request) if host == sf.host else dbx.handle(request)

    app: Any = client.app  # Any: the FastAPI app behind the test client
    app.state.integration = dataclasses.replace(
        app.state.integration,
        resolver=lambda host: ["127.0.0.1"],
        inner=httpx.MockTransport(handle),
    )
    return sf, dbx


def snowflake(
    client: TestClient,
    h: dict[str, str],
    name: str = "warehouse",
    org: str = A,
    *,
    scope: dict[str, str] | None = None,
    discover: bool = True,
    account: str = SF_ACCOUNT,
) -> str:
    """An active Snowflake read connection of `org` (organization-wide unless `scope`) on the
    `wire_snowflake` account, discovered unless told otherwise."""
    made = connection(
        client,
        h,
        {
            "name": name,
            "connection_type": "data_platform",
            "adapter_key": "snowflake",
            "config": {"account": account, "user": "reader", "warehouse": "COMPUTE_WH"},
            "auth_method": "bearer",
            "credential": {"value": SF_TOKEN},
            "access_mode": "read",
            "scope": scope or {},
            "read_only_confirmed": True,
        },
        org,
    )
    cid = str(made["id"])
    activate(client, h, cid, org)
    if discover:
        discovery_of(client, h, org, cid)
    return cid


def requests_url(org: str = A, project: str = PROJECT) -> str:
    return f"{org}{project}/app-requests"


def request_url(request_id: str, org: str = A, tail: str = "") -> str:
    return f"{org}/app-requests/{request_id}{tail}"


def databricks(
    client: TestClient,
    h: dict[str, str],
    name: str = "lakehouse",
    org: str = A,
    *,
    scope: dict[str, str] | None = None,
    access_mode: str = "read",
    discover: bool = True,
    active: bool = True,
) -> str:
    """A Databricks connection of `org` (organization-wide unless `scope`), activated and
    discovered unless told otherwise."""
    made = connection(
        client,
        h,
        {
            "name": name,
            "connection_type": "data_platform",
            "adapter_key": "databricks",
            "config": {"host": f"https://{HOST}", "warehouse_id": "wh-0001"},
            "auth_method": "bearer",
            "credential": {"value": TOKEN},
            "access_mode": access_mode,
            "scope": scope or {},
            **({"read_only_confirmed": True} if access_mode == "read" else {}),
        },
        org,
    )
    cid = str(made["id"])
    if active:
        activate(client, h, cid, org)
    if discover:
        discovery_of(client, h, org, cid)
    return cid


def drafting_model(client: TestClient, h: dict[str, str], org: str = A) -> str:
    """An active model of `org` on the fake provider scripted with `DRAFT_SCRIPT`."""
    ids = fake_model_stack(
        client, h, org=org, config={"script": DRAFT_SCRIPT}, conn_name=f"draft-{uuid7().hex[-8:]}"
    )
    return ids["model"]


def body(cid: str, model: str, brief: str = f"Sales app {SAMPLE}", **over: Any) -> dict[str, Any]:
    return {"connection_id": cid, "tables": TABLES, "brief": brief, "model_id": model, **over}


def create(
    client: TestClient,
    h: dict[str, str],
    payload: dict[str, Any],
    org: str = A,
    project: str = PROJECT,
) -> httpx.Response:
    return client.post(requests_url(org, project), headers=h, json=payload)


def created(
    client: TestClient,
    h: dict[str, str],
    payload: dict[str, Any],
    org: str = A,
    project: str = PROJECT,
) -> dict[str, Any]:
    r = create(client, h, payload, org, project)
    assert r.status_code == 202, r.text
    out: dict[str, Any] = r.json()
    return out


def job_of(owner: Session, request_id: str) -> UUID:
    owner.rollback()
    job = owner.execute(
        text("SELECT job_id FROM shifde.appgen_requests WHERE id = :r"), {"r": request_id}
    ).scalar_one()
    owner.rollback()
    return UUID(str(job))


def draft_jobs(owner: Session, tenant: UUID | None = None) -> list[tuple[Any, ...]]:
    """(id, status, payload) of every `appgen.draft` job (of `tenant`, when given)."""
    owner.rollback()
    rows = owner.execute(
        text(
            "SELECT id, status, payload FROM shifde.jobs WHERE kind = :k "
            "AND (CAST(:t AS uuid) IS NULL OR tenant_id = :t) ORDER BY id"
        ),
        {"k": appgen.DRAFT_JOB_KIND, "t": tenant},
    ).all()
    owner.rollback()
    return [tuple(r) for r in rows]


def run_job(client: TestClient, owner: Session, request_id: str, org_slug: str = "demo-a") -> bool:
    """Run the request's queued `appgen.draft` job as the worker does."""
    app: Any = client.app  # Any: the FastAPI app behind the test client
    return execute_job(
        app.state.db,
        app.state.integration,
        tenant_id(owner, org_slug),
        job_of(owner, request_id),
        kind=appgen.DRAFT_JOB_KIND,
        runner=appgen.run_draft_job,
    )


def get(client: TestClient, h: dict[str, str], request_id: str, org: str = A) -> dict[str, Any]:
    r = client.get(request_url(request_id, org), headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def drafted(
    client: TestClient, h: dict[str, str], owner: Session, payload: dict[str, Any], org: str = A
) -> dict[str, Any]:
    """A request created and drafted (revision 1)."""
    made = created(client, h, payload, org)
    assert run_job(client, owner, made["id"], "demo-a" if org == A else "demo-b")
    out = get(client, h, made["id"], org)
    assert out["status"] == "drafted", out
    return out


def add_project(client: TestClient, h: dict[str, str], slug: str, org: str = A) -> str:
    return str(
        post(client, f"{org}/workspaces/main/projects", h, {"slug": slug, "name": slug})["id"]
    )


@dataclass
class Calls:
    """Every provider call of the fake model: the messages sent."""

    messages: list[list[tuple[str, str]]] = field(default_factory=list)


def record_calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    calls = Calls()
    generate = FakeProvider.generate

    def recording(
        self: FakeProvider,
        ctx: ExecContext,
        conn: ConnectionConfig,
        model_key: str,
        request: ChatRequest,
    ) -> ChatResponse:
        calls.messages.append([(m.role, m.content) for m in request.messages])
        return generate(self, ctx, conn, model_key, request)

    monkeypatch.setattr(FakeProvider, "generate", recording)
    return calls
