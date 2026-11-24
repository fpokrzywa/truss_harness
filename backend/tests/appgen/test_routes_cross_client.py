"""SP5-2 (design 9.1): app request routes, client A against client B, and project P1 against P2.

Every route of `appgen.api.router` has a test here (`ROUTE_TESTS`, matched against the router),
run both ways (A against B, then B against A). A's administrator asking for B's request on any
request route gets exactly the answer a random id gets (404, same body) and changes nothing of
B's; B's rows are absent from A's list, B's connection in A's create is an unknown connection,
and B's slug in the path is an unknown organization. A member bound only at project P1 gets the
same 404 on every route for P2's request (an edit without waiting on another writer's lock of
it), and P2's list and create answer as a project that does not exist. Under row-level security, `shifde_app` in A's
context reads none of B's requests or drafts and an UPDATE naming B's request touches nothing;
an UPDATE of a draft is refused for `shifde_app` (42501: it holds no UPDATE grant, so the guard
is never reached) and for the schema owner (23514, the guard).
"""

from __future__ import annotations

import threading
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
from tests.delivery.app_releases import add_provider, make_app_release
from tests.tenancy.conftest import app_session

_VARYING = {"instance", "request_id"}
ACCEPT = {
    "revision_no": 1,
    "solution_name": "Sales app",
    "model_id": "01900000-0000-7000-8000-0000000000aa",
    "thresholds": {"min_accuracy": "0.9", "max_p95_latency_ms": 1000, "max_total_cost_usd": "1"},
}
P2 = "/workspaces/main/projects/other"
NO_PROJECT = "/workspaces/main/projects/no-such-project"
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


# each cross-client route test runs both ways: (me, them) is (A, B), then (B, A)
DIRECTIONS = pytest.mark.parametrize("direction", ["a->b", "b->a"])


def _ways(two: Two, direction: str) -> tuple[Side, Side]:
    return (two.a, two.b) if direction == "a->b" else (two.b, two.a)


def _slug(side: Side) -> str:
    return "demo-a" if side.org == A else "demo-b"


def _state(owner: Session, org_slug: str) -> tuple[Any, ...]:  # Any: row values
    """The requests of client `org_slug`, with their draft counts and its job count."""
    owner.rollback()
    slug = tenant_id(owner, org_slug)
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
APP_PREVIEW = "/api/v1/orgs/{org}/releases/{release_id}/app-preview"
ROUTE_TESTS = {
    ("POST", REQUESTS): "test_a_cannot_name_bs_connection_or_organization",
    ("GET", REQUESTS): "test_a_lists_only_its_own_rows",
    ("GET", REQUEST): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/drafts"): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/discard"): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/retry"): "test_a_cannot_reach_bs_request",
    ("POST", REQUEST + "/accept"): "test_a_cannot_reach_bs_request",
    ("GET", APP_PREVIEW): "test_a_cannot_preview_bs_release",
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
        ("POST", request_url(request_id, org, "/accept"), ACCEPT),
    ]


def _send(h: Hdr, method: str, url: str, json: Any) -> httpx.Response:  # Any: a JSON body
    sent: httpx.Response = h.client.request(method, url, headers=dict(h), json=json)
    return sent


@DIRECTIONS
def test_a_cannot_reach_bs_request(two: Two, owner_session: Session, direction: str) -> None:
    me, them = _ways(two, direction)
    h = me.h
    before = _state(owner_session, _slug(them))
    for target in (them.drafted, them.failed):
        for (method, url, payload), (_, missing, _) in zip(
            _request_calls(target, me.org), _request_calls(str(uuid7()), me.org), strict=True
        ):
            _same(_send(h, method, url, payload), _send(h, method, missing, payload))
    assert _state(owner_session, _slug(them)) == before
    # its own: allowed (the edit, then the discard of its failed request and a retry refused)
    assert _send(h, "GET", request_url(me.drafted, me.org), None).status_code == 200
    edit = {"base_revision_no": 1, "draft": SAMPLE_DRAFT}
    assert _send(h, "POST", request_url(me.drafted, me.org, "/drafts"), edit).status_code == 201
    assert _send(h, "POST", request_url(me.failed, me.org, "/retry"), None).status_code == 202
    discarded = _send(h, "POST", request_url(me.drafted, me.org, "/discard"), None)
    assert discarded.status_code == 200


@DIRECTIONS
def test_a_lists_only_its_own_rows(two: Two, owner_session: Session, direction: str) -> None:
    me, them = _ways(two, direction)
    mine = me.h.client.get(requests_url(me.org), headers=me.h)
    assert mine.status_code == 200, mine.text
    assert {r["id"] for r in mine.json()["items"]} == {me.drafted, me.failed}
    theirs = them.h.client.get(requests_url(them.org), headers=them.h).json()
    assert {r["id"] for r in theirs["items"]} == {them.drafted, them.failed}
    # the other organization in the path: the unknown-organization answer
    _same(
        me.h.client.get(requests_url(them.org), headers=me.h),
        me.h.client.get(requests_url("/api/v1/orgs/no-such-org"), headers=me.h),
    )


@DIRECTIONS
def test_a_cannot_name_bs_connection_or_organization(
    two: Two, owner_session: Session, direction: str
) -> None:
    me, them = _ways(two, direction)
    h = me.h
    before = _state(owner_session, _slug(them))
    _same(
        create(h.client, h, body(them.connection, me.model), me.org),
        create(h.client, h, body(str(uuid7()), me.model), me.org),
    )
    # the other's model: the answer an unknown model gets
    _same(
        create(h.client, h, body(me.connection, them.model), me.org),
        create(h.client, h, body(me.connection, str(uuid7())), me.org),
    )
    _same(
        create(h.client, h, body(me.connection, me.model), them.org),
        create(h.client, h, body(me.connection, me.model), "/api/v1/orgs/no-such-org"),
    )
    assert _state(owner_session, _slug(them)) == before


@DIRECTIONS
def test_a_cannot_preview_bs_release(
    two: Two, owner_session: Session, owner_db: Database, direction: str
) -> None:
    me, them = _ways(two, direction)
    releases = {}
    for side in (me, them):
        tenant = UUID(str(tenant_id(owner_session, _slug(side))))
        workspace = owner_session.execute(
            text("SELECT id FROM shifde.workspaces WHERE tenant_id = :t AND slug = 'main'"),
            {"t": tenant},
        ).scalar_one()
        owner_session.rollback()
        provider = add_provider(owner_db, tenant, "fake", None)
        releases[side.org] = make_app_release(owner_db, tenant, workspace, provider_id=provider).id
    h = me.h

    def preview(release_id: object) -> httpx.Response:
        return _send(h, "GET", f"{me.org}/releases/{release_id}/app-preview", None)

    _same(preview(releases[them.org]), preview(uuid7()))
    assert preview(releases[me.org]).status_code == 200


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
    # P2's list and create: the answer a project that does not exist gets
    _same(
        member.client.get(requests_url(A, P2), headers=member),
        member.client.get(requests_url(A, NO_PROJECT), headers=member),
    )
    _same(
        create(member.client, member, body(p2_conn, model), A, P2),
        create(member.client, member, body(p2_conn, model), A, NO_PROJECT),
    )
    # P2's connection named in P1: the answer an unknown connection gets
    _same(
        create(member.client, member, body(p2_conn, model)),
        create(member.client, member, body(str(uuid7()), model)),
    )
    assert member.client.get(requests_url(A), headers=member).json()["items"] == []
    # P2's request held by another writer: the member's edit is refused without waiting on it
    owner_session.rollback()
    owner_session.execute(
        text("SELECT 1 FROM shifde.appgen_requests WHERE id = :r FOR UPDATE"), {"r": p2_request}
    )
    edit = {"base_revision_no": 1, "draft": SAMPLE_DRAFT}
    answers: list[httpx.Response] = []
    editor = threading.Thread(
        target=lambda: answers.append(
            _send(member, "POST", request_url(p2_request, A, "/drafts"), edit)
        )
    )
    editor.start()
    editor.join(timeout=15)
    waited = editor.is_alive()
    owner_session.rollback()
    editor.join()
    assert not waited, "a member without access waited on the request's row lock"
    _same(answers[0], _send(member, "POST", request_url(str(uuid7()), A, "/drafts"), edit))
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
    before = _state(owner_session, "demo-b")
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
    assert _state(owner_session, "demo-b") == before


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
