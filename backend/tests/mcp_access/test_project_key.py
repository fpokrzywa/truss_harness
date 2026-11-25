"""The HTTP routes with a key masked to project P1 whose owner holds only organization-level
grants (issue #359, DECISIONS B-4040): P1 and its workspace by slug answer; P2, another workspace,
the workspace's own objects and another client do not; the dashboard shows no activity beyond P1.

`demo-a` has workspace `main` with projects `sample` (P1) and `zulu-p2` (P2), and workspace
`zulu-w2` with project `zulu-far`; P1, P2, `main` itself and `zulu-w2` each have a connection, a
delivery target and activity. The full app of `tests/mcp_access` serves both HTTP and `/mcp`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any  # Any: JSON bodies and row values
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import bearer, post
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import add_project, rows
from tests.board.conftest import owner_run, same_404, solution, target
from tests.connections.routes import connection, grant
from tests.identity.conftest import owner_membership
from tests.identity.harness import PASSWORD, person
from tests.mcp_access.helpers import (
    ADMIN_A,
    ORG_A,
    ORG_B,
    TOOL_SCOPES,
    V1_TOOLS,
    A,
    B,
    call,
    make_key,
    ok,
    project,
    set_mcp,
    tenant,
)

NOT_FOUND = {"error": "not_found", "message": "not found"}
MAIN, SECOND = f"{A}/workspaces/main", f"{A}/workspaces/zulu-w2"
P1, P2 = f"{MAIN}/projects/sample", f"{MAIN}/projects/zulu-p2"
FAR = f"{SECOND}/projects/zulu-far"
NO_ORG = "/api/v1/orgs/no-such-org"
# what `layout` names outside P1 (P1's own connection is `zulu-one`)
HIDDEN_NAMES = ("zulu-p2", "zulu-two", "zulu-main", "zulu-w2", "zulu-far", "zulu-second")
# the mask, not a missing key scope, is what refuses the writes and the organization-wide reads
KEY_SCOPES = [
    *TOOL_SCOPES,
    "connection:read",
    "workspace:manage",
    "project:create",
    "project:manage",
    "member:read",
    "tenant:admin",
]


@dataclass(frozen=True)
class Place:
    """The objects of one place: a project, or a workspace's own (no project)."""

    connection: str
    target: str
    solution: str | None = None

    @property
    def ids(self) -> set[str]:
        return {i for i in (self.connection, self.target, self.solution) if i}


@dataclass(frozen=True)
class Layout:
    tenant: UUID
    ws: UUID
    p1: UUID
    p2: UUID
    in_p1: Place
    in_p2: Place
    in_main: Place  # workspace-level objects
    in_far: Place
    in_second: Place  # workspace-level objects of the other workspace

    @property
    def hidden(self) -> set[str]:
        others = (self.in_p2, self.in_main, self.in_far, self.in_second)
        return {str(self.p2), *(i for place in others for i in place.ids)}


@pytest.fixture
def admin(mcp: TestClient, world: World) -> Hdr:
    return user_session(mcp, world.passwords, ADMIN_A)


def _change(admin: Hdr, url: str) -> None:
    """One audited change of the workspace or project at `url` (its activity)."""
    row = admin.client.get(url).json()
    body = {"description": "changed", "row_version": row["row_version"]}
    r = admin.client.patch(url, headers=admin, json=body)
    assert r.status_code == 200, r.text


@pytest.fixture
def layout(admin: Hdr, owner_session: Session, owner_db: Database) -> Layout:
    a, c = tenant(owner_session, ORG_A), admin.client
    p1, p2 = project(owner_session, ORG_A), UUID(add_project(admin, "zulu-p2"))
    [(ws,)] = rows(owner_session, "SELECT workspace_id FROM shifde.projects WHERE id = :p", p=p1)
    second = {"slug": "zulu-w2", "name": "zulu-w2"}
    ws2 = UUID(post(c, f"{A}/workspaces", admin, second)["id"])
    far = UUID(post(c, f"{SECOND}/projects", admin, {"slug": "zulu-far", "name": "zulu-far"})["id"])

    def place(name: str, ws_slug: str, ws_id: UUID, proj: tuple[str, UUID] | None) -> Place:
        scope = {"workspace": ws_slug, **({"project": proj[0]} if proj else {})}
        body = {
            "name": f"zulu-{name}",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "config": {"base_url": "https://api.example.com"},
            "scope": scope,
        }
        made = connection(c, admin, body, A)
        project_id = proj[1] if proj else None
        return Place(
            made["id"],
            str(target(owner_db, a, ws_id, project_id)),
            str(solution(owner_db, a, ws_id, project_id).id) if project_id else None,
        )

    made = Layout(
        a,
        ws,
        p1,
        p2,
        in_p1=place("one", "main", ws, ("sample", p1)),
        in_p2=place("two", "main", ws, ("zulu-p2", p2)),
        in_main=place("main", "main", ws, None),
        in_far=place("far", "zulu-w2", ws2, ("zulu-far", far)),
        in_second=place("second", "zulu-w2", ws2, None),
    )
    owner_run(  # bindings the target route can show
        owner_db,
        "UPDATE shifde.delivery_targets SET bindings = CAST(:b AS jsonb) WHERE tenant_id = :t",
        b=json.dumps({"warehouse_id": "abc123def456"}),
        t=a,
    )
    for url in (P1, P2, MAIN, SECOND, FAR):
        _change(admin, url)
    return made


@pytest.fixture
def key(mcp: TestClient, world: World, owner_session: Session, layout: Layout) -> Hdr:
    """The headers of the key; its owner's bindings are only at organization level."""
    bindings = rows(
        owner_session,
        "SELECT DISTINCT b.scope_type FROM shifde.role_bindings b "
        "JOIN shifde.users u ON u.id = b.subject_id WHERE u.primary_email = :e",
        e=ADMIN_A,
    )
    assert bindings == [("tenant",)]
    mask = {"workspace_ids": [], "project_ids": [str(layout.p1)]}
    _, secret = make_key(mcp, world, ADMIN_A, A, scopes=KEY_SCOPES, resource_scope=mask)
    set_mcp(mcp, world, ADMIN_A, A, allowed_tools=sorted(V1_TOOLS), rate_per_minute=600)
    mcp.cookies.clear()  # the key alone authenticates
    headers = Hdr(bearer(secret))
    headers.client = mcp
    return headers


def _json(c: Hdr, url: str, **params: Any) -> Any:
    r: httpx.Response = c.client.get(url, headers=c, params=params)
    assert r.status_code == 200, (url, r.text)
    return r.json()


def _ids(c: Hdr, url: str, **params: Any) -> list[str]:
    return [i["id"] for i in _json(c, url, **params)["items"]]


def _activity(c: Hdr, **params: str) -> list[tuple[str, str | None]]:
    shown = _json(c, f"{A}/dashboard", **params)["recent_activity"]
    return [(i["action"], i["target"]) for i in shown]


def _recorded(owner: Session, where: str, **params: Any) -> list[tuple[Any, ...]]:
    """(action, target) of demo-a's audit rows, newest first, as the dashboard shows them."""
    return rows(
        owner,
        "SELECT action, coalesce(details->>'target', target_type || ':' || target_id) "
        "FROM shifde.audit_events WHERE tenant_id = (SELECT id FROM shifde.tenants WHERE slug = "
        f"'demo-a') AND {where} ORDER BY occurred_at DESC, id DESC LIMIT 20",
        **params,
    )


# ------------------------------------------------------------------ allowed
def test_the_key_reads_its_own_project_and_the_workspace_of_it(key: Hdr, layout: Layout) -> None:
    assert _json(key, A)["slug"] == ORG_A
    ws = _json(key, MAIN)
    assert (ws["id"], ws["slug"]) == (str(layout.ws), "main")
    assert _ids(key, f"{A}/workspaces") == [str(layout.ws)]
    assert _ids(key, f"{MAIN}/projects") == [str(layout.p1)]
    assert _json(key, P1)["id"] == str(layout.p1)
    board = _json(key, f"{P1}/board")
    assert [p["project"]["id"] for p in board["projects"]] == [str(layout.p1)]
    [shown] = board["projects"][0]["solutions"]
    assert shown["id"] == layout.in_p1.solution
    text = json.dumps(board)
    assert {i for i in layout.hidden if i in text} == set() and "zulu" not in text


# ------------------------------------------------------------------ refused
def test_the_key_gets_the_404_of_an_unknown_slug_everywhere_else(key: Hdr) -> None:
    def get(url: str) -> httpx.Response:
        r: httpx.Response = key.client.get(url, headers=key)
        return r

    for hidden, unknown in (
        (P2, f"{MAIN}/projects/nope"),
        (f"{P2}/board", f"{MAIN}/projects/nope/board"),
        (SECOND, f"{A}/workspaces/nope"),
        (f"{SECOND}/projects", f"{A}/workspaces/nope/projects"),
        (FAR, f"{SECOND}/projects/nope"),
        (f"{FAR}/board", f"{SECOND}/projects/nope/board"),
    ):
        same_404(get(hidden), get(unknown))


def test_objects_outside_the_project_are_unknown_ids_and_in_no_list(
    key: Hdr, admin: Hdr, layout: Layout
) -> None:
    for collection, attr in (("connections", "connection"), ("delivery/targets", "target")):
        url = f"{A}/{collection}"
        unknown = key.client.get(f"{url}/{uuid7()}", headers=key)
        for place in (layout.in_main, layout.in_p2, layout.in_far, layout.in_second):
            same_404(key.client.get(f"{url}/{getattr(place, attr)}", headers=key), unknown)
        own = getattr(layout.in_p1, attr)
        assert _json(key, f"{url}/{own}")["id"] == own
        assert _ids(key, url) == [own]
        assert _ids(key, url, workspace="main", project="sample") == [own]
        assert _ids(key, url, workspace="main") == [own]
        other = key.client.get(url, headers=key, params={"workspace": "zulu-w2"})
        same_404(other, key.client.get(url, headers=key, params={"workspace": "nope"}))
    in_p1, shared = layout.in_p1.connection, layout.in_main.connection
    url, main = f"{A}/connections", {"workspace": "main", "include_shared": True}
    assert _ids(key, url, **main) == []  # the workspace's and the organization's own
    assert _ids(key, url, **main, project="sample") == [in_p1]
    assert _ids(admin, url, **main) == [shared]  # the control
    assert set(_ids(admin, url, **main, project="sample")) == {in_p1, shared}


def test_the_key_writes_neither_its_workspace_nor_another(
    key: Hdr, layout: Layout, owner_session: Session
) -> None:
    def state() -> list[tuple[Any, ...]]:
        return rows(
            owner_session,
            "SELECT 'w', id::text, name, status, row_version FROM shifde.workspaces UNION ALL "
            "SELECT 'p', id::text, name, status, row_version FROM shifde.projects ORDER BY 1, 2",
        )

    before = state()
    new = {"slug": "made-by-key", "name": "made by key"}
    [(version,)] = rows(
        owner_session, "SELECT row_version FROM shifde.workspaces WHERE id = :w", w=layout.ws
    )
    change = {"name": "renamed", "row_version": version}
    c = key.client
    assert c.patch(MAIN, headers=key, json=change).status_code == 403
    assert c.post(f"{MAIN}/projects", headers=key, json=new).status_code == 403
    assert c.patch(SECOND, headers=key, json=change).status_code == 404
    assert c.post(f"{SECOND}/projects", headers=key, json=new).status_code == 404
    # a hidden workspace answers as an unknown slug, whatever the write (issue #374, B-4093)
    nope = f"{A}/workspaces/nope"
    same_404(c.patch(SECOND, headers=key, json=change), c.patch(nope, headers=key, json=change))
    same_404(
        c.post(f"{SECOND}/projects", headers=key, json=new),
        c.post(f"{nope}/projects", headers=key, json=new),
    )
    unknown = c.patch(f"{nope}/projects/nope", headers=key, json=change)
    for hidden in (FAR, f"{SECOND}/projects/nope"):
        same_404(c.patch(hidden, headers=key, json=change), unknown)
    assert state() == before


def test_a_refused_write_on_a_hidden_workspace_is_still_recorded_as_denied(
    key: Hdr, layout: Layout, owner_session: Session
) -> None:
    def denied(action: str, resource: UUID) -> list[tuple[Any, ...]]:
        return rows(
            owner_session,
            "SELECT decision, reason_codes FROM shifde.policy_decisions "
            "WHERE tenant_id = :t AND action = :a AND resource_id = :r",
            t=layout.tenant,
            a=action,
            r=str(resource),
        )

    [(ws2, far)] = rows(
        owner_session,
        "SELECT w.id, p.id FROM shifde.workspaces w JOIN shifde.projects p "
        "ON p.workspace_id = w.id WHERE w.tenant_id = :t AND w.slug = 'zulu-w2'",
        t=layout.tenant,
    )
    assert denied("workspace:manage", ws2) == denied("project:manage", far) == []
    change = {"name": "renamed", "row_version": 1}
    for url in (SECOND, FAR):
        assert key.client.patch(url, headers=key, json=change).status_code == 404
    refused = [("deny", ["resource_scope"])]
    assert denied("workspace:manage", ws2) == denied("project:manage", far) == refused


# ------------------------------------------------------------------ dashboard
def test_the_dashboard_of_the_key_shows_no_activity_beyond_its_project(
    key: Hdr, layout: Layout, owner_session: Session
) -> None:
    in_main = "workspace_id = :w"
    for part in ("project_id = :p1", "project_id = :p2", "project_id IS NULL"):
        found = _recorded(
            owner_session, f"{in_main} AND {part}", w=layout.ws, p1=layout.p1, p2=layout.p2
        )
        assert found, part  # there is activity to leak in P1, in P2 and at the workspace level
    for params in ({}, {"workspace": "main"}):
        shown = _json(key, f"{A}/dashboard", **params)
        assert shown["recent_activity"] == [], params
        assert shown["counts"] == {"workspaces": 1, "projects": 1, "members": None}
        assert "zulu" not in json.dumps(shown) and str(layout.p2) not in json.dumps(shown)
    own = _recorded(owner_session, "project_id = :p", p=layout.p1)
    assert _activity(key, workspace="main", project="sample") == own != []
    assert not [t for _, t in own if t and "zulu-p2" in t]
    refused = key.client.get(
        f"{A}/dashboard", headers=key, params={"workspace": "main", "project": "zulu-p2"}
    )
    unknown = key.client.get(
        f"{A}/dashboard", headers=key, params={"workspace": "main", "project": "nope"}
    )
    same_404(refused, unknown)


def test_the_dashboard_activity_of_an_administrator_and_of_a_project_member_is_unchanged(
    mcp: TestClient, world: World, layout: Layout, owner_session: Session, owner_db: Database
) -> None:
    email = f"p1-{uuid7().hex[-8:]}@demo-a.local"
    uid = person(owner_session, email)
    owner_membership(owner_session, layout.tenant, uid)
    owner_session.commit()
    grant(owner_db, layout.tenant, uid, ["project:read"], scope_type="project", scope_id=layout.p1)
    member = user_session(mcp, {email: PASSWORD}, email)
    everything = _recorded(owner_session, "workspace_id = :w", w=layout.ws)
    in_p1 = _recorded(owner_session, "project_id = :p", p=layout.p1)
    assert len(everything) > len(in_p1) > 0
    assert _activity(member) == _activity(member, workspace="main") == everything
    assert _activity(member, workspace="main", project="sample") == in_p1
    admin = user_session(mcp, world.passwords, ADMIN_A)
    assert _activity(admin) == _recorded(owner_session, "true")
    assert _activity(admin, workspace="main") == everything
    assert _activity(admin, workspace="main", project="sample") == in_p1


# ------------------------------------------------------------------ another client
def test_every_path_of_another_client_answers_as_an_unknown_client(key: Hdr) -> None:
    for path in (
        "",
        "/workspaces",
        "/workspaces/main",
        "/workspaces/main/projects",
        "/workspaces/main/projects/sample",
        "/workspaces/main/projects/sample/board",
        "/workspaces/main/projects/nope",
        "/workspaces/nope",
        "/dashboard",
        "/dashboard?workspace=main&project=sample",
        "/connections",
        "/connections?workspace=main&include_shared=true",
        f"/connections/{uuid7()}",
        "/delivery/targets",
        "/delivery/targets?workspace=main",
        f"/delivery/targets/{uuid7()}",
    ):
        theirs = key.client.get(f"{B}{path}", headers=key)
        same_404(theirs, key.client.get(f"{NO_ORG}{path}", headers=key))


def test_another_clients_project_is_not_found_over_mcp(key: Hdr, owner_session: Session) -> None:
    def state() -> list[tuple[Any, ...]]:
        return rows(
            owner_session,
            "SELECT tenant_id::text, 'request', count(*) FROM shifde.assist_requests GROUP BY 1 "
            "UNION ALL SELECT tenant_id::text, 'job', count(*) FROM shifde.jobs GROUP BY 1 "
            "UNION ALL SELECT tenant_id::text, 'run', count(*) FROM shifde.runs GROUP BY 1 "
            "ORDER BY 1, 2",
        )

    secret = key["Authorization"].removeprefix("Bearer ")
    theirs, p1 = project(owner_session, ORG_B), project(owner_session, ORG_A)
    before = state()
    for project_id in (theirs, uuid7()):
        for tool, extra in (("board_get", {}), ("assist_ask", {"text": "hello"})):
            args = {"project_id": str(project_id), **extra}
            assert call(key.client, secret, tool, args) == (True, NOT_FOUND), (tool, project_id)
    assert state() == before
    assert ok(key.client, secret, "board_get", {"project_id": str(p1)})["projects"]
