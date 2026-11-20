"""Evaluation dataset and evaluation run routes, client A (demo-a) against client B (demo-b)
(issue #80, as `tests/packaging/test_routes_cross_client.py`).

Every route of `evaluation.api.router` has a test here (`ROUTE_TESTS`, matched against the
router): the caller first reaches their own object, then asks for B's dataset, release or
evaluation run under A's organization (and project) and gets exactly the answer an id that does
not exist gets (404, same body), and finds B's rows absent from A's lists. B's slug in the path
gives A the unknown-organization 404. An A release whose manifest names B's dataset (written as
the schema owner) is refused with the 409 an unknown dataset id gets, and writes nothing
(isolation review 029, finding 3; B-2703). B's rows never change.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any  # Any: database rows

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import set_tenant
from shifde.core.ids import uuid7
from shifde.evaluation import api as evaluation_api
from shifde.packaging.canonical import digest
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import user_session
from tests.evaluation.helpers import Release, case, release, request
from tests.packaging.helpers import datasets

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
    rel: Release
    run: str  # an evaluation run of `rel`


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def two(client: TestClient, world: World, owner_session: Session) -> Two:
    sides = []
    for email, org, slug in ((ADMIN_B, B, "demo-b"), (ADMIN, A, "demo-a")):
        h = user_session(client, world.passwords, email)
        rel = release(
            h, owner_session, [case("ok", "hello", ("contains", "hello"))], org=org, org_slug=slug
        )
        made = request(rel)
        assert made.status_code == 201, made.text
        sides.append(Side(rel, made.json()["id"]))
    return Two(sides[1], sides[0])


def _b_state(owner: Session, two: Two) -> tuple[Any, ...]:
    tenant = owner.execute(text("SELECT id FROM shifde.tenants WHERE slug = 'demo-b'")).scalar_one()
    rows = [
        tuple(r)
        for sql in (
            "SELECT * FROM shifde.evaluation_datasets WHERE tenant_id = :t ORDER BY id",
            "SELECT * FROM shifde.evaluation_runs WHERE tenant_id = :t ORDER BY id",
            "SELECT id, status FROM shifde.runs WHERE tenant_id = :t ORDER BY id",
        )
        for r in owner.execute(text(sql), {"t": tenant}).all()
    ]
    owner.rollback()
    return tuple(rows)


# ------------------------------------------------------------------ route table
DATASETS = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/evaluation-datasets"
EVALUATIONS = "/api/v1/orgs/{org}/releases/{release_id}/evaluations"
ROUTE_TESTS = {
    ("GET", DATASETS): "test_a_lists_only_its_own_rows",
    ("POST", DATASETS): "test_bs_organization_is_unknown_to_a",
    ("GET", DATASETS + "/{dataset_id}"): "test_a_cannot_reach_bs_dataset",
    ("POST", EVALUATIONS): "test_a_cannot_reach_bs_release_or_evaluation_run",
    ("GET", EVALUATIONS): "test_a_cannot_reach_bs_release_or_evaluation_run",
    ("GET", "/api/v1/orgs/{org}/evaluation-runs/{run_id}"): (
        "test_a_cannot_reach_bs_release_or_evaluation_run"
    ),
}


def test_every_evaluation_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in evaluation_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


def test_bs_organization_is_unknown_to_a(two: Two, owner_session: Session) -> None:
    h = two.a.rel.h
    body = {"name": "x", "cases": [case("c", "hello", ("contains", "hello"))]}
    own = h.client.post(datasets(A), headers=h, json=body)
    assert own.status_code == 201, own.text
    unknown = f"/api/v1/orgs/no-such-{uuid7().hex[-12:]}"
    before = _b_state(owner_session, two)
    b = two.b
    for method, tail, payload in (
        ("GET", datasets(), None),
        ("POST", datasets(), body),
        ("GET", f"{datasets()}/{b.rel.dataset['id']}", None),
        ("POST", f"/releases/{b.rel.id}/evaluations", {}),
        ("GET", f"/releases/{b.rel.id}/evaluations", None),
        ("GET", f"/evaluation-runs/{b.run}", None),
    ):
        tail = tail.removeprefix(A)
        missing = h.client.request(method, f"{unknown}{tail}", headers=h, json=payload)
        _same(h.client.request(method, f"{B}{tail}", headers=h, json=payload), missing)
    assert _b_state(owner_session, two) == before


def test_a_cannot_reach_bs_dataset(two: Two, owner_session: Session) -> None:
    h = two.a.rel.h
    before = _b_state(owner_session, two)
    own = h.client.get(f"{datasets(A)}/{two.a.rel.dataset['id']}", headers=h)
    assert own.status_code == 200, own.text
    _same(
        h.client.get(f"{datasets(A)}/{two.b.rel.dataset['id']}", headers=h),
        h.client.get(f"{datasets(A)}/{uuid7()}", headers=h),
    )
    assert _b_state(owner_session, two) == before


def test_a_lists_only_its_own_rows(two: Two) -> None:
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = me.rel.h
        listed = h.client.get(datasets(me.rel.org), headers=h, params={"limit": 200})
        assert listed.status_code == 200, listed.text
        ids = {x["id"] for x in listed.json()["items"]}
        assert me.rel.dataset["id"] in ids and other.rel.dataset["id"] not in ids
        runs = h.client.get(me.rel.evaluations(), headers=h)
        assert runs.status_code == 200, runs.text
        assert [x["id"] for x in runs.json()["items"]] == [me.run]


def test_a_cannot_reach_bs_release_or_evaluation_run(two: Two, owner_session: Session) -> None:
    h, a, b = two.a.rel.h, two.a, two.b
    nobody = str(uuid7())
    before = _b_state(owner_session, two)
    assert h.client.get(a.rel.evaluations(), headers=h).status_code == 200  # own first
    assert h.client.get(f"{A}/evaluation-runs/{a.run}", headers=h).status_code == 200
    for method, payload in (("GET", None), ("POST", {})):
        _same(
            h.client.request(
                method, f"{A}/releases/{b.rel.id}/evaluations", headers=h, json=payload
            ),
            h.client.request(method, f"{A}/releases/{nobody}/evaluations", headers=h, json=payload),
        )
    _same(
        h.client.get(f"{A}/evaluation-runs/{b.run}", headers=h),
        h.client.get(f"{A}/evaluation-runs/{nobody}", headers=h),
    )
    assert _b_state(owner_session, two) == before


def _release_naming(owner: Session, two: Two, dataset_id: str, dataset_digest: str) -> str:
    """A release of A's version whose manifest names `dataset_id`, written as the schema owner."""
    a = two.a.rel
    manifest = a.h.client.get(f"{A}/releases/{a.id}", headers=a.h).json()["manifest"]
    manifest = copy.deepcopy(manifest)
    manifest["evaluation"].update(dataset_id=dataset_id, dataset_digest=dataset_digest)
    compatibility = owner.execute(
        text("SELECT compatibility FROM shifde.releases WHERE id = :r"), {"r": a.id}
    ).scalar_one()
    tenant = owner.execute(text("SELECT id FROM shifde.tenants WHERE slug = 'demo-a'")).scalar_one()
    set_tenant(owner, tenant)
    release_id = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.releases (tenant_id, id, solution_version_id, digest, manifest, "
            "compatibility, status, sealed_by) VALUES (:t, :i, :v, :d, CAST(:m AS jsonb), "
            "CAST(:c AS jsonb), 'sealed', :t)"
        ),
        {
            "t": tenant,
            "i": release_id,
            "v": a.version,
            "d": digest(manifest),
            "m": json.dumps(manifest),
            "c": json.dumps(compatibility),
        },
    )
    owner.commit()
    return str(release_id)


def test_a_release_naming_bs_dataset_is_refused_as_an_unknown_one(
    two: Two, owner_session: Session
) -> None:
    h, b = two.a.rel.h, two.b.rel
    before = _b_state(owner_session, two)
    runs = owner_session.execute(text("SELECT count(*) FROM shifde.evaluation_runs")).scalar_one()
    owner_session.rollback()
    names_b = _release_naming(owner_session, two, b.dataset["id"], b.dataset["digest"])
    unknown = _release_naming(owner_session, two, str(uuid7()), b.dataset["digest"])
    refused = h.client.post(f"{A}/releases/{names_b}/evaluations", headers=h, json={})
    _same(refused, h.client.post(f"{A}/releases/{unknown}/evaluations", headers=h, json={}), 409)
    assert refused.json()["code"] == "evaluation_dataset_unavailable"
    after = owner_session.execute(text("SELECT count(*) FROM shifde.evaluation_runs")).scalar_one()
    owner_session.rollback()
    assert after == runs
    assert _b_state(owner_session, two) == before
