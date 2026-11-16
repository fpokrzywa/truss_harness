"""Runtime routes, client A against client B (RT-12, plan recipe step 5, carry_forward B-2125).

Every route of `runtime.api.router` has a case here (`ROUTES` plus `LISTS`, matched against the
router's OpenAPI paths). For each id-bearing route, A's owner asks for B's ids under A's own
organization and gets exactly the answer ids that do not exist get (status and body, no existence
hint); B's rows are unchanged afterwards; and the same request by B's own owner under B's
organization gets another answer (the refusal is not vacuous). A's session with B's slug in the
path gets the unknown-organization answer: the client comes only from
`tenancy.dependencies.resolve_org`. Lists never show B's rows, and B's ids or scope slugs in a
body or a filter answer like unknown ones. Everything runs as `shifde_app` over a minimal app (the
`app` port mounts the router, APP-2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies, row values
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.build import repository as build_repo
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import api
from tests.build.env_support import add_published
from tests.connections.routes import as_user, org
from tests.runtime.routes import runtime_client
from tests.runtime.test_service import Client, _client
from tests.tenancy.conftest import World, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

_VARYING = {"instance", "request_id"}
CRON = "0 * * * *"
STATE_TABLES = ("runs", "approvals", "run_events", "run_steps", "tool_invocations", "jobs",
                "run_artifacts", "pipeline_schedules")  # fmt: skip


@dataclass(frozen=True)
class Side:
    base: str
    email: str
    tenant: UUID
    c: Client
    ontology: UUID  # a published ontology with action `act` on `c.tool`
    sa: UUID  # a service account of the client

    def ids(self) -> dict[str, str]:
        c = self.c
        return {"run": str(c.done), "pending": str(c.pending), "pipe_run": str(c.pipe_run),
                "output": str(c.output), "approval": str(c.approval),
                "pipeline": str(c.pipeline), "ontology": str(self.ontology),
                "agent": str(c.agent), "agent_version": str(c.agent_version),
                "sa": str(self.sa)}  # fmt: skip


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with runtime_client(settings) as c:
        yield c


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def two(owner_db: Database, world: World, people: People) -> Two:
    sides = []
    with owner_db.session() as o:
        for w, email in ((world.a, people.owner_a), (world.b, people.owner_b)):
            c = _client(o, w.tenant)
            action = {"key": "act", "tool_version_id": str(c.tool), "param_mapping": {}}
            onto, version = add_published(
                o, w.tenant, "ontologies", f"onto-{uuid7().hex}", {"actions": [action]}
            )
            build_repo.insert_pins(o, w.tenant, version, [(c.tool, "action_tool")])
            o.execute(text("UPDATE shifde.artifacts SET latest_published_version_id = :v "
                           "WHERE id = :a"), {"v": version, "a": onto})  # fmt: skip
            sides.append(Side(org(w.slug), email, w.tenant, c, onto, w.service_account))
        o.commit()
    return Two(sides[0], sides[1])


def same(r: httpx.Response, ref: httpx.Response) -> None:
    """Refused exactly like an id that does not exist: no existence hint in status or body."""
    assert r.status_code == ref.status_code == 404, (r.status_code, r.text, ref.text)
    assert _body(r) == _body(ref)


def _body(r: httpx.Response) -> Any:  # Any: a JSON body
    body = r.json()
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if k not in _VARYING}
    return body


def _b_state(owner_db: Database, tenant: UUID) -> list[Any]:  # Any: rows
    with owner_db.session() as o:
        out: list[Any] = [
            o.execute(
                text(
                    f"SELECT coalesce(json_agg(t ORDER BY t::text), '[]'::json)::text "
                    f"FROM shifde.{table} t WHERE tenant_id = :t"
                ),
                {"t": tenant},
            ).scalar_one()
            for table in STATE_TABLES
        ]
        out.append(
            o.execute(
                text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t"),
                {"t": tenant},
            ).scalar_one()
        )
        return out


# ---------------------------------------------------------------- the route table
Req = tuple[str, str, dict[str, Any] | None]  # (method, path after the organization, JSON body)
Make = Callable[[dict[str, str], Side], Req]  # (target ids, the caller's own side)


def _run_body(ids: dict[str, str], own: Side) -> dict[str, Any]:
    return {"kind": "agent", "agent_id": ids["agent"], "version_id": ids["agent_version"],
            "workspace": own.c.ws, "project": own.c.proj, "input": "hello"}  # fmt: skip


ROUTES: list[tuple[str, Make]] = [
    ("POST /runs", lambda i, own: ("POST", "/runs", _run_body(i, own))),
    ("POST /ontologies/{ontology_id}/actions/{action_key}/invoke",
     lambda i, own: ("POST", f"/ontologies/{i['ontology']}/actions/act/invoke",
                     {"object_pk": "1", "workspace": own.c.ws, "project": own.c.proj})),
    ("GET /runs/{run_id}", lambda i, own: ("GET", f"/runs/{i['run']}", None)),
    ("GET /runs/{run_id}/steps", lambda i, own: ("GET", f"/runs/{i['run']}/steps", None)),
    ("GET /runs/{run_id}/events", lambda i, own: ("GET", f"/runs/{i['run']}/events", None)),
    ("GET /runs/{run_id}/result", lambda i, own: ("GET", f"/runs/{i['run']}/result", None)),
    ("POST /runs/{run_id}/cancel", lambda i, own: ("POST", f"/runs/{i['pending']}/cancel", None)),
    ("GET /runs/{run_id}/nodes", lambda i, own: ("GET", f"/runs/{i['pipe_run']}/nodes", None)),
    ("GET /runs/{run_id}/artifacts/{artifact_id}",
     lambda i, own: ("GET", f"/runs/{i['run']}/artifacts/{i['output']}", None)),
    ("GET /approvals/{approval_id}", lambda i, own: ("GET", f"/approvals/{i['approval']}", None)),
    ("POST /approvals/{approval_id}/decide",
     lambda i, own: ("POST", f"/approvals/{i['approval']}/decide", {"decision": "approve"})),
    ("GET /pipelines/{pipeline_id}/runs",
     lambda i, own: ("GET", f"/pipelines/{i['pipeline']}/runs", None)),
    ("GET /pipelines/{pipeline_id}/schedules",
     lambda i, own: ("GET", f"/pipelines/{i['pipeline']}/schedules", None)),
    ("POST /pipelines/{pipeline_id}/schedules",
     lambda i, own: ("POST", f"/pipelines/{i['pipeline']}/schedules",
                     {"cron": CRON, "run_as_service_account_id": i["sa"]})),
]  # fmt: skip
LISTS = {("GET", "/orgs/{org}/runs"), ("GET", "/orgs/{org}/approvals")}  # see the list tests


def test_every_runtime_route_has_a_cross_client_case() -> None:
    app = FastAPI()
    app.include_router(api.router)
    served = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    covered = {(label.split(" ", 1)[0], "/orgs/{org}" + label.split(" ", 1)[1])
               for label, _ in ROUTES} | LISTS  # fmt: skip
    assert served == covered


def _send(client: TestClient, h: dict[str, str], base: str, req: Req) -> httpx.Response:
    method, path, body = req
    return client.request(method, f"{base}{path}", json=body, headers=h)


def _unknown() -> dict[str, str]:
    return {k: str(uuid4()) for k in ("run", "pending", "pipe_run", "output", "approval",
                                      "pipeline", "ontology", "agent", "agent_version", "sa")}  # fmt: skip


@pytest.mark.parametrize(("label", "make"), ROUTES, ids=[r[0] for r in ROUTES])
def test_a_cannot_reach_bs_objects(
    label: str, make: Make, client: TestClient, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    refused = _send(client, h, two.a.base, make(two.b.ids(), two.a))
    missing = _send(client, h, two.a.base, make(_unknown(), two.a))
    same(refused, missing)
    assert _b_state(owner_db, two.b.tenant) == before
    # positive control: B's own owner gets another answer for the same request on B's ids
    hb = as_user(client, two.b.email)
    control = _send(client, hb, two.b.base, make(two.b.ids(), two.b))
    assert (control.status_code, _body(control)) != (missing.status_code, _body(missing)), label


@pytest.mark.parametrize(("label", "make"), ROUTES, ids=[r[0] for r in ROUTES])
def test_bs_slug_in_the_path_is_unknown_to_a(
    label: str, make: Make, client: TestClient, two: Two, owner_db: Database
) -> None:
    """The client comes only from `resolve_org`: A's session under B's organization slug gets the
    unknown-organization answer for every route, with B's own ids in the request."""
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    refused = _send(client, h, two.b.base, make(two.b.ids(), two.b))
    missing = _send(client, h, org(f"no-such-{uuid7().hex[-12:]}"), make(two.b.ids(), two.b))
    same(refused, missing)
    assert _b_state(owner_db, two.b.tenant) == before


@pytest.mark.parametrize("path", ["/runs", "/approvals"])
def test_bs_slug_is_unknown_to_a_for_the_lists(path: str, client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    same(client.get(f"{two.b.base}{path}", headers=h),
         client.get(f"{org('no-such-' + uuid7().hex[-12:])}{path}", headers=h))  # fmt: skip


def _ids(r: httpx.Response) -> set[str]:
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


def test_lists_never_show_bs_rows(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    a, b = two.a.c, two.b.c
    b_runs = {str(x) for x in (b.done, b.pending, b.waiting, b.pipe_run)}
    runs = _ids(client.get(f"{two.a.base}/runs", headers=h))
    assert {str(a.done), str(a.pending), str(a.waiting)} <= runs and not runs & b_runs
    for status in ("pending", "approved", "all"):
        got = client.get(f"{two.a.base}/approvals", params={"status": status}, headers=h)
        if got.status_code == 200:
            assert str(b.approval) not in _ids(got)
    pending = _ids(client.get(f"{two.a.base}/approvals", headers=h))
    assert str(a.approval) in pending and str(b.approval) not in pending
    # B's run as a filter lists nothing of B's (like an unknown run)
    for key in ("run_id", "tree_run_id"):
        crossed = client.get(f"{two.a.base}/approvals", params={key: str(b.waiting)}, headers=h)
        unknown = client.get(f"{two.a.base}/approvals", params={key: str(uuid4())}, headers=h)
        assert (crossed.status_code, _body(crossed)) == (unknown.status_code, _body(unknown))
        assert str(b.approval) not in crossed.text
    for key, value in (("agent_id", str(b.agent)), ("initiated_by", str(b.admin))):
        crossed = client.get(f"{two.a.base}/runs", params={key: value}, headers=h)
        assert _ids(crossed) == set()
    # B's workspace or project slug as a filter answers like an unknown slug
    for key, value in (("workspace", b.ws), ("project", b.proj)):
        crossed = client.get(f"{two.a.base}/runs", params={key: value}, headers=h)
        unknown = client.get(f"{two.a.base}/runs", params={key: f"w-{uuid4().hex}"}, headers=h)
        assert crossed.status_code == unknown.status_code, (crossed.text, unknown.text)
        assert _body(crossed) == _body(unknown)
    # the pipeline-runs list of A's pipeline shows only A's run
    assert _ids(client.get(f"{two.a.base}/pipelines/{a.pipeline}/runs", headers=h)) == {
        str(a.pipe_run)
    }


def _post(client: TestClient, h: dict[str, str], url: str, body: dict[str, Any]) -> httpx.Response:
    return client.post(url, json=body, headers=h)


def test_bs_ids_inside_as_own_requests_answer_like_unknown_ones(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    """A's own run, pipeline or agent combined with B's artifact, version, service account or
    scope slug: the answer an unknown id gets, and B's rows are unchanged."""
    h = as_user(client, two.a.email)
    a, b = two.a, two.b
    before = _b_state(owner_db, b.tenant)
    url = a.base

    def pair(make: Callable[[str], httpx.Response], b_value: str, unknown: str) -> None:
        crossed, missing = make(b_value), make(unknown)
        assert crossed.status_code == missing.status_code, (crossed.text, missing.text)
        assert crossed.status_code in (404, 422), crossed.text
        assert _body(crossed) == _body(missing)

    pair(lambda x: client.get(f"{url}/runs/{a.c.done}/artifacts/{x}", headers=h),
         str(b.c.output), str(uuid4()))  # fmt: skip
    body = {"cron": CRON, "run_as_service_account_id": str(a.sa)}
    sched = f"{url}/pipelines/{a.c.pipeline}/schedules"
    pair(lambda x: _post(client, h, sched, {**body, "run_as_service_account_id": x}),
         str(b.sa), str(uuid4()))  # fmt: skip
    pair(lambda x: _post(client, h, sched, {**body, "version_policy": "pinned",
                                            "pipeline_version_id": x}),
         str(b.c.agent_version), str(uuid4()))  # fmt: skip
    run = _run_body(a.ids(), a)
    pair(lambda x: _post(client, h, f"{url}/runs", {**run, "version_id": x}),
         str(b.c.agent_version), str(uuid4()))  # fmt: skip
    pair(lambda x: _post(client, h, f"{url}/runs", {**run, "workspace": x}),
         b.c.ws, f"w-{uuid4().hex}")  # fmt: skip
    pair(lambda x: _post(client, h, f"{url}/runs", {**run, "project": x}),
         b.c.proj, f"p-{uuid4().hex}")  # fmt: skip
    invoke = f"{url}/ontologies/{a.ontology}/actions/act/invoke"
    pair(lambda x: _post(client, h, invoke, {"object_pk": "1", "workspace": x,
                                             "project": a.c.proj}),
         b.c.ws, f"w-{uuid4().hex}")  # fmt: skip
    pair(lambda x: _post(client, h, invoke, {"object_pk": "1", "workspace": a.c.ws,
                                             "project": a.c.proj, "version_id": x}),
         str(b.c.agent_version), str(uuid4()))  # fmt: skip
    assert _b_state(owner_db, b.tenant) == before


def test_a_run_started_through_the_route_lies_in_the_callers_client(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    r = client.post(f"{two.a.base}/runs", json=_run_body(two.a.ids(), two.a),
                    headers={**h, "Idempotency-Key": "k-1"})  # fmt: skip
    assert r.status_code == 202, r.text
    with owner_db.session() as o:
        tenant = o.execute(
            text("SELECT tenant_id FROM shifde.runs WHERE id = :r"), {"r": r.json()["id"]}
        ).scalar_one()
    assert tenant == two.a.tenant
    # the same key under B (B's owner, B's own request) is B's own run, not A's replay
    hb = as_user(client, two.b.email)
    rb = client.post(f"{two.b.base}/runs", json=_run_body(two.b.ids(), two.b),
                     headers={**hb, "Idempotency-Key": "k-1"})  # fmt: skip
    assert rb.status_code == 202 and rb.json()["id"] != r.json()["id"], rb.text
    assert _b_state(owner_db, two.b.tenant) != before  # B's own start changed B only
