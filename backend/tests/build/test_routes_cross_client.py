"""Build routes, client A against client B (plan recipe step 5, build isolation review 2 F4).

Every route of `build.api.router` has a case here (`ROUTES`, matched against the router's OpenAPI
paths). For each, A's session asks for B's ids under A's own organization and gets exactly the
answer ids that do not exist get (same status and body, no existence hint); B's rows are unchanged
afterwards; and a positive control shows the same request by B's own owner is not "not found" (so
the refusal is not vacuous). A's session with B's slug in the path gets the unknown-organization
answer: the client comes only from `tenancy.dependencies.resolve_org`, never from the body.
Everything runs as `shifde_app` over a minimal app (the `app` port mounts the router, B-501).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.build import api
from shifde.build.service import ArtifactService
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.build.conftest import Side as DbSide
from tests.build.routes import (
    agent_spec,
    art,
    builtin_tool_spec,
    edit,
    prompt_spec,
    publish,
    scope_of,
)
from tests.connections.routes import as_user, org
from tests.models_admin.routes import fake_model_stack
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

_VARYING = {"instance", "request_id"}
KINDS = ("prompts", "agents", "tools", "skills", "orchestrations", "pipelines", "ontologies")
ONTO_KEY = "cs.customer"


def same(r: httpx.Response, ref: httpx.Response) -> None:
    """Refused exactly like an id that does not exist: no existence hint in status or body."""
    assert r.status_code == ref.status_code == 404, (r.status_code, r.text, ref.text)
    body, reference = r.json(), ref.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Ids:
    """One client's ids of one kind: its artifact, a version (the draft or a published one)."""

    artifact: str
    version: str
    request: str = ""  # a pending publish request (prompts only), for the decide route


@dataclass(frozen=True)
class Side:
    base: str
    email: str
    tenant: UUID
    ids: dict[str, Ids]
    request: str  # a pending publish request
    connection: str  # a database connection with a discovery


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


def _skill_spec() -> dict[str, Any]:
    return {
        "instructions_md": "Always greet the customer.",
        "required_inputs": [],
        "dependencies": [],
        "applicable_tools": [],
        "validation": {"max_instruction_chars": 2000},
    }


def _onto_spec() -> dict[str, Any]:
    return {
        "namespaces": [{"key": "cs", "label": "Customer service"}],
        "object_types": [
            {
                "key": ONTO_KEY,
                "namespace": "cs",
                "properties": [
                    {"key": k, "type": t, "nullable": False, "classification": "internal"}
                    for k, t in (("id", "integer"), ("name", "string"))
                ],
                "primary_key": ["id"],
                "display_property": "name",
            }
        ],
        "relationships": [],
        "actions": [],
    }


def _put_draft(
    client: TestClient, h: dict[str, str], base: str, kind: str, aid: str, spec: dict[str, Any]
) -> str:
    """A saved draft for `aid` (a new one after a publish); its version id."""
    got = client.get(f"{base}/{kind}/{aid}/draft", headers=h)
    if got.status_code == 200:
        return str(edit(client, h, base, kind, aid, spec, got.json()["row_version"])["id"])
    return str(edit(client, h, base, kind, aid, spec, 0)["id"])


def _side(
    client: TestClient,
    owner_db: Database,
    tenant: UUID,
    slug: str,
    email: str,
    conn: DbSide,
) -> Side:
    base = org(slug)
    h = as_user(client, email)
    scope = scope_of(tenant)
    stack = fake_model_stack(client, h, base, conn_name=f"fake-{slug}")
    prompt = art(client, h, base, "prompts", "p", prompt_spec(), scope)
    pv = publish(client, h, base, "prompts", prompt["id"])
    specs: dict[str, dict[str, Any]] = {
        "prompts": prompt_spec(),
        "tools": builtin_tool_spec(),
        "skills": _skill_spec(),
        "ontologies": _onto_spec(),
        "agents": agent_spec(pv["id"], stack["model"]),
        "orchestrations": {"nodes": [], "edges": []},
        "pipelines": {"nodes": [], "edges": []},
    }
    ids: dict[str, Ids] = {}
    for kind, spec in specs.items():
        a = (
            prompt
            if kind == "prompts"
            else art(client, h, base, kind, f"{kind[:-1]}-x", spec, scope)
        )
        if kind in ("prompts", "tools", "skills", "agents"):
            version = pv if kind == "prompts" else publish(client, h, base, kind, a["id"])
            ids[kind] = Ids(str(a["id"]), str(version["id"]))
            _put_draft(client, h, base, kind, a["id"], spec)
        else:
            ids[kind] = Ids(str(a["id"]), _put_draft(client, h, base, kind, a["id"], spec))
    # a mapping on the ontology's draft, so the mapping routes (check, delete) have an object for
    # the positive control of the client's own owner
    mapped = client.put(
        f"{base}/ontologies/{ids['ontologies'].artifact}/draft/mappings/object_type/{ONTO_KEY}",
        json={
            "target_kind": "object_type",
            "target_key": ONTO_KEY,
            "connection_id": str(conn.connection),
            "relation": {"schema": "public", "name": "customers_x"},
            "column_map": {"id": "id", "name": "name"},
        },
        headers=h,
    )
    assert mapped.status_code == 200, mapped.text
    # a pending publish request: this client requires approval, then a second prompt is published
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.tenants SET settings = settings || "
                "'{\"publish_requires_approval\": true}'::jsonb WHERE id = :t"
            ),
            {"t": tenant},
        )
        o.commit()
    waiting = art(client, h, base, "prompts", "waiting", prompt_spec(), scope)
    pending = client.post(
        f"{base}/prompts/{waiting['id']}/publish", json={"change_note": "n"}, headers=h
    )
    assert pending.status_code == 202, pending.text
    request = str(pending.json()["publish_request"]["id"])
    ids["prompts"] = Ids(ids["prompts"].artifact, ids["prompts"].version, request)
    return Side(base, email, tenant, ids, request, str(conn.connection))


@pytest.fixture
def two(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    sides: tuple[DbSide, DbSide],
) -> Two:
    b = _side(client, owner_db, world.b.tenant, world.b.slug, people.owner_b, sides[1])
    a = _side(client, owner_db, world.a.tenant, world.a.slug, people.owner_a, sides[0])
    return Two(a, b)


# ---------------------------------------------------------------- the route table
Req = tuple[str, str, dict[str, Any] | None]  # (method, path after the organization, JSON body)
Make = Callable[[str, "Ids", "Side"], Req]


def _no_body(method: str, path: str) -> Make:
    return lambda kind, ids, side: (
        method,
        path.format(k=kind, a=ids.artifact, v=ids.version),
        None,
    )


def _body(method: str, path: str, body: Callable[[Ids, Side], dict[str, Any]]) -> Make:
    return lambda kind, ids, side: (
        method,
        path.format(k=kind, a=ids.artifact, v=ids.version),
        body(ids, side),
    )


def _spec_for(kind: str) -> dict[str, Any]:
    return {"orchestrations": {"nodes": [], "edges": []}}.get(kind, {})


COMMON: dict[tuple[str, str], Make] = {
    ("GET", "/orgs/{org}/{k}/{a}"): _no_body("GET", "/{k}/{a}"),
    ("PATCH", "/orgs/{org}/{k}/{a}"): _body("PATCH", "/{k}/{a}", lambda i, s: {"name": "renamed"}),
    ("GET", "/orgs/{org}/{k}/{a}/versions"): _no_body("GET", "/{k}/{a}/versions"),
    ("GET", "/orgs/{org}/{k}/{a}/versions/{v}"): _no_body("GET", "/{k}/{a}/versions/{v}"),
    ("POST", "/orgs/{org}/{k}/{a}/versions/{v}/state"): _body(
        "POST", "/{k}/{a}/versions/{v}/state", lambda i, s: {"state": "deprecated"}
    ),
    ("GET", "/orgs/{org}/{k}/{a}/draft"): _no_body("GET", "/{k}/{a}/draft"),
    ("POST", "/orgs/{org}/{k}/{a}/draft/validate"): _no_body("POST", "/{k}/{a}/draft/validate"),
    ("POST", "/orgs/{org}/{k}/{a}/publish"): _body(
        "POST", "/{k}/{a}/publish", lambda i, s: {"change_note": "x"}
    ),
    ("GET", "/orgs/{org}/{k}/{a}/diff"): _no_body("GET", "/{k}/{a}/diff?from=draft&to=draft"),
    ("POST", "/orgs/{org}/{k}/{a}/rollback"): _body(
        "POST", "/{k}/{a}/rollback", lambda i, s: {"version_id": i.version}
    ),
}

PUT_DRAFT = ("PUT", "/orgs/{org}/{k}/{a}/draft")
SPECIFIC: dict[str, dict[tuple[str, str], Make]] = {
    "prompts": {
        ("POST", "/orgs/{org}/prompts/{a}/render"): _body(
            "POST", "/{k}/{a}/render", lambda i, s: {"version_id": i.version, "variables": {}}
        ),
        ("POST", "/orgs/{org}/prompts/{a}/test"): _body(
            "POST",
            "/{k}/{a}/test",
            lambda i, s: {"version_id": i.version, "variables": {"message": "hi"}},
        ),
        ("GET", "/orgs/{org}/prompts/{a}/test-runs"): _no_body("GET", "/{k}/{a}/test-runs"),
    },
    "tools": {
        ("POST", "/orgs/{org}/tools/{a}/test"): _body(
            "POST", "/{k}/{a}/test", lambda i, s: {"version_id": i.version, "args": {"value": "x"}}
        ),
    },
    "agents": {
        ("POST", "/orgs/{org}/agents/{a}/test"): _body(
            "POST", "/{k}/{a}/test", lambda i, s: {"version_id": i.version, "input": "hi"}
        ),
        ("POST", "/orgs/{org}/agents/{a}/status"): _body(
            "POST", "/{k}/{a}/status", lambda i, s: {"status": "disabled"}
        ),
        ("GET", "/orgs/{org}/agents/{a}/versions/{v}/resolved"): _no_body(
            "GET", "/{k}/{a}/versions/{v}/resolved"
        ),
    },
    "ontologies": {
        ("GET", "/orgs/{org}/ontologies/{a}/versions/{v}/mappings"): _no_body(
            "GET", "/{k}/{a}/versions/{v}/mappings"
        ),
        ("PUT", "/orgs/{org}/ontologies/{a}/draft/mappings/{target_kind}/{target_key}"): _body(
            "PUT",
            "/{k}/{a}/draft/mappings/object_type/" + ONTO_KEY,
            lambda i, s: {
                "target_kind": "object_type",
                "target_key": ONTO_KEY,
                "connection_id": s.connection,
                "relation": {"schema": "public", "name": "customers_x"},
                "column_map": {"id": "id", "name": "name"},
            },
        ),
        ("POST", "/orgs/{org}/ontologies/{a}/draft/mappings/{target_kind}/{target_key}/check"): (
            _no_body("POST", "/{k}/{a}/draft/mappings/object_type/" + ONTO_KEY + "/check")
        ),
        ("DELETE", "/orgs/{org}/ontologies/{a}/draft/mappings/{target_kind}/{target_key}"): (
            _no_body("DELETE", "/{k}/{a}/draft/mappings/object_type/" + ONTO_KEY)
        ),
    },
}


def _routes() -> list[tuple[str, str, str, Make]]:
    """(label, kind, route key, request maker) for every id-bearing route of every kind."""
    out: list[tuple[str, str, str, Make]] = []
    for kind in KINDS:
        put = _body(
            "PUT", "/{k}/{a}/draft", lambda i, s, k=kind: {"spec": _spec_for(k), "row_version": 0}
        )
        table = {**COMMON, PUT_DRAFT: put, **SPECIFIC.get(kind, {})}
        for (method, path), make in table.items():
            out.append((f"{method} {path.replace('{k}', kind)}", kind, f"{method} {path}", make))
    return out


DECIDE = ("POST", "/orgs/{org}/publish-requests/{request_id}/decide")


def _decide(kind: str, ids: Ids, side: Side) -> Req:
    return ("POST", f"/publish-requests/{ids.request}/decide", {"decision": "reject"})


ROUTES = [*_routes(), (f"{DECIDE[0]} {DECIDE[1]}", "prompts", f"{DECIDE[0]} {DECIDE[1]}", _decide)]
UNKNOWN_FREE = {  # routes without a path id: list, create, publish-requests
    ("GET", "/orgs/{org}/{k}"),
    ("POST", "/orgs/{org}/{k}"),
    ("GET", "/orgs/{org}/publish-requests"),
    ("GET", "/tool-executors"),
    ("GET", "/pipelines/node-types"),
}


def test_every_build_route_has_a_cross_client_case() -> None:
    app = FastAPI()
    app.include_router(api.router)
    served = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    covered = {(label.split(" ", 1)[0], label.split(" ", 1)[1]) for label, *_ in ROUTES}
    for method, path in UNKNOWN_FREE:
        for kind in KINDS if "{k}" in path else (None,):
            covered.add((method, path.replace("{k}", kind) if kind else path))
    covered = {
        (m, p.replace("{a}", "{artifact_id}").replace("{v}", "{version_id}")) for m, p in covered
    }
    assert served == covered


def _send(client: TestClient, h: dict[str, str], base: str, req: Req) -> httpx.Response:
    method, path, body = req
    return client.request(method, f"{base}{path}", json=body, headers=h)


def _unknown(kind: str, side: Side) -> Ids:
    return Ids(str(uuid4()), str(uuid4()), str(uuid4()))


def _b_state(owner_db: Database, tenant: UUID) -> list[Any]:  # Any: rows
    with owner_db.session() as o:
        out: list[Any] = []
        for table in (
            "artifacts",
            "artifact_versions",
            "artifact_version_pins",
            "publish_requests",
            "prompt_test_runs",
            "pipeline_version_refs",
            "usage_events",
        ):
            out.append(
                o.execute(
                    text(
                        f"SELECT coalesce(json_agg(t ORDER BY t::text), '[]'::json)::text "
                        f"FROM shifde.{table} t WHERE tenant_id = :t"
                    ),
                    {"t": tenant},
                ).scalar_one()
            )
        out.append(
            o.execute(
                text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t"),
                {"t": tenant},
            ).scalar_one()
        )
        return out


@pytest.mark.parametrize(("label", "kind", "key", "make"), ROUTES, ids=[r[0] for r in ROUTES])
def test_a_cannot_reach_bs_objects(
    label: str,
    kind: str,
    key: str,
    make: Make,
    client: TestClient,
    two: Two,
    owner_db: Database,
) -> None:
    h = as_user(client, two.a.email)
    bs = two.b.ids[kind]
    before = _b_state(owner_db, two.b.tenant)
    refused = _send(client, h, two.a.base, make(kind, bs, two.a))
    missing = _send(client, h, two.a.base, make(kind, _unknown(kind, two.a), two.a))
    same(refused, missing)
    assert _b_state(owner_db, two.b.tenant) == before
    # A's own artifact with B's version id: the unknown-version answer (version routes only)
    if "{v}" in key or "rollback" in key or "test" in key or "render" in key:
        mine = two.a.ids[kind]
        crossed = _send(client, h, two.a.base, make(kind, Ids(mine.artifact, bs.version), two.a))
        unknown = _send(client, h, two.a.base, make(kind, Ids(mine.artifact, str(uuid4())), two.a))
        assert crossed.status_code == unknown.status_code, (crossed.text, unknown.text)
        assert {k: v for k, v in crossed.json().items() if k not in _VARYING} == {
            k: v for k, v in unknown.json().items() if k not in _VARYING
        }
        assert _b_state(owner_db, two.b.tenant) == before
    # positive control: B's own owner reaches its object with the same request
    hb = as_user(client, two.b.email)
    control = _send(client, hb, two.b.base, make(kind, bs, two.b))
    assert control.status_code != 404, (label, control.text)


@pytest.mark.parametrize(("label", "kind", "key", "make"), ROUTES, ids=[r[0] for r in ROUTES])
def test_bs_slug_in_the_path_is_unknown_to_a(
    label: str, kind: str, key: str, make: Make, client: TestClient, two: Two, owner_db: Database
) -> None:
    """The client comes only from `resolve_org`: A's session under B's organization slug gets the
    unknown-organization answer for every route, with B's own ids in the request."""
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    bs = two.b.ids[kind]
    refused = _send(client, h, two.b.base, make(kind, bs, two.b))
    missing = _send(client, h, org(f"no-such-{uuid7().hex[-12:]}"), make(kind, bs, two.b))
    same(refused, missing)
    assert _b_state(owner_db, two.b.tenant) == before


def test_lists_name_only_the_callers_client(client: TestClient, two: Two) -> None:
    h = as_user(client, two.a.email)
    for kind in KINDS:
        listed = client.get(f"{two.a.base}/{kind}", headers=h)
        assert listed.status_code == 200, listed.text
        found = {i["id"] for i in listed.json()["items"]}
        assert two.a.ids[kind].artifact in found and two.b.ids[kind].artifact not in found
    requests = client.get(f"{two.a.base}/publish-requests", headers=h).json()["items"]
    assert [r["id"] for r in requests] == [two.a.request]
    runs = client.get(f"{two.a.base}/prompts/{two.b.ids['prompts'].artifact}/test-runs", headers=h)
    assert runs.status_code == 404


def test_create_takes_the_client_from_the_session_never_the_body_or_a_header(
    client: TestClient, two: Two, world: World, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    body = {"key": "mine", "name": "Mine", "spec": prompt_spec()}
    ghost = client.post(
        f"{two.a.base}/prompts",
        json={**body, "scope": {"type": "tenant", "id": str(uuid7())}},
        headers=h,
    )
    theirs = client.post(
        f"{two.a.base}/prompts", json={**body, "scope": scope_of(world.b.tenant)}, headers=h
    )
    same(theirs, ghost)
    for kind, theirs_id in (("workspace", world.b.ws), ("project", world.b.project)):
        there = client.post(
            f"{two.a.base}/prompts",
            json={**body, "scope": {"type": kind, "id": str(theirs_id)}},
            headers=h,
        )
        nowhere = client.post(
            f"{two.a.base}/prompts",
            json={**body, "scope": {"type": kind, "id": str(uuid7())}},
            headers=h,
        )
        same(there, nowhere)
    forged = client.post(
        f"{two.a.base}/prompts",
        json={**body, "scope": scope_of(world.a.tenant), "tenant_id": str(world.b.tenant)},
        headers={**h, "X-Tenant-Id": str(world.b.tenant), "X-Org": world.b.slug},
    )
    assert forged.status_code in (201, 422), forged.text
    with owner_db.session() as o:
        owners = (
            o.execute(text("SELECT DISTINCT tenant_id FROM shifde.artifacts WHERE key = 'mine'"))
            .scalars()
            .all()
        )
    assert set(owners) <= {world.a.tenant}


def test_the_catalogs_answer_authenticated_callers_only(client: TestClient, two: Two) -> None:
    for path in ("/api/v1/tool-executors", "/api/v1/pipelines/node-types"):
        client.cookies.clear()
        assert client.get(path).status_code == 401
        h = as_user(client, two.a.email)
        a = client.get(path, headers=h)
        h = as_user(client, two.b.email)
        b = client.get(path, headers=h)
        assert a.status_code == b.status_code == 200 and a.json() == b.json()


def test_every_request_gets_its_own_service_for_its_own_client(
    client: TestClient, two: Two, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3 F3: `ArtifactService` caches per scope (`_labels`, `_maturity`, `conns`,
    `models`); the router builds one per request, so two requests never share one."""
    made: list[ArtifactService] = []  # strong references: no id is reused

    class Recording(ArtifactService):
        def __init__(self, scope: Any, runtime: Any) -> None:
            super().__init__(scope, runtime)
            made.append(self)

    monkeypatch.setattr(api, "ArtifactService", Recording)
    for side in (two.a, two.b, two.a):
        h = as_user(client, side.email)
        assert client.get(f"{side.base}/prompts", headers=h).status_code == 200
    assert [s.sc.tenant_id for s in made] == [two.a.tenant, two.b.tenant, two.a.tenant]
    assert len({id(s) for s in made}) == 3
    assert len({id(s.conns) for s in made}) == len({id(s.models) for s in made}) == 3
    assert all(s.conns.sc is s.sc and s.models.sc is s.sc for s in made)


def test_decide_on_bs_request_is_unknown_to_a_and_not_to_bs_own_owner(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    """A rejecting B's pending request and an unknown request get the same answer and B's request
    stays pending; B's own owner (who requested it) is refused by separation of duties (403)."""
    h = as_user(client, two.a.email)
    body = {"decision": "reject"}
    refused = client.post(
        f"{two.a.base}/publish-requests/{two.b.request}/decide", json=body, headers=h
    )
    missing = client.post(f"{two.a.base}/publish-requests/{uuid4()}/decide", json=body, headers=h)
    same(refused, missing)
    control = client.post(
        f"{two.b.base}/publish-requests/{two.b.request}/decide",
        json=body,
        headers=as_user(client, two.b.email),
    )
    assert control.status_code == 403, control.text
    with owner_db.session() as o:
        status = o.execute(
            text("SELECT status FROM shifde.publish_requests WHERE id = :i"),
            {"i": two.b.request},
        ).scalar_one()
    assert status == "pending"


def test_bs_slug_is_unknown_to_a_for_list_create_and_publish_requests(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    h = as_user(client, two.a.email)
    before = _b_state(owner_db, two.b.tenant)
    ghost = org(f"no-such-{uuid7().hex[-12:]}")
    body = {"key": "x", "name": "X", "spec": prompt_spec(), "scope": scope_of(two.b.tenant)}
    requests: list[Req] = [
        *(("GET", f"/{kind}", None) for kind in KINDS),
        ("POST", "/prompts", body),
        ("GET", "/publish-requests", None),
        ("POST", f"/publish-requests/{two.b.request}/decide", {"decision": "reject"}),
    ]
    for req in requests:
        same(_send(client, h, two.b.base, req), _send(client, h, ghost, req))
    assert _b_state(owner_db, two.b.tenant) == before


def _count(owner_db: Database, tenant: UUID, table: str) -> int:
    with owner_db.session() as o:
        return int(
            o.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :t"), {"t": tenant}
            ).scalar_one()
        )


def test_bs_model_and_route_in_a_request_body_are_unknown_to_a(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    """Review F4: A's prompt test naming B's model, and A's agent spec naming B's model or route,
    get the answer an unknown id gets; no run or usage row is written and B's rows are unchanged."""
    ha, hb = as_user(client, two.a.email), as_user(client, two.b.email)
    b_model = client.get(f"{two.b.base}/models", headers=hb).json()["items"][0]["id"]
    route = client.post(
        f"{two.b.base}/model-routes",
        headers=hb,
        json={
            "name": "b-route",
            "scope": {"type": "tenant", "id": str(two.b.tenant)},
            "primary_model_id": b_model,
            "fallback_model_ids": [],
            "conditions": {},
        },
    )
    assert route.status_code == 201, route.text
    ha = as_user(client, two.a.email)  # one session at a time: B's login replaced A's cookies
    before_b = _b_state(owner_db, two.b.tenant)
    counts = {
        (t, table): _count(owner_db, t, table)
        for t in (two.a.tenant, two.b.tenant)
        for table in ("prompt_test_runs", "usage_events")
    }
    pid, pv = two.a.ids["prompts"].artifact, two.a.ids["prompts"].version
    test = f"{two.a.base}/prompts/{pid}/test"
    base_body = {"version_id": pv, "variables": {"message": "hi"}}
    same(
        client.post(test, json={**base_body, "model_id": b_model}, headers=ha),
        client.post(test, json={**base_body, "model_id": str(uuid4())}, headers=ha),
    )
    # an agent spec naming B's model or route fails validation like an unknown id does
    agent = two.a.ids["agents"].artifact
    draft = client.get(f"{two.a.base}/agents/{agent}/draft", headers=ha).json()
    answers = []
    for ref in (
        {"model_id": b_model},
        {"model_id": str(uuid4())},
        {"route_id": route.json()["id"]},
        {"route_id": str(uuid4())},
    ):
        spec = {
            **draft["spec"],
            "model": {**ref, **({"fallback_model_ids": []} if "model_id" in ref else {})},
        }
        r = client.put(
            f"{two.a.base}/agents/{agent}/draft",
            json={"spec": spec, "row_version": draft["row_version"]},
            headers=ha,
        )
        v = client.post(f"{two.a.base}/agents/{agent}/draft/validate", headers=ha)
        answers.append((r.status_code, v.status_code, _issues(v)))
        draft = client.get(f"{two.a.base}/agents/{agent}/draft", headers=ha).json()
    assert answers[0] == answers[1] and answers[2] == answers[3], answers
    assert all(ans[0] >= 400 or ans[2] for ans in answers), answers  # refused, not vacuous
    assert _b_state(owner_db, two.b.tenant) == before_b
    after = {k: _count(owner_db, *k) for k in counts}
    assert after == counts


def _issues(r: httpx.Response) -> list[Any]:  # Any: JSON
    body = r.json()
    found = body.get("errors") or body.get("issues") or body.get("detail") or []
    return [
        {k: v for k, v in i.items() if k != "message"} if isinstance(i, dict) else i for i in found
    ]
