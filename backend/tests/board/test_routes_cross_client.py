"""Board routes, client A against client B (sub-project 7 design 9.1; Call S7-F: no board across
organizations).

Every route of `board.api.router` has a test here (`ROUTE_TESTS`, matched against the router).
B's organization slug gives A the unknown-organization 404 on both routes; B's workspace and
project slugs under A's organization give the 404 nonexistent slugs get on the single-project
route and an empty board as the organization route's filter; A's board, built while B has
solutions, releases, evaluation runs, targets, plans and deployments, names none of B's ids. The
reverse direction is covered once.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from shifde.board import api as board_api
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.board.conftest import (
    Sol,
    deployment,
    get_board,
    org_board,
    plan,
    project_board,
    release,
    same_404,
    solution,
    target,
    without_time,
)
from tests.connections.routes import as_user
from tests.tenancy.conftest import Client, World
from tests.tenancy.routes import People, project_slug, ws_slug

ROUTE_TESTS = {
    ("GET", "/api/v1/orgs/{org}/board"): "test_a_never_sees_bs_board",
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/board"): (
        "test_a_never_sees_bs_board"
    ),
}


def test_every_board_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in board_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


@dataclass(frozen=True)
class Side:
    sol: Sol
    targets: tuple[UUID, UUID]
    ids: frozenset[str]


def _side(owner_db: Database, c: Client) -> Side:
    """A full board of `c`: a solution with two evaluated releases, a project target and a
    workspace target, a verified and an unknown-outcome deployment and an open plan."""
    sol = solution(owner_db, c.tenant, c.ws, c.project)
    second, _ = release(owner_db, sol, 2)
    own, shared = target(owner_db, c.tenant, c.ws, c.project), target(owner_db, c.tenant, c.ws)
    ids = {sol.id, sol.first, second, own, shared, c.project, c.ws}
    for t, status in ((own, "verified"), (shared, "unknown_outcome")):
        p = plan(owner_db, c.tenant, t, sol.first)
        ids.add(deployment(owner_db, c.tenant, p, t, status))  # a deployed plan is not open
    ids.add(plan(owner_db, c.tenant, own, second))
    return Side(sol, (own, shared), frozenset(str(i) for i in ids))


@pytest.fixture
def sides(owner_db: Database, world: World) -> tuple[Side, Side]:
    return _side(owner_db, world.a), _side(owner_db, world.b)


def _check_one_way(
    client: TestClient, email: str, mine: Client, theirs: Client, own: Side, other: Side
) -> None:
    h = as_user(client, email)
    unknown = f"no-such-{uuid7().hex[-12:]}"
    # the other organization's slug: the unknown-organization 404, on both routes
    same_404(
        client.get(org_board(theirs.slug), headers=h), client.get(org_board(unknown), headers=h)
    )
    same_404(
        client.get(project_board(theirs.slug, ws_slug(theirs), project_slug(theirs)), headers=h),
        client.get(project_board(unknown, ws_slug(theirs), project_slug(theirs)), headers=h),
    )
    # the other organization's workspace and project slugs under the caller's organization
    same_404(
        client.get(project_board(mine.slug, ws_slug(theirs), project_slug(theirs)), headers=h),
        client.get(project_board(mine.slug, f"w-{uuid7().hex}", f"p-{uuid7().hex}"), headers=h),
    )
    same_404(
        client.get(project_board(mine.slug, ws_slug(mine), project_slug(theirs)), headers=h),
        client.get(project_board(mine.slug, ws_slug(mine), f"p-{uuid7().hex}"), headers=h),
    )
    for params in (
        {"workspace": ws_slug(theirs)},
        {"workspace": ws_slug(theirs), "project": project_slug(theirs)},
        {"workspace": ws_slug(mine), "project": project_slug(theirs)},
    ):
        body = get_board(client, h, org_board(mine.slug), params)
        assert without_time(body) == {"projects": [], "next_cursor": None}, params
    # the caller's own board: everything of its own, nothing of the other's
    for url in (org_board(mine.slug), project_board(mine.slug, ws_slug(mine), project_slug(mine))):
        text = client.get(url, headers=h).text
        assert all(i in text for i in own.ids), url
        assert not [i for i in other.ids if i in text], url


def test_a_never_sees_bs_board(
    client: TestClient, world: World, people: People, sides: tuple[Side, Side]
) -> None:
    a_side, b_side = sides
    _check_one_way(client, people.owner_a, world.a, world.b, a_side, b_side)


def test_b_never_sees_as_board(
    client: TestClient, world: World, people: People, sides: tuple[Side, Side]
) -> None:
    a_side, b_side = sides
    _check_one_way(client, people.owner_b, world.b, world.a, b_side, a_side)


def test_a_person_in_both_sees_each_board_only_under_its_organization(
    client: TestClient, world: World, people: People, sides: tuple[Side, Side]
) -> None:
    """`both` is a viewer in A and in B: A's board names nothing of B's, and B's nothing of A's."""
    a_side, b_side = sides
    h = as_user(client, people.both)
    for c, other in ((world.a, b_side), (world.b, a_side)):
        text = client.get(org_board(c.slug), headers=h).text
        assert str(c.project) in text
        assert not [i for i in other.ids if i in text], c.slug
