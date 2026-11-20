"""Solution and solution version routes, client A (demo-a) against client B (demo-b) (issue #78,
as `tests/delivery/test_routes_cross_client.py`).

Every route of `packaging.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first reaches their own object, then asks for B's solution or version under A's
organization and project and gets exactly the answer an id that does not exist gets (404, same
body), and finds B's rows absent from A's lists. B's slug in the path gives A the
unknown-organization 404. A version naming B's agent version or B's connection gets the same 422
as an unknown id. B's rows never change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: database rows

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.packaging import api as packaging_api
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, user_session
from tests.packaging.helpers import Stack, new_version, solution, solutions, stack
from tests.packaging.helpers import version_body as body_for

_VARYING = {"instance", "request_id"}


def _same(r: httpx.Response, reference: httpx.Response, status: int = 404) -> None:
    """Refused exactly like an id or slug that does not exist: no existence hint."""
    assert r.status_code == reference.status_code == status, (r.status_code, r.text)
    got, want = r.json(), reference.json()
    assert {k: v for k, v in got.items() if k not in _VARYING} == {
        k: v for k, v in want.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    h: Hdr
    stack: Stack
    solution: str
    version: str


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def two(client: TestClient, world: World) -> Two:
    sides = []
    for email, org in ((ADMIN_B, B), (ADMIN, A)):
        h = user_session(client, world.passwords, email)
        s = stack(h.client, h, org)
        sid = solution(h.client, h, f"solution-{org[-6:]}", org)["id"]
        vid = new_version(h.client, h, sid, body_for(s.agent_v1, connection=s.connection), org)
        sides.append(Side(h, s, sid, vid["id"]))
    return Two(sides[1], sides[0])


def _b_state(owner: Session, two: Two) -> tuple[Any, ...]:
    rows = owner.execute(
        text(
            "SELECT s.name, s.status, s.updated_at, v.id, v.spec_hash, "
            "(SELECT count(*) FROM shifde.solution_versions x WHERE x.solution_id = s.id) "
            "FROM shifde.solutions s JOIN shifde.solution_versions v ON v.solution_id = s.id "
            "WHERE s.id = :s"
        ),
        {"s": two.b.solution},
    ).all()
    owner.rollback()
    return tuple(tuple(r) for r in rows)


# ------------------------------------------------------------------ route table
SOLUTIONS = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/solutions"
ROUTE_TESTS = {
    ("GET", SOLUTIONS): "test_a_lists_only_its_own_rows",
    ("POST", SOLUTIONS): "test_bs_organization_is_unknown_to_a",
    ("GET", SOLUTIONS + "/{solution_id}"): "test_a_cannot_reach_bs_solution",
    ("POST", SOLUTIONS + "/{solution_id}/archive"): "test_a_cannot_reach_bs_solution",
    ("GET", SOLUTIONS + "/{solution_id}/versions"): "test_a_cannot_reach_bs_solution",
    ("POST", SOLUTIONS + "/{solution_id}/versions"): "test_a_cannot_name_bs_build_or_connection",
    ("GET", SOLUTIONS + "/{solution_id}/versions/{version_id}"): ("test_a_cannot_reach_bs_version"),
}


def test_every_packaging_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in packaging_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


def test_bs_organization_is_unknown_to_a(two: Two, owner_session: Session) -> None:
    h = two.a.h
    own = h.client.post(
        solutions(two.a.stack.org),
        headers=h,
        json={"name": "own", "target_kind": "databricks.app/v1"},
    )
    assert own.status_code == 201, own.text
    unknown = f"/api/v1/orgs/no-such-{uuid7().hex[-12:]}"
    sid, vid = two.b.solution, two.b.version
    before = _b_state(owner_session, two)
    for method, tail, payload in (
        ("GET", "", None),
        ("POST", "", {"name": "x", "target_kind": "databricks.app/v1"}),
        ("GET", f"/{sid}", None),
        ("POST", f"/{sid}/archive", None),
        ("GET", f"/{sid}/versions", None),
        ("POST", f"/{sid}/versions", body_for(two.b.stack.agent_v1)),
        ("GET", f"/{sid}/versions/{vid}", None),
    ):
        missing = h.client.request(method, f"{solutions(unknown)}{tail}", headers=h, json=payload)
        _same(h.client.request(method, f"{solutions(B)}{tail}", headers=h, json=payload), missing)
    assert _b_state(owner_session, two) == before


def test_a_cannot_reach_bs_solution(two: Two, owner_session: Session) -> None:
    h, base = two.a.h, solutions(two.a.stack.org)
    nobody = str(uuid7())
    before = _b_state(owner_session, two)
    for method, suffix in (("GET", ""), ("GET", "/versions"), ("POST", "/archive")):
        r = h.client.request(method, f"{base}/{two.a.solution}{suffix}", headers=h)
        assert r.status_code == 200, (method, suffix, r.text)  # own object first
        _same(
            h.client.request(method, f"{base}/{two.b.solution}{suffix}", headers=h),
            h.client.request(method, f"{base}/{nobody}{suffix}", headers=h),
        )
    body = body_for(two.a.stack.agent_v1)
    _same(
        h.client.post(f"{base}/{two.b.solution}/versions", headers=h, json=body),
        h.client.post(f"{base}/{nobody}/versions", headers=h, json=body),
    )
    assert _b_state(owner_session, two) == before


def test_a_cannot_reach_bs_version(two: Two) -> None:
    h, base = two.a.h, solutions(two.a.stack.org)
    own = h.client.get(f"{base}/{two.a.solution}/versions/{two.a.version}", headers=h)
    assert own.status_code == 200, own.text
    nobody = str(uuid7())
    _same(  # B's version under A's solution
        h.client.get(f"{base}/{two.a.solution}/versions/{two.b.version}", headers=h),
        h.client.get(f"{base}/{two.a.solution}/versions/{nobody}", headers=h),
    )
    _same(  # B's solution and version
        h.client.get(f"{base}/{two.b.solution}/versions/{two.b.version}", headers=h),
        h.client.get(f"{base}/{nobody}/versions/{nobody}", headers=h),
    )


def test_a_lists_only_its_own_rows(two: Two) -> None:
    for me, other in ((two.a, two.b), (two.b, two.a)):
        base = solutions(me.stack.org)
        listed = me.h.client.get(f"{base}?limit=200", headers=me.h)
        assert listed.status_code == 200, listed.text
        ids = [x["id"] for x in listed.json()["items"]]
        assert ids == [me.solution] and other.solution not in ids
        versions = me.h.client.get(f"{base}/{me.solution}/versions", headers=me.h).json()
        assert [v["id"] for v in versions["items"]] == [me.version]


def test_a_cannot_name_bs_build_or_connection(two: Two, owner_session: Session) -> None:
    h, url = two.a.h, f"{solutions(two.a.stack.org)}/{two.a.solution}/versions"
    a = two.a.stack
    own = h.client.post(url, headers=h, json=body_for(a.agent_v1, connection=a.connection))
    assert own.status_code == 201, own.text  # own objects first
    before = _b_state(owner_session, two)
    _same(
        h.client.post(url, headers=h, json=body_for(two.b.stack.agent_v1)),
        h.client.post(url, headers=h, json=body_for(str(uuid7()))),
        422,
    )
    _same(
        h.client.post(url, headers=h, json=body_for(a.agent_v1, connection=two.b.stack.connection)),
        h.client.post(url, headers=h, json=body_for(a.agent_v1, connection=str(uuid7()))),
        422,
    )
    count = owner_session.execute(
        text("SELECT count(*) FROM shifde.solution_versions WHERE solution_id = :s"),
        {"s": two.a.solution},
    ).scalar_one()
    assert count == 2  # the fixture's version and the own one; no refused one was written
    assert _b_state(owner_session, two) == before
