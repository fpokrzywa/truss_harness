"""The board across projects of one workspace (sub-project 7 design 9.7): a member whose only
binding is on P1 sees P1 only. P2 is absent from the organization board, an empty board with
P2's filter, and on its own route the 404 a nonexistent project gets. P2's target is not on P1's
board, and the workspace's own target (no project) needs a workspace grant, which the member
lacks."""

from __future__ import annotations

from fastapi.testclient import TestClient

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.board.conftest import (
    add_project,
    deployment,
    get_board,
    member,
    org_board,
    plan,
    project_board,
    same_404,
    solution,
    target,
    without_time,
)
from tests.board.test_board import ALL
from tests.connections.routes import as_user
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws_slug


def test_a_p1_only_member_sees_p1_and_nothing_of_p2(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    p1, p2 = a.project, add_project(owner_db, a.tenant, a.ws)
    sol1 = solution(owner_db, a.tenant, a.ws, p1)
    sol2 = solution(owner_db, a.tenant, a.ws, p2)
    t1 = target(owner_db, a.tenant, a.ws, p1)
    t2 = target(owner_db, a.tenant, a.ws, p2)
    shared = target(owner_db, a.tenant, a.ws, None)
    for sol, t in ((sol1, t1), (sol2, t2), (sol1, shared), (sol2, shared)):
        deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t, "verified")
    p2_ids = {str(p2), str(sol2.id), str(sol2.first), str(t2), str(shared)}

    h = as_user(client, member(owner_db, a.tenant, ALL, scope_type="project", scope_id=p1))
    board = get_board(client, h, org_board(a.slug))
    assert [p["project"]["id"] for p in board["projects"]] == [str(p1)]
    (project,) = board["projects"]
    assert [s["id"] for s in project["solutions"]] == [str(sol1.id)]
    assert [t["target_id"] for t in project["solutions"][0]["targets"]] == [str(t1)]
    assert not [i for i in p2_ids if i in str(board)]

    filtered = get_board(
        client, h, org_board(a.slug), {"workspace": ws_slug(a), "project": f"p-{p2.hex}"}
    )
    assert without_time(filtered) == {"projects": [], "next_cursor": None}

    missing = client.get(project_board(a.slug, ws_slug(a), f"p-{uuid7().hex}"), headers=h)
    same_404(client.get(project_board(a.slug, ws_slug(a), f"p-{p2.hex}"), headers=h), missing)

    own = get_board(client, h, project_board(a.slug, ws_slug(a), project_slug(a)))
    assert without_time(own) == without_time(board)

    # the client administrator sees both projects and the workspace's target on each
    h = as_user(client, people.owner_a)
    whole = get_board(client, h, org_board(a.slug))
    assert [p["project"]["id"] for p in whole["projects"]] == sorted([str(p1), str(p2)])
    for p, mine in zip(whole["projects"], (t1, t2), strict=True):
        targets = [t["target_id"] for t in p["solutions"][0]["targets"]]
        assert targets == sorted([str(mine), str(shared)])
