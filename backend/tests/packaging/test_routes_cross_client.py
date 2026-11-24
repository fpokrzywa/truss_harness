"""Solution, solution version and release routes, client A (demo-a) against client B (demo-b)
(issues #78 and #79, as `tests/delivery/test_routes_cross_client.py`).

Every route of `packaging.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first reaches their own object, then asks for B's solution, version or release under
A's organization (and project) and gets exactly the answer an id that does not exist gets (404,
same body), and finds B's rows absent from A's lists. B's slug in the path gives A the
unknown-organization 404. A version naming B's agent version or B's connection gets the same 422
as an unknown id; a release binding B's Databricks connection or B's model provider gets the
diagnostic an unknown id gets. A version naming B's evaluation dataset gets the 422 an unknown
dataset id gets, and sealing a version whose spec names B's dataset (written as the schema owner)
gets the `evaluation` diagnostic an unknown id gets (isolation review 029, finding 3; B-2703).
B's rows never change. Within one client, a user bound only at
workspace W1 who names a W2 project's agent as a W1 solution's entrypoint gets the unknown-id 422
too (isolation review 029-1, finding 5).

SP6-4, both ways: another client's `snowflake.native-app/v1` solution, its version and its sealed
`/2` release are the unknown-id 404 on every packaging route that serves them (seal included); a
Snowflake solution version naming the other client's Snowflake connection gets the unknown-id
422; and `shifde_app` in one client's context selects no row of the other's Snowflake solution,
version or release.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any  # Any: database rows

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.packaging import api as packaging_api
from shifde.packaging.canonical import digest
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p4util import agent_spec, art, prompt_spec, publish
from tests._support.p5util import Hdr, user_session
from tests.appgen.helpers import snowflake, wire_snowflake
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
from tests.packaging.test_app_releases import CHAT, _table
from tests.tenancy.conftest import app_session

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
        body = body_for(s.agent_v1, connection=s.connection, dataset=s.dataset)
        vid = new_version(h.client, h, sid, body, org)
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
    own_body = body_for(a.agent_v1, connection=a.connection, dataset=a.dataset)
    own = h.client.post(url, headers=h, json=own_body)
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


# ------------------------------------------------------------------ evaluation datasets (B-2703)
def _dataset_row(owner: Session, dataset_id: str) -> tuple[Any, ...]:
    row = owner.execute(
        text("SELECT * FROM shifde.evaluation_datasets WHERE id = :d"), {"d": dataset_id}
    ).one()
    owner.rollback()
    return tuple(row)


def _version_naming(owner: Session, two: Two, dataset_id: str) -> str:
    """A version of A's solution whose spec names `dataset_id`, written as the schema owner (the
    service would refuse it)."""
    a = two.a
    spec = a.h.client.get(
        f"{solutions(a.stack.org)}/{a.solution}/versions/{a.version}", headers=a.h
    ).json()["spec"]
    spec["evaluation"]["dataset_id"] = dataset_id
    tenant = owner.execute(text("SELECT id FROM shifde.tenants WHERE slug = 'demo-a'")).scalar_one()
    set_tenant(owner, tenant)
    version_id = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.solution_versions (id, tenant_id, solution_id, version_no, "
            "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES (:i, :t, "
            ":s, (SELECT max(version_no) + 1 FROM shifde.solution_versions WHERE solution_id = :s), "
            "'agent', :e, CAST(:spec AS jsonb), :h, :t)"
        ),
        {
            "i": version_id,
            "t": tenant,
            "s": a.solution,
            "e": a.stack.agent_v1,
            "spec": json.dumps(spec),
            "h": digest(spec),
        },
    )
    owner.commit()
    return str(version_id)


def test_a_cannot_name_or_seal_bs_dataset(two: Two, owner_session: Session) -> None:
    h, a = two.a.h, two.a
    url = f"{solutions(a.stack.org)}/{a.solution}/versions"
    b_dataset = _dataset_row(owner_session, two.b.stack.dataset)
    before = _b_state(owner_session, two)
    own = h.client.post(url, headers=h, json=body_for(a.stack.agent_v1, dataset=a.stack.dataset))
    assert own.status_code == 201, own.text  # own dataset first
    refused = h.client.post(
        url, headers=h, json=body_for(a.stack.agent_v1, dataset=two.b.stack.dataset)
    )
    _same(
        refused,
        h.client.post(url, headers=h, json=body_for(a.stack.agent_v1, dataset=str(uuid7()))),
        422,
    )
    assert [e["loc"] for e in refused.json()["errors"]] == [["body", "evaluation", "dataset_id"]]

    def evaluation(version_id: str) -> Any:  # Any: the `evaluation` diagnostic
        r = seal(h.client, h, a.solution, version_id, a.bindings())
        assert r.status_code == 422 and r.json()["code"] == "release_incompatible", r.text
        [found] = [e for e in r.json()["errors"] if e["path"] == "evaluation"]
        return found

    names_b = evaluation(_version_naming(owner_session, two, two.b.stack.dataset))
    assert names_b == evaluation(_version_naming(owner_session, two, str(uuid7())))
    assert names_b["status"] == "unsupported"
    assert names_b["reason"] == (
        "the evaluation dataset is not a dataset of this project you can read"
    )
    assert _dataset_row(owner_session, two.b.stack.dataset) == b_dataset
    assert _b_state(owner_session, two) == before


# ------------------------------------------------------------------ Snowflake solutions (SP6-4)
SNOWFLAKE_KIND = "snowflake.native-app/v1"


@dataclass(frozen=True)
class SfSide:
    h: Hdr
    org: str
    slug: str
    stack: Stack
    connection: str
    agent: str
    solution: str
    version: str
    release: str

    def bindings(self) -> dict[str, Any]:  # Any: a JSON body
        return bindings({self.stack.model: self.stack.provider}, {self.connection: self.connection})

    def body(self, connection: str | None = None) -> dict[str, Any]:  # Any: a JSON body
        made = body_for(
            self.agent, connection=connection or self.connection, dataset=self.stack.dataset
        )
        return {**made, "screens": [CHAT, _table("orders", "customer_id")]}


def _sf_side(h: Hdr, org: str, slug: str) -> SfSide:
    s = stack(h.client, h, org)
    sf = snowflake(h.client, h, "warehouse", org)
    agent = _agent_of(h, s, sf)
    sid = solution(h.client, h, f"warehouse-{slug}", org, target_kind=SNOWFLAKE_KIND)["id"]
    side = SfSide(h, org, slug, s, sf, agent, sid, "", "")
    vid = new_version(h.client, h, sid, side.body(), org)["id"]
    sealed = seal(h.client, h, sid, vid, side.bindings(), org)
    assert sealed.status_code == 201, sealed.text
    assert sealed.json()["manifest"]["target_kind"] == SNOWFLAKE_KIND
    return SfSide(h, org, slug, s, sf, agent, sid, vid, sealed.json()["id"])


def _agent_of(h: Hdr, s: Stack, connection: str) -> str:
    """A published agent of the stack's organization pinning one Snowflake `connection.sql`
    tool keyed `orders`."""
    scope = {"type": "project", "id": s.project_id}
    spec = {
        "executor": "connection.sql",
        "binding": {
            "connection_id": connection,
            "sql_template": "select o.id from DEMO_DB.PUBLIC.ORDERS o "
            "where o.customer_id = %(customer_id)s",
            "max_rows": 10,
        },
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}},
            "required": ["customer_id"],
        },
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "timeout_s": 10,
        "audit_level": "metadata",
    }
    tool = art(h.client, h, "tools", "orders", spec, scope, org=s.org)
    tool_v1 = publish(h.client, h, "tools", tool["id"], org=s.org)["id"]
    agent = agent_spec(f"latest:{s.prompt}", s.model, tool_version_ids=[tool_v1])
    made = art(h.client, h, "agents", "app", agent, scope, org=s.org)
    return str(publish(h.client, h, "agents", made["id"], org=s.org)["id"])


@pytest.fixture
def sf_two(client: TestClient, world: World) -> tuple[SfSide, SfSide]:
    wire_snowflake(client)
    b = _sf_side(user_session(client, world.passwords, ADMIN_B), B, "demo-b")
    a = _sf_side(user_session(client, world.passwords, ADMIN), A, "demo-a")
    return a, b


def _sf_state(owner: Session, side: SfSide) -> tuple[Any, ...]:
    rows = owner.execute(
        text(
            "SELECT s.name, s.target_kind, s.status, s.updated_at, v.id, v.spec_hash, "
            "r.id, r.digest, r.status, r.updated_at "
            "FROM shifde.solutions s JOIN shifde.solution_versions v ON v.solution_id = s.id "
            "LEFT JOIN shifde.releases r ON r.solution_version_id = v.id "
            "WHERE s.id = :s ORDER BY v.id, r.id"
        ),
        {"s": side.solution},
    ).all()
    owner.rollback()
    return tuple(tuple(r) for r in rows)


SF_DIRECTIONS = pytest.mark.parametrize("direction", ["a->b", "b->a"])


def _sf_ways(sides: tuple[SfSide, SfSide], direction: str) -> tuple[SfSide, SfSide]:
    return sides if direction == "a->b" else (sides[1], sides[0])


@SF_DIRECTIONS
def test_bs_snowflake_solution_version_and_release_are_unknown_to_a(
    sf_two: tuple[SfSide, SfSide], owner_session: Session, direction: str
) -> None:
    me, them = _sf_ways(sf_two, direction)
    h, base, nobody = me.h, solutions(me.org), str(uuid7())
    before = _sf_state(owner_session, them)
    for method, suffix, payload in (
        ("GET", "", None),
        ("GET", "/versions", None),
        ("POST", "/versions", me.body()),
        ("POST", "/archive", None),
    ):
        _same(
            h.client.request(method, f"{base}/{them.solution}{suffix}", headers=h, json=payload),
            h.client.request(method, f"{base}/{nobody}{suffix}", headers=h, json=payload),
        )
    for solution_id, version_id, ref_solution, ref_version in (
        (me.solution, them.version, me.solution, nobody),  # their version under my solution
        (them.solution, them.version, nobody, nobody),  # their solution and version
    ):
        _same(
            h.client.get(f"{base}/{solution_id}/versions/{version_id}", headers=h),
            h.client.get(f"{base}/{ref_solution}/versions/{ref_version}", headers=h),
        )
        _same(
            seal(h.client, h, solution_id, version_id, me.bindings(), me.org),
            seal(h.client, h, ref_solution, ref_version, me.bindings(), me.org),
        )
        _same(
            h.client.get(releases_url(solution_id, version_id, me.org), headers=h),
            h.client.get(releases_url(ref_solution, ref_version, me.org), headers=h),
        )
    _same(
        h.client.get(f"{me.org}/releases/{them.release}", headers=h),
        h.client.get(f"{me.org}/releases/{nobody}", headers=h),
    )
    _same(
        h.client.post(f"{me.org}/releases/{them.release}/withdraw", headers=h, json={"note": "x"}),
        h.client.post(f"{me.org}/releases/{nobody}/withdraw", headers=h, json={"note": "x"}),
    )
    # their organization's slug: the unknown-organization 404
    unknown = f"/api/v1/orgs/no-such-{uuid7().hex[-12:]}"
    _same(
        h.client.get(f"{solutions(them.org)}/{them.solution}", headers=h),
        h.client.get(f"{solutions(unknown)}/{them.solution}", headers=h),
    )
    own = h.client.get(f"{me.org}/releases/{me.release}", headers=h)
    assert own.status_code == 200 and own.json()["manifest"]["target_kind"] == SNOWFLAKE_KIND
    assert _sf_state(owner_session, them) == before


@SF_DIRECTIONS
def test_a_snowflake_version_cannot_name_bs_snowflake_connection(
    sf_two: tuple[SfSide, SfSide], owner_session: Session, direction: str
) -> None:
    me, them = _sf_ways(sf_two, direction)
    h, url = me.h, f"{solutions(me.org)}/{me.solution}/versions"
    before = _sf_state(owner_session, them)
    assert h.client.post(url, headers=h, json=me.body()).status_code == 201  # own first
    _same(
        h.client.post(url, headers=h, json=me.body(them.connection)),
        h.client.post(url, headers=h, json=me.body(str(uuid7()))),
        422,
    )
    # their Snowflake connection bound at seal: the diagnostic an unknown id gets
    model = {me.stack.model: me.stack.provider}
    assert _report(
        seal(
            h.client,
            h,
            me.solution,
            me.version,
            bindings(model, {me.connection: them.connection}),
            me.org,
        )
    ) == _report(
        seal(
            h.client,
            h,
            me.solution,
            me.version,
            bindings(model, {me.connection: str(uuid7())}),
            me.org,
        )
    )
    assert _sf_state(owner_session, them) == before


@SF_DIRECTIONS
def test_shifde_app_selects_no_row_of_bs_snowflake_solution(
    sf_two: tuple[SfSide, SfSide], owner_session: Session, app_db: Database, direction: str
) -> None:
    me, them = _sf_ways(sf_two, direction)
    tenant = owner_session.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": me.slug}
    ).scalar_one()
    owner_session.rollback()
    with app_session(app_db, tenant) as app:
        for table, column, value in (
            ("solutions", "id", them.solution),
            ("solutions", "target_kind", SNOWFLAKE_KIND),
            ("solution_versions", "solution_id", them.solution),
            ("releases", "id", them.release),
        ):
            rows = app.execute(
                text(f"SELECT id FROM shifde.{table} WHERE {column} = :v"),  # nosec B608
                {"v": value},
            ).all()
            if column == "target_kind":
                assert [str(r[0]) for r in rows] == [me.solution], table
            else:
                assert rows == [], table
        own = app.execute(
            text("SELECT target_kind FROM shifde.solutions WHERE id = :s"), {"s": me.solution}
        ).scalar_one()
        assert own == SNOWFLAKE_KIND
