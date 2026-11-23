"""The board's model, visibility and bounds (sub-project 7 design 7.1 and 7.2; B-3922 points
46-53 and 70): `unknown_outcome` never counts as current, expiry is computed on read and never
stored, unknown and invisible filters answer alike, a platform administrator without a binding
gets 404, each cell is null without its read permission, and the page, solution and release
bounds hold."""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.board.conftest import (
    add_project,
    cells,
    deployment,
    get_board,
    member,
    org_board,
    owner_run,
    plan,
    project_board,
    release,
    same_404,
    solution,
    target,
    without_time,
)
from tests.connections.routes import as_user
from tests.delivery.routes import plan_row
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, project_slug, ws2_slug, ws_slug

ALL = [
    "workspace:read",
    "project:read",
    "packaging:solution:read",
    "evaluation:run:read",
    "delivery:target:read",
    "delivery:deployment:read",
    "delivery:plan:read",
]


def _own_board(client: TestClient, world: World, email: str) -> Any:  # Any: a BoardOut body
    a = world.a
    h = as_user(client, email)
    return get_board(client, h, project_board(a.slug, ws_slug(a), project_slug(a)))


def _solution_of(body: dict[str, Any], solution_id: object) -> Any:  # Any: a solution body
    return next(
        sol for p in body["projects"] for sol in p["solutions"] if sol["id"] == str(solution_id)
    )


# ------------------------------------------------------------------ (a) unknown_outcome
def test_unknown_outcome_is_the_last_attempt_and_never_current(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t = target(owner_db, a.tenant, a.ws, a.project)
    verified = deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t, "verified")
    unknown = deployment(
        owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t, "unknown_outcome", "lost"
    )
    lone = solution(owner_db, a.tenant, a.ws, a.project)  # only an unknown outcome
    only = deployment(
        owner_db, a.tenant, plan(owner_db, a.tenant, t, lone.first), t, "unknown_outcome"
    )
    body = _own_board(client, world, people.owner_a)
    cell = cells(body, sol.id, t)
    assert cell["current"]["deployment_id"] == str(verified)
    assert cell["current"]["release_id"] == str(sol.first) and cell["current"]["version_no"] == 1
    assert cell["current"]["verified_at"] is not None
    assert cell["last_attempt"]["deployment_id"] == str(unknown)
    assert cell["last_attempt"]["status"] == "unknown_outcome"
    assert cell["last_attempt"]["status_reason"] == "lost"
    lone_cell = cells(body, lone.id, t)
    assert lone_cell["current"] is None
    assert lone_cell["last_attempt"]["deployment_id"] == str(only)
    assert lone_cell["last_attempt"]["status"] == "unknown_outcome"


# ------------------------------------------------------------------ (b) expiry on read
def test_a_sealed_or_approved_plan_past_its_expiry_reads_expired_and_is_not_changed(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t_sealed, t_approved, t_fresh = (target(owner_db, a.tenant, a.ws, a.project) for _ in "abc")
    sealed = plan(owner_db, a.tenant, t_sealed, sol.first, "sealed", sealed_age="1 hour")
    approved = plan(owner_db, a.tenant, t_approved, sol.first, "approved", sealed_age="1 hour")
    fresh = plan(owner_db, a.tenant, t_fresh, sol.first, "sealed")
    before = {p: dict(plan_row(owner_db, p)) for p in (sealed, approved, fresh)}
    body = _own_board(client, world, people.owner_a)
    for t, p, status in (
        (t_sealed, sealed, "expired"),
        (t_approved, approved, "expired"),
        (t_fresh, fresh, "sealed"),
    ):
        open_plan = cells(body, sol.id, t)["open_plan"]
        assert open_plan["plan_id"] == str(p) and open_plan["status"] == status, open_plan
        assert open_plan["release_id"] == str(sol.first) and open_plan["expires_at"] is not None
    after = {p: dict(plan_row(owner_db, p)) for p in (sealed, approved, fresh)}
    assert after == before
    assert after[sealed]["status"] == "sealed" and after[approved]["status"] == "approved"
    assert after[sealed]["updated_at"] is None


# ------------------------------------------------------------------ (c) filter oracle
def test_unknown_and_invisible_filters_answer_alike(
    client: TestClient, world: World, owner_db: Database
) -> None:
    a = world.a
    p2 = add_project(owner_db, a.tenant, a.ws)  # same workspace, not readable by the member
    email = member(owner_db, a.tenant, ALL, scope_type="project", scope_id=a.project)
    h = as_user(client, email)
    url = org_board(a.slug)
    unknown_ws = f"w-{uuid7().hex}"
    answers = [
        get_board(client, h, url, params)
        for params in (
            {"workspace": unknown_ws},
            {"workspace": ws2_slug(a)},  # exists, invisible
            {"workspace": ws_slug(a), "project": f"p-{uuid7().hex}"},
            {"workspace": ws_slug(a), "project": f"p-{p2.hex}"},  # exists, invisible
        )
    ]
    assert all(without_time(body) == {"projects": [], "next_cursor": None} for body in answers), (
        answers
    )
    # the member's own project is still listed with the filter
    own = get_board(client, h, url, {"workspace": ws_slug(a), "project": project_slug(a)})
    assert [p["project"]["id"] for p in own["projects"]] == [str(a.project)]
    # `project` without `workspace`: the existing 422
    r = client.get(url, headers=h, params={"project": project_slug(a)})
    assert r.status_code == 422, r.text
    # the single-project route: 404 for both, identical bodies
    missing = client.get(project_board(a.slug, ws_slug(a), f"p-{uuid7().hex}"), headers=h)
    same_404(client.get(project_board(a.slug, ws_slug(a), f"p-{p2.hex}"), headers=h), missing)
    missing_ws = client.get(project_board(a.slug, unknown_ws, f"p-{p2.hex}"), headers=h)
    same_404(client.get(project_board(a.slug, ws2_slug(a), f"p-{p2.hex}"), headers=h), missing_ws)


# ------------------------------------------------------------------ (d) platform administrator
def test_a_platform_administrator_without_a_binding_gets_404(
    client: TestClient, world: World, people: People
) -> None:
    a = world.a
    h = as_user(client, people.platform)
    unknown = f"no-such-{uuid7().hex[-12:]}"
    same_404(client.get(org_board(a.slug), headers=h), client.get(org_board(unknown), headers=h))
    same_404(
        client.get(project_board(a.slug, ws_slug(a), project_slug(a)), headers=h),
        client.get(project_board(unknown, ws_slug(a), project_slug(a)), headers=h),
    )


# ------------------------------------------------------------------ (e) nulls
def test_each_field_is_null_without_its_read_permission(
    client: TestClient, world: World, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t = target(owner_db, a.tenant, a.ws, a.project)
    deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t, "verified")
    plan(owner_db, a.tenant, t, sol.first)  # open

    def board(missing: str | None) -> Any:  # Any: a BoardOut body
        perms = [p for p in ALL if p != missing]
        return _own_board(client, world, member(owner_db, a.tenant, perms))

    full = board(None)
    cell = cells(full, sol.id, t)
    assert cell["current"] and cell["last_attempt"] and cell["open_plan"], cell
    assert _solution_of(full, sol.id)["releases"][0]["evaluation"]["status"] == "passed"

    cell = cells(board("delivery:deployment:read"), sol.id, t)
    assert cell["current"] is None and cell["last_attempt"] is None and cell["open_plan"]
    cell = cells(board("delivery:plan:read"), sol.id, t)
    assert cell["current"] and cell["last_attempt"] and cell["open_plan"] is None
    no_eval = board("evaluation:run:read")
    assert [r["evaluation"] for r in _solution_of(no_eval, sol.id)["releases"]] == [None]
    no_solutions = board("packaging:solution:read")
    assert [p["solutions"] for p in no_solutions["projects"]] == [[]]


# ------------------------------------------------------------------ (f) no read anywhere
def test_a_caller_with_no_read_anywhere_gets_an_empty_board(
    client: TestClient, world: World, owner_db: Database
) -> None:
    a = world.a
    solution(owner_db, a.tenant, a.ws, a.project)
    h = as_user(client, member(owner_db, a.tenant, ["delivery:target:read"]))
    body = get_board(client, h, org_board(a.slug))
    assert without_time(body) == {"projects": [], "next_cursor": None}


# ------------------------------------------------------------------ (g) bounds
def test_the_newest_five_releases_and_the_limit_bounds(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    newer = [release(owner_db, sol, n)[0] for n in range(2, 7)]
    body = _own_board(client, world, people.owner_a)
    shown = [r["id"] for r in _solution_of(body, sol.id)["releases"]]
    assert shown == [str(r) for r in reversed(newer)]  # newest 5 of 6, newest first
    h = as_user(client, people.owner_a)
    for limit in (0, 51):
        r = client.get(org_board(a.slug), headers=h, params={"limit": limit})
        assert r.status_code == 422, r.text


def test_projects_page_in_id_order(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    p2 = add_project(owner_db, a.tenant, a.ws2)
    add_project(owner_db, a.tenant, a.ws, status="archived")  # left out of the org board
    h = as_user(client, people.owner_a)
    first = get_board(client, h, org_board(a.slug), {"limit": 1})
    assert first["next_cursor"]
    second = get_board(client, h, org_board(a.slug), {"limit": 1, "cursor": first["next_cursor"]})
    assert second["next_cursor"] is None
    ids = [p["project"]["id"] for p in first["projects"] + second["projects"]]
    assert ids == sorted([str(a.project), str(p2)], key=UUID)
    assert first["projects"][0]["workspace"] == {
        "id": str(a.ws),
        "slug": ws_slug(a),
        "name": ws_slug(a),
    }
    whole = get_board(client, h, org_board(a.slug))
    assert [p["project"]["id"] for p in whole["projects"]] == ids


def test_an_archived_readable_project_has_its_own_board(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    archived = add_project(owner_db, a.tenant, a.ws, status="archived")
    h = as_user(client, people.owner_a)
    body = get_board(client, h, project_board(a.slug, ws_slug(a), f"p-{archived.hex}"))
    assert [p["project"]["id"] for p in body["projects"]] == [str(archived)]
    assert body["next_cursor"] is None


def test_more_than_200_solutions_are_truncated(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    p3 = add_project(owner_db, a.tenant, a.ws)
    owner_run(
        owner_db,
        "INSERT INTO shifde.solutions (tenant_id, id, workspace_id, project_id, name, "
        "target_kind, created_by) SELECT :t, gen_random_uuid(), :w, :p, 'bulk-' || n, "
        "'databricks.app/v1', :t FROM generate_series(1, 201) n",
        t=a.tenant,
        w=a.ws,
        p=p3,
    )
    h = as_user(client, people.owner_a)
    body = get_board(client, h, project_board(a.slug, ws_slug(a), f"p-{p3.hex}"))
    (project,) = body["projects"]
    assert project["truncated"] is True and len(project["solutions"]) == 200
    ids = [s["id"] for s in project["solutions"]]
    assert ids == sorted(ids, key=UUID)
    small = _own_board(client, world, people.owner_a)
    assert small["projects"][0]["truncated"] is False


# ------------------------------------------------------------------ (h) digest12, version
def test_digest12_and_the_latest_version_number(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    v2, digest2 = release(owner_db, sol, 2, evaluation=None)
    owner_run(  # version 3 without a release still counts
        owner_db,
        "INSERT INTO shifde.solution_versions (tenant_id, id, solution_id, version_no, "
        "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES "
        "(:t, :v, :s, 3, 'agent', :e, '{}'::jsonb, :h, :t)",
        t=a.tenant,
        v=uuid7(),
        s=sol.id,
        e=sol.entrypoint,
        h="b" * 64,
    )
    body = _own_board(client, world, people.owner_a)
    shown = _solution_of(body, sol.id)
    assert shown["latest_version_no"] == 3 and shown["status"] == "active"
    by_id = {r["id"]: r for r in shown["releases"]}
    assert by_id[str(v2)]["digest12"] == digest2[:12] and by_id[str(v2)]["version_no"] == 2
    assert by_id[str(v2)]["evaluation"] is None
    assert by_id[str(sol.first)]["digest12"] == sol.first_digest[:12]
    assert by_id[str(sol.first)]["status"] == "sealed"


# ------------------------------------------------------------------ (i), (j) plans not shown
def test_a_plan_without_a_release_is_not_shown(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t = target(owner_db, a.tenant, a.ws, a.project)
    template = plan(owner_db, a.tenant, t, None)
    deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, None), t, "verified")
    body = _own_board(client, world, people.owner_a)
    cell = cells(body, sol.id, t)
    assert cell["open_plan"] is None and cell["current"] is None and cell["last_attempt"] is None
    assert str(template) not in str(body)


def test_a_plan_a_deployment_names_is_not_the_open_plan(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t = target(owner_db, a.tenant, a.ws, a.project)
    started = plan(owner_db, a.tenant, t, sol.first)
    queued = deployment(owner_db, a.tenant, started, t)
    body = _own_board(client, world, people.owner_a)
    cell = cells(body, sol.id, t)
    assert cell["open_plan"] is None
    assert cell["last_attempt"]["deployment_id"] == str(queued)
    assert cell["last_attempt"]["status"] == "queued"


def test_every_visible_target_of_the_project_and_its_workspace_is_listed(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    own = target(owner_db, a.tenant, a.ws, a.project)
    shared = target(owner_db, a.tenant, a.ws, None)
    other_ws = target(owner_db, a.tenant, a.ws2, None)
    body = _own_board(client, world, people.owner_a)
    listed = [t["target_id"] for t in _solution_of(body, sol.id)["targets"]]
    assert listed == sorted([str(own), str(shared)], key=UUID)
    assert str(other_ws) not in str(body)
    row = cells(body, sol.id, own)
    assert row["current"] is None and row["last_attempt"] is None and row["open_plan"] is None
    assert row["environment"] == "dev" and row["platform_kind"] == "databricks"


def _statements(client: TestClient, h: dict[str, str], url: str) -> tuple[int, Any]:
    """(SQL statements run, body) of one board request."""
    seen: list[str] = []

    def count(*args: Any) -> None:  # Any: the cursor-execute event arguments
        seen.append(args[2])

    event.listen(Engine, "before_cursor_execute", count)
    try:
        body = get_board(client, h, url)
    finally:
        event.remove(Engine, "before_cursor_execute", count)
    return len(seen), body


def test_the_statement_count_does_not_grow_with_the_rows(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    url = org_board(a.slug)
    sol = solution(owner_db, a.tenant, a.ws, a.project)
    t = target(owner_db, a.tenant, a.ws, a.project)
    deployment(owner_db, a.tenant, plan(owner_db, a.tenant, t, sol.first), t, "verified")
    plan(owner_db, a.tenant, t, sol.first)
    small, _ = _statements(client, h, url)
    for _ in range(3):
        more = solution(owner_db, a.tenant, a.ws, a.project)
        release(owner_db, more, 2)
        extra = target(owner_db, a.tenant, a.ws, a.project)
        deployment(owner_db, a.tenant, plan(owner_db, a.tenant, extra, more.first), extra)
    large, body = _statements(client, h, url)
    (project,) = body["projects"]
    assert len(project["solutions"]) == 4
    assert all(len(s["targets"]) == 4 for s in project["solutions"])
    assert large == small, (small, large)
