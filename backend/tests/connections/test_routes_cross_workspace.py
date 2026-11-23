"""Connections across workspaces inside one client (connections review 2 item 2, B-1512).

A member whose connection grants cover workspace 1 only asks for a connection of workspace 2 of the
same client on every connection route: each answer is exactly the answer an id that does not exist
gets (404 "connection not found", same body), never a 403 or a different 404 that would confirm the
id exists. The member's own connection in workspace 1 answers first; the workspace 2 connection
has its own listener, which no request of the member reaches.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.testservers import RestTestServer
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.conntests import discovery_of, run_invoke
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
from tests.tenancy.routes import People, ws2_slug, ws_slug

_VARYING = {"instance", "request_id"}

WORKSPACE_PERMS = [
    "workspace:read",
    "project:read",
    "connection:read",
    "connection:manage",
    "connection:test",
    "connection:use",
    "connection:write",
]


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
        jobs = o.execute(  # its test and discover jobs (ARCH M2): none is queued by the member
            text(
                "SELECT id, kind, status FROM shifde.jobs "
                "WHERE kind IN ('connection.test', 'connection.discover') "
                "AND payload->>'connection_id' = :i ORDER BY id"
            ),
            {"i": cid},
        ).all()
        invocations = o.execute(  # its invoke results (issue #190): none is added by the member
            text(
                "SELECT * FROM shifde.connection_invocations WHERE connection_id = :i ORDER BY id"
            ),
            {"i": cid},
        ).all()
    return (tuple(row), tests, found, [tuple(j) for j in jobs], [tuple(i) for i in invocations])


def test_a_connection_in_an_unreadable_workspace_answers_like_an_unknown_id(
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
    owner = as_user(client, people.owner_a)
    # a workspace connection takes a secret of its own workspace (connections_0002, #156)
    sids = {
        ws_slug(a): secret(
            app_db, settings, a.tenant, REST_TOKEN, scope_type="workspace", scope_id=a.ws
        ),
        ws2_slug(a): secret(
            app_db, settings, a.tenant, REST_TOKEN, scope_type="workspace", scope_id=a.ws2
        ),
    }

    def made(server: RestTestServer, name: str, workspace: str) -> str:
        body = {**rest_body(server, sids[workspace], name=name), "scope": {"workspace": workspace}}
        cid = str(connection(client, owner, body, A)["id"])
        activate(client, owner, cid, A)
        discovery_of(client, owner, A, cid)
        return cid

    own = made(rest_server, "in-ws1", ws_slug(a))
    hidden = made(evil, "in-ws2", ws2_slug(a))
    invoke = {"method": "GET", "path": "/health"}
    hidden_inv = run_invoke(client, owner, A, hidden, invoke, tenant_id=a.tenant)["id"]
    member, member_id = limited_user(owner_db, a.tenant, [], label="ws1-member")
    grant(owner_db, a.tenant, member_id, WORKSPACE_PERMS, scope_type="workspace", scope_id=a.ws)
    h = as_user(client, member)

    listed = client.get(f"{A}/connections?limit=200", headers=h)
    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()["items"]] == [own]

    before, evil_before = _state(owner_db, world, hidden), len(evil.requests)
    nobody = str(uuid7())
    cases: list[tuple[str, str, Any, int]] = [
        ("GET", "", None, 200),
        ("GET", "/tests", None, 200),
        ("GET", "/discoveries/latest", None, 200),
        ("POST", "/discover", None, 202),
        ("POST", "/invoke", invoke, 202),
        ("POST", "/test", None, 202),
        ("PATCH", "", {"name": "renamed"}, 200),
        ("POST", "/status", {"status": "disabled"}, 200),
    ]
    for method, suffix, payload, ok in cases:
        r = client.request(method, f"{A}/connections/{own}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        missing = client.request(
            method, f"{A}/connections/{nobody}{suffix}", headers=h, json=payload
        )
        assert missing.json()["detail"] == "connection not found", missing.text
        _same_404(
            client.request(method, f"{A}/connections/{hidden}{suffix}", headers=h, json=payload),
            missing,
        )
    # the status routes of the hidden connection's test and discover jobs (ARCH M2): the
    # unknown-connection 404
    hidden_jobs = {kind: jid for jid, kind, _ in _state(owner_db, world, hidden)[3]}
    for path, kind in (("test-jobs", "connection.test"), ("discovery-jobs", "connection.discover")):
        _same_404(
            client.get(f"{A}/connections/{hidden}/{path}/{hidden_jobs[kind]}", headers=h),
            client.get(f"{A}/connections/{nobody}/{path}/{hidden_jobs[kind]}", headers=h),
        )
    # ... and of its invocation (issue #190)
    _same_404(
        client.get(f"{A}/connections/{hidden}/invocations/{hidden_inv}", headers=h),
        client.get(f"{A}/connections/{nobody}/invocations/{hidden_inv}", headers=h),
    )
    assert _state(owner_db, world, hidden) == before  # unchanged, no test or discovery added
    assert len(evil.requests) == evil_before  # the hidden connection was never driven
    assert any(r["path"] == "/health" for r in rest_server.requests)  # the member's own were
