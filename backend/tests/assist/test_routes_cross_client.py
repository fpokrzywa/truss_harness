"""SP7-2 and SP7-3 (design 9.1): assist routes, client A against client B, and project P1 against
P2.

Every route of `assist.api.router` has a test here (`ROUTE_TESTS`, matched against the router),
run both ways. B's organization in the path gets A's user exactly the answer an unknown
organization gets (404, same body) on every route, and changes nothing of B's; B's installs never
appear in A's catalog. A member bound only at project P1 gets on every project route for P2 the
answer a project that does not exist gets (the request list and a new request included), and on
P2's request (and proposal) the answer an unknown id gets. Under row-level security, `shifde_app` in A's context
reads none of B's rows of the four assist tables (seeded as the schema owner), an UPDATE or a
DELETE naming them touches nothing, and an INSERT with B's `tenant_id` is refused. A DELETE of a
request or a proposal without the retention cutoff, or of a row not older than it, raises 42501;
`shifde_app` holds no DELETE on the settings and the specialists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies and row values
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.assist import api as assist_api
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import (
    NO_PROJECT,
    P2,
    PROJECT,
    add_project,
    enable,
    installed,
    model,
    project_member,
    rows,
)
from tests.tenancy.conftest import app_session

_VARYING = {"instance", "request_id"}
NO_ORG = "/api/v1/orgs/no-such-org"
TABLES = ("assist_settings", "assist_specialists", "assist_requests", "assist_proposals")
ORG_ROUTES = "/api/v1/orgs/{org}/assist"
PROJECT_ROUTES = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/assist"
ROUTE_TESTS = {
    ("GET", ORG_ROUTES + "/catalog"): "test_a_cannot_reach_bs_organization",
    ("GET", ORG_ROUTES + "/settings"): "test_a_cannot_reach_bs_organization",
    ("PUT", ORG_ROUTES + "/settings"): "test_a_cannot_reach_bs_organization",
    ("POST", ORG_ROUTES + "/enable"): "test_a_cannot_reach_bs_organization",
    ("POST", ORG_ROUTES + "/disable"): "test_a_cannot_reach_bs_organization",
    ("POST", PROJECT_ROUTES + "/install"): "test_a_cannot_reach_bs_organization",
    ("GET", PROJECT_ROUTES + "/specialists"): "test_a_cannot_reach_bs_organization",
    ("POST", PROJECT_ROUTES + "/specialists/{key}/enable"): "test_a_cannot_reach_bs_organization",
    ("POST", PROJECT_ROUTES + "/specialists/{key}/disable"): "test_a_cannot_reach_bs_organization",
    ("POST", PROJECT_ROUTES + "/requests"): "test_a_cannot_reach_bs_organization",
    ("GET", PROJECT_ROUTES + "/requests"): "test_a_cannot_reach_bs_organization",
    ("GET", ORG_ROUTES + "/requests/{request_id}"): "test_a_cannot_reach_bs_organization",
    (
        "POST",
        ORG_ROUTES + "/proposals/{proposal_id}/accept",
    ): "test_proposals_answer_like_unknown_ids",
    (
        "POST",
        ORG_ROUTES + "/proposals/{proposal_id}/dismiss",
    ): "test_proposals_answer_like_unknown_ids",
}
COST_REQUEST = {"text": "how much did we spend last week"}


def _same(r: httpx.Response, reference: httpx.Response, status: int = 404) -> None:
    """Refused exactly like an organization, project or id that does not exist."""
    assert r.status_code == reference.status_code == status, (r.status_code, r.text)
    got, want = r.json(), reference.json()
    assert {k: v for k, v in got.items() if k not in _VARYING} == {
        k: v for k, v in want.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    h: Hdr
    org: str
    slug: str
    model: str
    project_id: str
    request_id: str


def _request(h: Hdr, org: str, project: str = PROJECT) -> str:
    """A rule-routed request of the project (it runs the project's `cost` specialist)."""
    r = h.client.post(f"{org}{project}/assist/requests", headers=h, json=COST_REQUEST)
    assert r.status_code == 201 and r.json()["status"] == "routed", r.text
    return str(r.json()["id"])


def _side(client: TestClient, world: World, email: str, org: str, slug: str) -> Side:
    h = user_session(client, world.passwords, email)
    enable(h, org)
    model_id = model(h, org)
    [cost] = installed(h, model_id, org, keys=["cost"])
    return Side(h, org, slug, model_id, cost["project_id"], _request(h, org))


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def two(client: TestClient, world: World) -> Two:
    b = _side(client, world, ADMIN_B, B, "demo-b")
    a = _side(client, world, ADMIN, A, "demo-a")
    return Two(a, b)


DIRECTIONS = pytest.mark.parametrize("direction", ["a->b", "b->a"])


def test_every_assist_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in assist_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert set(ROUTE_TESTS.values()) <= set(globals())


def _calls(
    org: str, project: str, model_id: str, request_id: str
) -> list[tuple[str, str, Any]]:  # Any: bodies
    settings = {"model_routing_enabled": False, "specialist_max_cost": "1"}
    return [
        ("GET", f"{org}/assist/catalog", None),
        ("GET", f"{org}/assist/settings", None),
        ("PUT", f"{org}/assist/settings", settings),
        ("POST", f"{org}/assist/disable", None),
        ("POST", f"{org}/assist/enable", None),
        ("POST", f"{org}{project}/assist/install", {"model_id": model_id, "keys": ["cost"]}),
        ("GET", f"{org}{project}/assist/specialists", None),
        ("POST", f"{org}{project}/assist/specialists/cost/disable", None),
        ("POST", f"{org}{project}/assist/specialists/cost/enable", None),
        ("POST", f"{org}{project}/assist/requests", COST_REQUEST),
        ("GET", f"{org}{project}/assist/requests", None),
        ("GET", f"{org}/assist/requests/{request_id}", None),
    ]


def _send(h: Hdr, method: str, url: str, json: Any) -> httpx.Response:  # Any: a JSON body
    sent: httpx.Response = h.client.request(method, url, headers=dict(h), json=json)
    return sent


def _state(owner: Session, slug: str) -> list[tuple[Any, ...]]:
    t = tenant_id(owner, slug)
    out: list[tuple[Any, ...]] = []
    for table in TABLES:
        out += rows(owner, f"SELECT * FROM shifde.{table} WHERE tenant_id = :t", t=t)  # nosec B608
    out += rows(
        owner,
        "SELECT id, status, row_version FROM shifde.artifacts WHERE tenant_id = :t ORDER BY id",
        t=t,
    )
    return out


@DIRECTIONS
def test_a_cannot_reach_bs_organization(two: Two, owner_session: Session, direction: str) -> None:
    me, them = (two.a, two.b) if direction == "a->b" else (two.b, two.a)
    before = _state(owner_session, them.slug)
    for (method, url, body), (_, missing, _) in zip(
        _calls(them.org, PROJECT, me.model, them.request_id),
        _calls(NO_ORG, PROJECT, me.model, them.request_id),
        strict=True,
    ):
        _same(_send(me.h, method, url, body), _send(me.h, method, missing, body))
    assert _state(owner_session, them.slug) == before
    # their installs are not in my catalog; mine are
    mine = me.h.client.get(f"{me.org}/assist/catalog", headers=me.h)
    assert mine.status_code == 200, mine.text
    assert them.project_id not in mine.text and me.project_id in mine.text
    # their request is not reachable under my organization either
    unknown = me.h.client.get(f"{me.org}/assist/requests/{uuid7()}", headers=me.h)
    _same(me.h.client.get(f"{me.org}/assist/requests/{them.request_id}", headers=me.h), unknown)
    assert _state(owner_session, them.slug) == before
    # each route of my own organization answers
    for method, url, body in _calls(me.org, PROJECT, me.model, me.request_id):
        assert _send(me.h, method, url, body).status_code in (200, 201), url


def test_a_p1_only_member_gets_404_on_every_p2_route(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    p1 = admin.client.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    add_project(admin, "other")
    enable(admin)
    model_id = model(admin)
    installed(admin, model_id, project=P2, keys=["cost"])
    p2_request = _request(admin, A, P2)
    before = _state(owner_session, "demo-a")
    member = project_member(
        client, world, "p1-admin@demo-a.local", p1, "01900000-0000-7000-8000-000000000003"
    )  # project administrator: every project-level assist key at P1
    calls = _calls(A, P2, model_id, p2_request)[5:]
    for (method, url, body), (_, missing, _) in zip(
        calls, _calls(A, NO_PROJECT, model_id, str(uuid7()))[5:], strict=True
    ):
        _same(_send(member, method, url, body), _send(member, method, missing, body))
    assert _state(owner_session, "demo-a") == before
    catalog_out = member.client.get(f"{A}/assist/catalog", headers=member)
    assert catalog_out.status_code == 200, catalog_out.text
    assert all(e["installs"] == [] for e in catalog_out.json()["entries"])


# ------------------------------------------------------------------ proposals (SP7-4)
def _proposal(owner: Session, slug: str, request_id: str) -> str:
    """An open evaluation-run proposal of the request (schema owner)."""
    proposal_id = uuid7()
    owner.rollback()
    owner.execute(
        text("INSERT INTO shifde.assist_proposals (tenant_id, id, request_id, run_id, step_key, "
             "kind, payload) SELECT tenant_id, :id, id, run_id, 'k', 'evaluation_run', "
             "CAST(:p AS jsonb) FROM shifde.assist_requests WHERE id = :r"),
        {"id": proposal_id, "r": request_id, "p": f'{{"release_id": "{uuid7()}"}}'},
    )  # fmt: skip
    owner.commit()
    return str(proposal_id)


def _decide(h: Hdr, org: str, proposal_id: str, action: str) -> httpx.Response:
    return _send(h, "POST", f"{org}/assist/proposals/{proposal_id}/{action}", {})


@DIRECTIONS
def test_proposals_answer_like_unknown_ids(
    two: Two, client: TestClient, world: World, owner_session: Session, direction: str
) -> None:
    """Client A's user on B's proposal, under B's organization and under A's, gets the answer an
    unknown organization or proposal gets; a P1-only member gets on P2's proposal the answer an
    unknown proposal gets. Nothing changes."""
    me, them = (two.a, two.b) if direction == "a->b" else (two.b, two.a)
    theirs = _proposal(owner_session, them.slug, them.request_id)
    before = _state(owner_session, them.slug)
    for action in ("accept", "dismiss"):
        _same(_decide(me.h, them.org, theirs, action), _decide(me.h, NO_ORG, theirs, action))
        _same(_decide(me.h, me.org, theirs, action), _decide(me.h, me.org, str(uuid7()), action))
    assert _state(owner_session, them.slug) == before
    mine = _proposal(owner_session, me.slug, me.request_id)
    assert _decide(me.h, me.org, mine, "dismiss").json()["status"] == "dismissed"
    if direction == "b->a":
        return
    # project P1 against P2, within A
    admin = two.a.h
    p1 = admin.client.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    add_project(admin, "other")
    installed(admin, two.a.model, project=P2, keys=["cost"])
    p2_proposal = _proposal(owner_session, "demo-a", _request(admin, A, P2))
    member = project_member(
        client, world, "p1-admin@demo-a.local", p1, "01900000-0000-7000-8000-000000000003"
    )
    before = _state(owner_session, "demo-a")
    for action in ("accept", "dismiss"):
        _same(_decide(member, A, p2_proposal, action), _decide(member, A, str(uuid7()), action))
    assert _state(owner_session, "demo-a") == before


# ------------------------------------------------------------------ SQL level
def _seed(owner: Session, side: Side) -> dict[str, UUID]:
    """A request and a proposal of `side`'s client, 200 days old, written as the schema owner."""
    t = tenant_id(owner, side.slug)
    owner.rollback()
    user = owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"),
        {"e": ADMIN if side.org == A else ADMIN_B},
    ).scalar_one()
    ws = owner.execute(
        text("SELECT workspace_id FROM shifde.projects WHERE id = :p"), {"p": side.project_id}
    ).scalar_one()
    request_id, proposal_id = uuid7(), uuid7()
    owner.execute(
        text("INSERT INTO shifde.assist_requests (tenant_id, id, workspace_id, project_id, "
             "requested_by, text, status, created_at) VALUES (:t, :id, :w, :p, :u, 'hello', "
             "'refused', now() - interval '200 days')"),
        {"t": t, "id": request_id, "w": ws, "p": side.project_id, "u": user},
    )  # fmt: skip
    owner.execute(
        text("INSERT INTO shifde.assist_proposals (tenant_id, id, request_id, run_id, step_key, "
             "kind, payload, created_at) VALUES (:t, :id, :r, :run, 'k', 'evaluation_run', "
             "'{}'::jsonb, now() - interval '200 days')"),
        {"t": t, "id": proposal_id, "r": request_id, "run": uuid7()},
    )  # fmt: skip
    owner.commit()
    return {"request": request_id, "proposal": proposal_id}


def _ids(s: Session, table: str) -> set[str]:
    return {str(r[0]) for r in s.execute(text(f"SELECT tenant_id FROM shifde.{table}"))}  # nosec B608


def test_shifde_app_reaches_none_of_bs_rows(
    two: Two, owner_session: Session, app_db: Database
) -> None:
    _seed(owner_session, two.a)
    theirs = _seed(owner_session, two.b)
    tenant_a, tenant_b = (str(tenant_id(owner_session, s)) for s in ("demo-a", "demo-b"))
    owner_session.rollback()
    before = _state(owner_session, "demo-b")
    with app_session(app_db, UUID(tenant_a)) as s:
        for table in TABLES:
            assert _ids(s, table) == {tenant_a}, table
        for table, column, value in (
            ("assist_settings", "assist_enabled", "false"),
            ("assist_specialists", "status", "'disabled'"),
            ("assist_requests", "status", "'failed'"),
            ("assist_proposals", "status", "'dismissed'"),
        ):
            changed = s.execute(
                text(f"UPDATE shifde.{table} SET {column} = {value} "  # nosec B608
                     "WHERE tenant_id = :t RETURNING 1"),
                {"t": tenant_b},
            ).all()  # fmt: skip
            assert changed == [], table
        s.execute(text("SELECT set_config('shifde.retention_delete_cutoff', "
                       "CAST(now() - interval '100 days' AS text), true)"))  # fmt: skip
        for table, key in (("assist_proposals", "proposal"), ("assist_requests", "request")):
            gone = s.execute(
                text(f"DELETE FROM shifde.{table} WHERE id = :id RETURNING 1"),  # nosec B608
                {"id": theirs[key]},
            ).all()
            assert gone == [], table
        s.commit()
    assert _state(owner_session, "demo-b") == before
    with app_session(app_db, UUID(tenant_a)) as s:
        with pytest.raises(DBAPIError) as refused:
            s.execute(
                text("INSERT INTO shifde.assist_settings (tenant_id) VALUES (:t)"), {"t": tenant_b}
            )
        assert getattr(refused.value.orig, "sqlstate", None) == "42501"
        s.rollback()


@pytest.mark.parametrize("cutoff", [None, "", "not a time", "2000-01-01T00:00:00Z"])
def test_a_delete_outside_the_retention_cutoff_is_refused(
    two: Two, owner_session: Session, app_db: Database, cutoff: str | None
) -> None:
    mine = _seed(owner_session, two.a)
    tenant_a = UUID(str(tenant_id(owner_session, "demo-a")))
    owner_session.rollback()
    for table, key in (("assist_proposals", "proposal"), ("assist_requests", "request")):
        with app_session(app_db, tenant_a) as s:
            if cutoff is not None:
                s.execute(text("SELECT set_config('shifde.retention_delete_cutoff', :c, true)"),
                          {"c": cutoff})  # fmt: skip
            with pytest.raises(DBAPIError) as refused:
                s.execute(
                    text(f"DELETE FROM shifde.{table} WHERE id = :id"),  # nosec B608
                    {"id": mine[key]},
                )
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", table
            s.rollback()
    # before the cutoff: deleted (the proposal first; the request's cascade passes the guard too)
    with app_session(app_db, tenant_a) as s:
        s.execute(text("SELECT set_config('shifde.retention_delete_cutoff', "
                       "CAST(now() - interval '100 days' AS text), true)"))  # fmt: skip
        gone = s.execute(
            text("DELETE FROM shifde.assist_requests WHERE id = :id RETURNING 1"),
            {"id": mine["request"]},
        ).all()
        assert gone == [(1,)]
        s.commit()
    assert rows(owner_session, "SELECT 1 FROM shifde.assist_proposals WHERE id = :id",
                id=mine["proposal"]) == []  # fmt: skip


def test_shifde_app_deletes_no_settings_or_specialists(owner_session: Session) -> None:
    held = rows(
        owner_session,
        "SELECT t, has_table_privilege('shifde_app', 'shifde.' || t, 'DELETE') "
        "FROM unnest(CAST(:ts AS text[])) AS t ORDER BY t",
        ts=list(TABLES),
    )
    assert dict(held) == {
        "assist_proposals": True,
        "assist_requests": True,
        "assist_settings": False,
        "assist_specialists": False,
    }
