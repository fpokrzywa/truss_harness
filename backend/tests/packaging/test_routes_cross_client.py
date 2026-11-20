"""Solution, solution version and release routes, client A (demo-a) against client B (demo-b)
(issues #78 and #79, as `tests/delivery/test_routes_cross_client.py`).

Every route of `packaging.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first reaches their own object, then asks for B's solution, version or release under
A's organization (and project) and gets exactly the answer an id that does not exist gets (404,
same body), and finds B's rows absent from A's lists. B's slug in the path gives A the
unknown-organization 404. A version naming B's agent version or B's connection gets the same 422
as an unknown id; a release binding B's Databricks connection or B's model provider gets the
diagnostic an unknown id gets. B's rows never change. Within one client, a user bound only at
workspace W1 who names a W2 project's agent as a W1 solution's entrypoint gets the unknown-id 422
too (isolation review 029-1, finding 5).
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
from tests._support.p4util import agent_spec, art, prompt_spec, publish
from tests._support.p5util import Hdr, user_session
from tests.authz.scoping.test_workspace_routes import _world
from tests.packaging.helpers import (
    Stack,
    bindings,
    databricks_connection,
    new_version,
    releases_url,
    seal,
    solution,
    solutions,
    stack,
)
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
    lake: str
    release: str

    def bindings(self) -> dict[str, Any]:  # Any: a JSON body
        s = self.stack
        return bindings({s.model: s.provider}, {s.connection: self.lake})


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
        lake = databricks_connection(h.client, h, org=org)
        side = Side(h, s, sid, vid["id"], lake, "")
        sealed = seal(h.client, h, sid, vid["id"], side.bindings(), org)
        assert sealed.status_code == 201, sealed.text
        sides.append(Side(h, s, sid, vid["id"], lake, sealed.json()["id"]))
    return Two(sides[1], sides[0])


def _b_state(owner: Session, two: Two) -> tuple[Any, ...]:
    rows = owner.execute(
        text(
            "SELECT s.name, s.status, s.updated_at, v.id, v.spec_hash, "
            "(SELECT count(*) FROM shifde.solution_versions x WHERE x.solution_id = s.id), "
            "r.id, r.digest, r.status, r.updated_at "
            "FROM shifde.solutions s JOIN shifde.solution_versions v ON v.solution_id = s.id "
            "LEFT JOIN shifde.releases r ON r.solution_version_id = v.id "
            "WHERE s.id = :s ORDER BY v.id, r.id"
        ),
        {"s": two.b.solution},
    ).all()
    owner.rollback()
    return tuple(tuple(r) for r in rows)


# ------------------------------------------------------------------ route table
SOLUTIONS = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/solutions"
RELEASES = SOLUTIONS + "/{solution_id}/versions/{version_id}/releases"
RELEASE = "/api/v1/orgs/{org}/releases/{release_id}"
ROUTE_TESTS = {
    ("GET", SOLUTIONS): "test_a_lists_only_its_own_rows",
    ("POST", SOLUTIONS): "test_bs_organization_is_unknown_to_a",
    ("GET", SOLUTIONS + "/{solution_id}"): "test_a_cannot_reach_bs_solution",
    ("POST", SOLUTIONS + "/{solution_id}/archive"): "test_a_cannot_reach_bs_solution",
    ("GET", SOLUTIONS + "/{solution_id}/versions"): "test_a_cannot_reach_bs_solution",
    ("POST", SOLUTIONS + "/{solution_id}/versions"): "test_a_cannot_name_bs_build_or_connection",
    ("GET", SOLUTIONS + "/{solution_id}/versions/{version_id}"): ("test_a_cannot_reach_bs_version"),
    ("POST", RELEASES): "test_a_cannot_seal_or_list_bs_version",
    ("GET", RELEASES): "test_a_cannot_seal_or_list_bs_version",
    ("GET", RELEASE): "test_a_cannot_reach_bs_release",
    ("POST", RELEASE + "/withdraw"): "test_a_cannot_reach_bs_release",
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
    for method, tail, payload in (
        ("POST", f"/{sid}/versions/{vid}/releases", two.b.bindings()),
        ("GET", f"/{sid}/versions/{vid}/releases", None),
    ):
        missing = h.client.request(method, f"{solutions(unknown)}{tail}", headers=h, json=payload)
        _same(h.client.request(method, f"{solutions(B)}{tail}", headers=h, json=payload), missing)
    rid = two.b.release
    for method, tail, payload in (("GET", "", None), ("POST", "/withdraw", {"note": "x"})):
        missing = h.client.request(
            method, f"{unknown}/releases/{rid}{tail}", headers=h, json=payload
        )
        _same(
            h.client.request(method, f"{B}/releases/{rid}{tail}", headers=h, json=payload), missing
        )
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
        url = releases_url(me.solution, me.version, me.stack.org)
        listed_releases = me.h.client.get(url, headers=me.h).json()["items"]
        assert [r["id"] for r in listed_releases] == [me.release]


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


def test_a_cannot_seal_or_list_bs_version(two: Two, owner_session: Session) -> None:
    h, a = two.a.h, two.a
    nobody = str(uuid7())
    before = _b_state(owner_session, two)
    own = seal(h.client, h, a.solution, a.version, a.bindings())
    assert own.status_code == 200, own.text  # own version first: the fixture's release
    own_list = h.client.get(releases_url(a.solution, a.version), headers=h)
    assert own_list.status_code == 200, own_list.text
    for solution_id, version_id, ref_solution, ref_version in (
        (a.solution, two.b.version, a.solution, nobody),  # B's version under A's solution
        (two.b.solution, two.b.version, nobody, nobody),  # B's solution and version
    ):
        _same(
            seal(h.client, h, solution_id, version_id, a.bindings()),
            seal(h.client, h, ref_solution, ref_version, a.bindings()),
        )
        _same(
            h.client.get(releases_url(solution_id, version_id), headers=h),
            h.client.get(releases_url(ref_solution, ref_version), headers=h),
        )
    assert _b_state(owner_session, two) == before


def test_a_cannot_reach_bs_release(two: Two, owner_session: Session) -> None:
    h = two.a.h
    nobody = str(uuid7())
    before = _b_state(owner_session, two)
    own = h.client.get(f"{A}/releases/{two.a.release}", headers=h)
    assert own.status_code == 200, own.text
    _same(
        h.client.get(f"{A}/releases/{two.b.release}", headers=h),
        h.client.get(f"{A}/releases/{nobody}", headers=h),
    )
    _same(
        h.client.post(f"{A}/releases/{two.b.release}/withdraw", headers=h, json={"note": "x"}),
        h.client.post(f"{A}/releases/{nobody}/withdraw", headers=h, json={"note": "x"}),
    )
    own_withdraw = f"{A}/releases/{two.a.release}/withdraw"
    withdrawn = h.client.post(own_withdraw, headers=h, json={"note": "x"})
    assert withdrawn.status_code == 200, withdrawn.text  # own release last
    assert _b_state(owner_session, two) == before


def _report(r: httpx.Response) -> list[dict[str, Any]]:  # Any: diagnostics
    assert r.status_code == 422 and r.json()["code"] == "release_incompatible", r.text
    return [e for e in r.json()["errors"] if e["path"] != "evaluation"]


def test_a_cannot_bind_bs_connection_or_provider(two: Two, owner_session: Session) -> None:
    h, a = two.a.h, two.a
    before = _b_state(owner_session, two)
    model, data = a.stack.model, a.stack.connection
    own = {model: a.stack.provider}
    sid, vid = a.solution, a.version
    # B's Databricks connection: the diagnostic an unknown connection id gets
    assert _report(seal(h.client, h, sid, vid, bindings(own, {data: two.b.lake}))) == _report(
        seal(h.client, h, sid, vid, bindings(own, {data: str(uuid7())}))
    )
    # B's model provider: the diagnostic an unknown provider id gets
    lake = {data: a.lake}
    b_provider = seal(h.client, h, sid, vid, bindings({model: two.b.stack.provider}, lake))
    unknown = seal(h.client, h, sid, vid, bindings({model: str(uuid7())}, lake))
    assert _report(b_provider) == _report(unknown)
    count = owner_session.execute(
        text("SELECT count(*) FROM shifde.releases WHERE solution_version_id = :v"), {"v": vid}
    ).scalar_one()
    owner_session.rollback()
    assert count == 1  # the fixture's release; no refused one was written
    assert _b_state(owner_session, two) == before


def test_a_w1_only_user_cannot_name_a_w2_agent(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """Lead addition 4: a W2 project's published agent, whether only W2 may see it (`scope`) or
    the W1-only user may read it but it is out of the W1 solution's scope (`tenant`), is refused
    as an unknown id is."""
    w = _world(client, owner_session, world.passwords)
    admin = w.admin
    model = stack(admin.client, admin).model
    w2 = {"type": "project", "id": w("proj", 2)}
    prompt = art(admin.client, admin, "prompts", "w2-prompt", prompt_spec(), w2)
    publish(admin.client, admin, "prompts", prompt["id"])
    agents = []
    for visibility in ("scope", "tenant"):
        spec = agent_spec(f"latest:{prompt['id']}", model)
        made = art(
            admin.client, admin, "agents", f"w2-{visibility}", spec, w2, visibility=visibility
        )
        agents.append(publish(admin.client, admin, "agents", made["id"])["id"])
    base = f"{A}/workspaces/w1/projects/p1/solutions"
    made_solution = admin.client.post(
        base, headers=admin, json={"name": "w1-solution", "target_kind": "databricks.app/v1"}
    )
    assert made_solution.status_code == 201, made_solution.text
    url = f"{base}/{made_solution.json()['id']}/versions"
    u = w.user

    def answer(entry: str) -> tuple[int, Any]:  # Any: the problem body
        r = u.client.post(url, headers=dict(u), json=body_for(entry))
        return r.status_code, {k: v for k, v in r.json().items() if k not in _VARYING}

    unknown = answer(str(uuid7()))
    assert unknown[0] == 422, unknown
    for agent in agents:
        assert answer(agent) == unknown
