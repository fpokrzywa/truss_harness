"""SP5-2 (design 9.1): app request routes, client A against client B, and project P1 against P2.

Every route of `appgen.api.router` has a test here (`ROUTE_TESTS`, matched against the router).
A's administrator asking for B's request on any request route gets exactly the answer a random
id gets (404, same body) and changes nothing of B's; B's rows are absent from A's list, B's
connection in A's create is an unknown connection, and B's slug in the path is an unknown
organization. A member bound only at project P1 gets the same 404 on every route for P2's
request, and is refused on P2's list and create. Under row-level security, `shifde_app` in A's
context reads none of B's requests or drafts and an UPDATE naming B's request touches nothing;
an UPDATE of a draft is refused for `shifde_app` (42501: it holds no UPDATE grant, so the guard
is never reached) and for the schema owner (23514, the guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.appgen import api as appgen_api
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.appgen_fixtures import SAMPLE_DRAFT
from tests._support.fullapp import World
from tests._support.p2util import new_user, post, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, user_session
from tests.appgen.helpers import (
    INVALID,
    PROJECT,
    add_project,
    body,
    create,
    databricks,
    drafted,
    drafting_model,
    request_url,
    requests_url,
    run_job,
    wire,
)
from tests.tenancy.conftest import app_session

_VARYING = {"instance", "request_id"}
P2 = "/workspaces/main/projects/other"
MEMBER = "p1-only@demo-a.local"
MEMBER_PERMISSIONS = [
    "workspace:read",
    "project:read",
    "connection:read",
    "model:read",
    "model:invoke",
    "appgen:request:read",
    "appgen:request:write",
    "appgen:draft:accept",
]


def _same(r: httpx.Response, reference: httpx.Response, status: int = 404) -> None:
    """Refused exactly like an id that does not exist: no existence hint."""
    assert r.status_code == reference.status_code == status, (r.status_code, r.text)
    got, want = r.json(), reference.json()
    assert {k: v for k, v in got.items() if k not in _VARYING} == {
        k: v for k, v in want.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    h: Hdr
    org: str
    connection: str
    model: str
    drafted: str  # a drafted request
    failed: str  # a failed request


def _side(h: Hdr, owner: Session, org: str) -> Side:
    c = h.client
    cid, model = databricks(c, h, org=org), drafting_model(c, h, org)
    done = drafted(c, h, owner, body(cid, model), org)["id"]
    r = create(c, h, body(cid, model, brief=f"broken {INVALID}"), org)
    assert r.status_code == 202, r.text
    assert run_job(c, owner, r.json()["id"], "demo-a" if org == A else "demo-b")
    return Side(h, org, cid, model, done, r.json()["id"])


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def two(client: TestClient, world: World, owner_session: Session) -> Two:
    wire(client)
    b = _side(user_session(client, world.passwords, ADMIN_B), owner_session, B)
    a = _side(user_session(client, world.passwords, ADMIN), owner_session, A)
    return Two(a, b)


def _b_state(owner: Session) -> tuple[Any, ...]:  # Any: row values
    owner.rollback()
    slug = tenant_id(owner, "demo-b")
    rows = owner.execute(
        text(
            "SELECT r.id, r.status, r.status_reason, r.job_id, r.updated_at, "
            "(SELECT count(*) FROM shifde.appgen_drafts d WHERE d.request_id = r.id), "
            "(SELECT count(*) FROM shifde.jobs j WHERE j.tenant_id = r.tenant_id) "
            "FROM shifde.appgen_requests r WHERE r.tenant_id = :t ORDER BY r.id"
        ),
        {"t": slug},
    ).all()
    owner.rollback()
    return tuple(tuple(r) for r in rows)


# ------------------------------------------------------------------ route table
REQUESTS = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/app-requests"
REQUEST = "/api/v1/orgs/{org}/app-requests/{request_id}"
ROUTE_TESTS = {
    ("POST", REQUESTS): "test_a_cannot_name_bs_connection_or_organization",
    ("GET", REQUESTS): "test_a_lists_only_its_own_rows",
    ("GET", REQUEST): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/drafts"): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/discard"): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/retry"): "test_a_cannot_reach_bs_request",
}


def test_every_appgen_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in appgen_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert set(ROUTE_TESTS.values()) <= set(globals())


def _request_calls(request_id: str, org: str) -> list[tuple[str, str, Any]]:  # Any: bodies
    edit = {"base_revision_no": 1, "draft": SAMPLE_DRAFT}
    return [
        ("GET", request_url(request_id, org), None),
        ("POST", request_url(request_id, org, "/drafts"), edit),
        ("POST", request_url(request_id, org, "/discard"), None),
        ("POST", request_url(request_id, org, "/retry"), None),
    ]


def _send(h: Hdr, method: str, url: str, json: Any) -> httpx.Response:  # Any: a JSON body
    sent: httpx.Response = h.client.request(method, url, headers=dict(h), json=json)
    return sent


def test_a_cannot_reach_bs_request(two: Two, owner_session: Session) -> None:
    a = two.a.h
    before = _b_state(owner_session)
    for target in (two.b.drafted, two.b.failed):
        for (method, url, payload), (_, missing, _) in zip(
            _request_calls(target, A), _request_calls(str(uuid7()), A), strict=True
        ):
            _same(_send(a, method, url, payload), _send(a, method, missing, payload))
    assert _b_state(owner_session) == before
    # A's own: allowed (the edit, then the discard of its failed request and a retry refused)
    assert _send(a, "GET", request_url(two.a.drafted), None).status_code == 200
    edit = {"base_revision_no": 1, "draft": SAMPLE_DRAFT}
    assert _send(a, "POST", request_url(two.a.drafted, A, "/drafts"), edit).status_code == 201
    assert _send(a, "POST", request_url(two.a.failed, A, "/retry"), None).status_code == 202
    assert _send(a, "POST", request_url(two.a.drafted, A, "/discard"), None).status_code == 200


def test_a_lists_only_its_own_rows(two: Two, owner_session: Session) -> None:
    a, b = two.a.h, two.b.h
    mine = a.client.get(requests_url(A), headers=a)
    assert mine.status_code == 200, mine.text
    assert {r["id"] for r in mine.json()["items"]} == {two.a.drafted, two.a.failed}
    theirs = b.client.get(requests_url(B), headers=b).json()
    assert {r["id"] for r in theirs["items"]} == {two.b.drafted, two.b.failed}
    # B's organization in the path: the unknown-organization answer
    _same(
        a.client.get(requests_url(B), headers=a),
        a.client.get(requests_url("/api/v1/orgs/no-such-org"), headers=a),
    )


def test_a_cannot_name_bs_connection_or_organization(two: Two, owner_session: Session) -> None:
    a = two.a.h
    before = _b_state(owner_session)
    _same(
        create(a.client, a, body(two.b.connection, two.a.model)),
        create(a.client, a, body(str(uuid7()), two.a.model)),
    )
    # B's model: the answer an unknown model gets
    _same(
        create(a.client, a, body(two.a.connection, two.b.model)),
        create(a.client, a, body(two.a.connection, str(uuid7()))),
    )
    _same(
        create(a.client, a, body(two.a.connection, two.a.model), B),
        create(a.client, a, body(two.a.connection, two.a.model), "/api/v1/orgs/no-such-org"),
    )
    assert _b_state(owner_session) == before


# ------------------------------------------------------------------ project P1 against P2
def test_a_p1_only_member_gets_404_on_p2s_request(
    client: TestClient, world: World, owner_session: Session
) -> None:
    wire(client)
    admin = user_session(client, world.passwords, ADMIN)
    c = admin.client
    p1 = c.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    add_project(c, admin, "other")
    model = drafting_model(c, admin)
    p2_conn = databricks(c, admin, "p2-lake", scope={"workspace": "main", "project": "other"})
    made = create(c, admin, body(p2_conn, model), A, P2)
    assert made.status_code == 202, made.text
    p2_request = made.json()["id"]
    assert run_job(c, owner_session, p2_request)
    role = post(c, f"{A}/roles", admin,
                {"key": "p1-appgen", "name": "P1 appgen", "permissions": MEMBER_PERMISSIONS})  # fmt: skip
    uid = new_user(c, admin, "demo-a", MEMBER, "a-long-password-1")
    world.passwords[MEMBER] = "a-long-password-1"
    admin = user_session(client, world.passwords, ADMIN)
    binding = {"subject_type": "user", "subject_id": uid, "role_id": role["id"],
               "scope": {"type": "project", "id": p1}}  # fmt: skip
    post(admin.client, f"{A}/role-bindings", admin, binding)
    member = user_session(client, world.passwords, MEMBER)
    for (method, url, payload), (_, missing, _) in zip(
        _request_calls(p2_request, A), _request_calls(str(uuid7()), A), strict=True
    ):
        _same(_send(member, method, url, payload), _send(member, method, missing, payload))
    listed = member.client.get(requests_url(A, P2), headers=member)
    assert listed.status_code == 404, listed.text
    refused = create(member.client, member, body(p2_conn, model), A, P2)
    assert refused.status_code == 404, refused.text
    # P2's connection named in P1: the answer an unknown connection gets
    _same(
        create(member.client, member, body(p2_conn, model)),
        create(member.client, member, body(str(uuid7()), model)),
    )
    assert member.client.get(requests_url(A), headers=member).json()["items"] == []
    owner_session.rollback()
    status = owner_session.execute(
        text("SELECT status FROM shifde.appgen_requests WHERE id = :r"), {"r": p2_request}
    ).scalar_one()
    assert status == "drafted"


# ------------------------------------------------------------------ SQL level
def test_shifde_app_reads_and_updates_nothing_of_b(
    two: Two, owner_session: Session, app_db: Database
) -> None:
    tenant_a = UUID(str(tenant_id(owner_session, "demo-a")))
    before = _b_state(owner_session)
    with app_session(app_db, tenant_a) as s:
        requests = {str(r[0]) for r in s.execute(text("SELECT id FROM shifde.appgen_requests"))}
        assert requests == {two.a.drafted, two.a.failed}
        drafts = s.execute(text("SELECT DISTINCT request_id FROM shifde.appgen_drafts")).all()
        assert {str(r[0]) for r in drafts} == {two.a.drafted}
        changed = s.execute(
            text(
                "UPDATE shifde.appgen_requests SET status = 'discarded' WHERE id = ANY(:ids) "
                "RETURNING id"
            ),
            {"ids": [UUID(two.b.drafted), UUID(two.b.failed)]},
        ).all()
        assert changed == []
        s.commit()
    assert _b_state(owner_session) == before


def test_a_draft_cannot_be_updated(two: Two, owner_session: Session, app_db: Database) -> None:
    tenant_a = UUID(str(tenant_id(owner_session, "demo-a")))
    with app_session(app_db, tenant_a) as s:
        with pytest.raises(DBAPIError) as refused:
            s.execute(text("UPDATE shifde.appgen_drafts SET draft_hash = draft_hash"))
        # no UPDATE grant: refused before the trigger is reached
        assert getattr(refused.value.orig, "sqlstate", None) == "42501"
        s.rollback()
    owner_session.rollback()
    with pytest.raises(DBAPIError) as refused:
        owner_session.execute(
            text("UPDATE shifde.appgen_drafts SET source = 'edited' WHERE request_id = :r"),
            {"r": two.a.drafted},
        )
    assert getattr(refused.value.orig, "sqlstate", None) == "23514"
    owner_session.rollback()
