"""Connections across projects inside one workspace (#156 rule 4.2, CM-1, design 2026-11-22
section 10).

A member whose connection grants cover project 1 only asks for a connection of project 2 of the
same workspace on every connection route that takes a `connection_id` (`CASES`, matched against
the router): each answer is exactly the answer an id that does not exist gets (404 "connection not
found", same body). The member's own connection in project 1 answers first; the project 2
connection has its own listener, which no request of the member reaches. Lists, with and without
`include_shared`, leave project 2 out.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.testservers import RestTestServer
from shifde.connections import api as connections_api
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.tenants import add_project
from tests.connections.routes import (
    REST_TOKEN,
    activate,
    as_user,
    connection,
    grant,
    limited_user,
    org,
    rest_body,
    secret,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws_slug

_VARYING = {"instance", "request_id"}
_ONE = "/api/v1/orgs/{org}/connections/{connection_id}"

PROJECT_PERMS = [
    "workspace:read",
    "project:read",
    "connection:read",
    "connection:manage",
    "connection:test",
    "connection:use",
    "connection:write",
    "secret:manage",  # PUT .../credential (CM-2, B-2748)
]

# every connections route naming a connection, in the order the member calls it (its own
# connection is disabled last): request body and the status its own connection answers
CASES: dict[tuple[str, str], tuple[Any, int]] = {
    ("GET", _ONE): (None, 200),
    ("GET", f"{_ONE}/tests"): (None, 200),
    ("GET", f"{_ONE}/test-jobs/{{job_id}}"): (None, 200),
    ("GET", f"{_ONE}/discoveries/latest"): (None, 200),
    ("POST", f"{_ONE}/discover"): (None, 200),
    ("POST", f"{_ONE}/invoke"): ({"method": "GET", "path": "/health"}, 200),
    ("POST", f"{_ONE}/test"): (None, 202),
    # `row_version` is the own connection's current one, read just before the call
    ("PUT", f"{_ONE}/credential"): ({"value": REST_TOKEN, "read_only_confirmed": True}, 200),
    ("PATCH", _ONE): ({"name": "renamed"}, 200),
    ("POST", f"{_ONE}/status"): ({"status": "disabled"}, 200),
}


def test_every_connection_id_route_has_a_cross_project_case() -> None:
    routes = {
        (method, route.path)
        for route in connections_api.router.routes
        for method in getattr(route, "methods", set())
        if "{connection_id}" in getattr(route, "path", "")
    }
    assert routes == set(CASES)


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


def _state(owner_db: Database, world: World, cid: str) -> tuple[Any, ...]:
    with owner_db.session() as o:
        set_tenant(o, world.a.tenant)
        row = o.execute(
            text(
                "SELECT name, status, health_status, row_version, config, secret_ref_id "
                "FROM shifde.connections WHERE id = :i"
            ),
            {"i": cid},
        ).one()
        tests = o.execute(
            text("SELECT count(*) FROM shifde.connection_tests WHERE connection_id = :i"),
            {"i": cid},
        ).scalar_one()
        found = o.execute(
            text("SELECT count(*) FROM shifde.connection_discoveries WHERE connection_id = :i"),
            {"i": cid},
        ).scalar_one()
        secret = o.execute(  # the bound secret, all columns (ciphertext included)
            text("SELECT s.* FROM shifde.secrets s WHERE s.id = :s"), {"s": row.secret_ref_id}
        ).one()
        jobs = o.execute(
            text(
                "SELECT id, status FROM shifde.jobs WHERE kind = 'connection.test' "
                "AND payload->>'connection_id' = :i ORDER BY id"
            ),
            {"i": cid},
        ).all()
    return (tuple(row), tests, found, [tuple(j) for j in jobs], tuple(secret))


def test_a_connection_of_another_project_answers_like_an_unknown_id(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    evil: RestTestServer,
) -> None:
    a, A = world.a, org(world.a.slug)
    with owner_db.session() as o:
        p2 = add_project(o, a.tenant, a.ws)
        o.commit()
    p2_slug = f"p-{p2.hex}"
    owner = as_user(client, people.owner_a)

    def made(server: RestTestServer, name: str, project: str, project_id: Any) -> str:
        sid = secret(
            app_db, settings, a.tenant, REST_TOKEN, scope_type="project", scope_id=project_id
        )
        scope = {"workspace": ws_slug(a), "project": project}
        cid = str(
            connection(client, owner, {**rest_body(server, sid, name=name), "scope": scope}, A)[
                "id"
            ]
        )
        activate(client, owner, cid, A)
        found = client.post(f"{A}/connections/{cid}/discover", headers=owner)
        assert found.status_code == 200, found.text
        return cid

    own = made(rest_server, "in-p1", project_slug(a), a.project)
    hidden = made(evil, "in-p2", p2_slug, p2)
    member, member_id = limited_user(owner_db, a.tenant, [], label="p1-member")
    grant(owner_db, a.tenant, member_id, PROJECT_PERMS, scope_type="project", scope_id=a.project)
    h = as_user(client, member)

    for url in (
        f"{A}/connections?limit=200",
        f"{A}/connections?workspace={ws_slug(a)}&project={project_slug(a)}&limit=200",
        f"{A}/connections?workspace={ws_slug(a)}&project={project_slug(a)}"
        "&include_shared=true&limit=200",
        f"{A}/connections?workspace={ws_slug(a)}&include_shared=true&limit=200",
    ):
        listed = client.get(url, headers=h)
        assert listed.status_code == 200, (url, listed.text)
        ids = [c["id"] for c in listed.json()["items"]]
        assert hidden not in ids, url
    assert [
        c["id"] for c in client.get(f"{A}/connections?limit=200", headers=h).json()["items"]
    ] == [own]

    before, evil_before = _state(owner_db, world, hidden), len(evil.requests)
    own_job, hidden_job = _state(owner_db, world, own)[3][-1][0], before[3][-1][0]
    nobody = str(uuid7())

    def url(path: str, cid: str, job: Any) -> str:
        return (
            path.replace("/api/v1/orgs/{org}", A)
            .replace("{connection_id}", cid)
            .replace("{job_id}", str(job))
        )

    for (method, path), (payload, ok) in CASES.items():
        if method == "PUT":
            current = client.get(url(_ONE, own, None), headers=h).json()["row_version"]
            payload = {**payload, "row_version": current}
        r = client.request(method, url(path, own, own_job), headers=h, json=payload)
        assert r.status_code == ok, (method, path, r.text)  # own object first
        missing = client.request(method, url(path, nobody, hidden_job), headers=h, json=payload)
        assert missing.json()["detail"] == "connection not found", missing.text
        _same_404(
            client.request(method, url(path, hidden, hidden_job), headers=h, json=payload),
            missing,
        )
    assert _state(owner_db, world, hidden) == before  # unchanged, no test, discovery or job added
    assert len(evil.requests) == evil_before  # the hidden connection was never driven
    assert any(r["path"] == "/health" for r in rest_server.requests)  # the member's own were
