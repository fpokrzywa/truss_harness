"""SP8-3: `board_get`, `assist_ask` and `assist_get` (SP8 design 3.3, 3.4 and 3.7; section 10 items 8
to 14 and 30): the defaults, client A against client B and project P1 against P2 for every id they
take, the routes' documents, the permissions, the audit, the arguments, and the spend cap of
`assist_ask` (the router call and the specialist's run) on its three paths."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any  # Any: JSON documents, row values, spy arguments
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shifde.assist import routing
from shifde.assist import service as assist
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import apikeys
from shifde.ledger.estimate import estimate_input_tokens
from shifde.runtime.service import RunService
from tests._support.assist_fixtures import (
    PICKS_COST,
    assist_model,
    read,
    request_row,
    route_jobs,
    run_route_job,
    sent,
    set_status,
)
from tests._support.fullapp import World
from tests._support.p2util import post
from tests._support.p4util import set_tenant_setting
from tests._support.p5util import Hdr, user_session
from tests._support.p8util import budget, project_scope, rate_card, rate_version
from tests._support.statements import statement_log
from tests.appgen.helpers import Calls, record_calls
from tests.assist.helpers import (
    add_project,
    enable,
    installed,
    project_of,
    proposal_row,
    rows,
    seed_proposal,
    set_settings,
)
from tests.board.conftest import (
    cells,
    deployment,
    evaluation_run,
    owner_run,
    plan,
    solution,
    target,
    without_time,
)
from tests.connections.routes import grant
from tests.identity.conftest import app_session, owner_membership, owner_user
from tests.ledger.util import add_budget
from tests.mcp_access.helpers import (
    ADMIN_A,
    ADMIN_B,
    ORG_A,
    ORG_B,
    TOOL_SCOPES,
    V1_TOOLS,
    A,
    audit_rows,
    call,
    make_key,
    mcp_rows,
    ok,
    project,
    refused,
    set_mcp,
    tenant,
)

ALL = sorted(V1_TOOLS)
NOT_FOUND = {"error": "not_found", "message": "not found"}
COST_TEXT = "how much did we spend last week"  # rule-routed to `cost`
VAGUE_TEXT = f"tell me a joke {PICKS_COST}"  # undecided: the router model picks `cost`
SENTINEL = "SENTINEL-sp83-51c0d7"
IN_RATE, OUT_RATE = Decimal(100), Decimal(200)  # per 1M tokens


@pytest.fixture
def admin(mcp: TestClient, world: World) -> Hdr:
    h = user_session(mcp, world.passwords, ADMIN_A)
    enable(h)
    return h


@pytest.fixture
def model_id(admin: Hdr) -> str:
    model = assist_model(admin.client, admin)
    installed(admin, model, keys=["cost"])
    return model


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    return record_calls(monkeypatch)


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> list[Decimal | None]:
    """The `max_cost` of every `RunService.create`, in order."""
    seen: list[Decimal | None] = []
    original = RunService.create

    def spy(self: RunService, body: Any, key: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("max_cost"))
        return original(self, body, key, **kwargs)

    monkeypatch.setattr(RunService, "create", spy)
    return seen


def _key(c: TestClient, world: World, **over: Any) -> str:
    """A key of A's administrator with every tool scope; every tool is allowed."""
    _, key = make_key(c, world, ADMIN_A, A, **over)
    return key


def _allow(c: TestClient, world: World, **over: Any) -> None:
    set_mcp(c, world, ADMIN_A, A, **{"allowed_tools": ALL, "rate_per_minute": 600, **over})


def _counts(owner: Session) -> list[tuple[Any, ...]]:
    return rows(
        owner,
        "SELECT tenant_id::text, 'request' AS what, count(*) FROM shifde.assist_requests GROUP BY 1 "
        "UNION ALL SELECT tenant_id::text, 'job', count(*) FROM shifde.jobs GROUP BY 1 ORDER BY 1, 2",
    )


def _objects(
    owner_db: Database, tenant_id: UUID, workspace_id: UUID, project_id: UUID
) -> dict[str, str]:
    """One object of each context kind in the project (its own target)."""
    sol = solution(owner_db, tenant_id, workspace_id, project_id)
    run = evaluation_run(owner_db, sol, sol.first, "passed")
    target_id = target(owner_db, tenant_id, workspace_id, project_id)
    owner_run(
        owner_db,
        "UPDATE shifde.delivery_targets SET bindings = CAST(:b AS jsonb) WHERE id = :g",
        b=json.dumps({"warehouse_id": "abc123def456"}),
        g=target_id,
    )
    plan_id = plan(owner_db, tenant_id, target_id, sol.first)
    made = deployment(owner_db, tenant_id, plan_id, target_id, "verified")
    ids = {
        "solution": sol.id,
        "release": sol.first,
        "target": target_id,
        "plan": plan_id,
        "deployment": made,
        "evaluation_run": run,
    }
    return {k: str(v) for k, v in ids.items()}


def _cases(
    mine: str, theirs: str, request: str, objects: dict[str, str]
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(tool, arguments naming a hidden id, the same with an unknown id) for every id the three
    tools take; `mine` is a project the key can ask in."""
    unknown = str(uuid7())
    ask = {"text": COST_TEXT}
    return [
        ("board_get", {"project_id": theirs}, {"project_id": unknown}),
        ("assist_ask", {"project_id": theirs, **ask}, {"project_id": unknown, **ask}),
        ("assist_get", {"request_id": request}, {"request_id": unknown}),
        *(
            (
                "assist_ask",
                {"project_id": mine, **ask, "context": {"kind": kind, "id": oid}},
                {"project_id": mine, **ask, "context": {"kind": kind, "id": unknown}},
            )
            for kind, oid in objects.items()
        ),
    ]


# ------------------------------------------------------------------ the default set
def test_assist_ask_is_opt_in_and_the_two_reads_are_default(mcp: TestClient, world: World) -> None:
    key = _key(mcp, world)
    _allow(mcp, world, allowed_tools=None)
    ask = {"project_id": str(uuid7()), "text": COST_TEXT}
    assert refused(mcp, key, "assist_ask", ask) == "tool_not_allowed"
    tools = ok(mcp, key, "whoami")["tools"]
    assert {"board_get", "assist_get"} <= set(tools) and "assist_ask" not in tools


# ------------------------------------------------------------------ isolation
def test_another_clients_ids_answer_as_unknown_ones_and_no_row_of_it_is_read(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    owner_db: Database,
    app_url: str,
) -> None:
    a, b = tenant(owner_session, ORG_A), tenant(owner_session, ORG_B)
    ws_a, p_a = (UUID(i) for i in project_of(owner_session, ORG_A))
    ws_b, p_b = (UUID(i) for i in project_of(owner_session, ORG_B))
    theirs = _objects(owner_db, b, ws_b, p_b)
    mine = _objects(owner_db, a, ws_a, p_a)
    payload = {"release_id": theirs["release"]}
    request_b, proposal_b = seed_proposal(
        owner_session, ORG_B, "evaluation_run", payload, email=ADMIN_B
    )
    key = _key(mcp, world)
    _allow(mcp, world)
    before, row_b = _counts(owner_session), request_row(owner_session, request_b)
    role = make_url(app_url).username or "shifde_app"
    with statement_log(role) as log:
        for tool, hidden, unknown in _cases(str(p_a), str(p_b), request_b, theirs):
            assert call(mcp, key, tool, hidden) == (True, NOT_FOUND), (tool, hidden)
            assert call(mcp, key, tool, unknown) == (True, NOT_FOUND), (tool, unknown)
    assert log.statements and log.rows  # the log saw the tools' reads
    ids_b = {str(b), str(ws_b), str(p_b), request_b, proposal_b, *theirs.values()}
    assert {v for v in log.values() if any(i in v for i in ids_b)} == set()
    assert _counts(owner_session) == before  # no request and no job, in either client
    assert request_row(owner_session, request_b) == row_b
    assert proposal_row(owner_session, proposal_b)["status"] == "open"
    # the controls: A's own project, request and contexts
    board = ok(mcp, key, "board_get", {"project_id": str(p_a)})
    assert [p["project"]["id"] for p in board["projects"]] == [str(p_a)]
    for kind, oid in mine.items():
        args = {"project_id": str(p_a), "text": COST_TEXT, "specialist": "cost"}
        asked = ok(mcp, key, "assist_ask", {**args, "context": {"kind": kind, "id": oid}})
        assert asked["status"] == "routed" and asked["context"] == {"kind": kind, "id": oid}
        got = ok(mcp, key, "assist_get", {"request_id": asked["id"]})
        assert got["id"] == asked["id"] and got["project_id"] == str(p_a)


def _p1_member_key(
    owner: Session, owner_db: Database, app_db: Database, settings: Settings, p1: UUID
) -> str:
    """The key of a member of A with `mcp:access` at organization level and everything else only
    on project `p1`."""
    a = tenant(owner, ORG_A)
    user = owner_user(owner, f"p1-{uuid7().hex[-8:]}@demo-a.local")
    owner_membership(owner, a, user)
    owner.commit()
    grant(owner_db, a, user, ["mcp:access"])
    grant(
        owner_db,
        a,
        user,
        [s for s in TOOL_SCOPES if s != "mcp:access"],
        scope_type="project",
        scope_id=p1,
    )
    s = app_session(app_db, a, user)
    try:
        _, secret = apikeys.mint(
            s,
            settings,
            tenant_id=a,
            name="p1 member key",
            principal_type="user",
            principal_id=user,
            scopes=TOOL_SCOPES,
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=user,
        )
        s.commit()
    finally:
        s.close()
    return secret


def _binding_scopes(owner: Session, email: str) -> list[str]:
    """The scope types of every role binding of the person with `email`."""
    found = rows(
        owner,
        "SELECT DISTINCT b.scope_type FROM shifde.role_bindings b "
        "JOIN shifde.users u ON u.id = b.subject_id WHERE u.primary_email = :e ORDER BY 1",
        e=email,
    )
    return [str(r[0]) for r in found]


@pytest.mark.parametrize("who", ["scoped key", "organization grants only", "p1 member"])
def test_a_p1_key_gets_not_found_for_p2(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    settings: Settings,
    owner_session: Session,
    owner_db: Database,
    app_db: Database,
    who: str,
) -> None:
    add_project(admin, "other")
    installed(admin, model_id, project="/workspaces/main/projects/other", keys=["cost"])
    a = tenant(owner_session, ORG_A)
    ws, p1 = (UUID(i) for i in project_of(owner_session, ORG_A))
    _, p2 = (UUID(i) for i in project_of(owner_session, ORG_A, "other"))
    theirs, mine = _objects(owner_db, a, ws, p2), _objects(owner_db, a, ws, p1)
    payload = {"release_id": theirs["release"]}
    request_2, proposal_2 = seed_proposal(
        owner_session, ORG_A, "evaluation_run", payload, project="other"
    )
    if who == "scoped key":
        # a key whose owner holds both kinds of grant: organization-level and one at P1
        [(admin_id,)] = rows(
            owner_session, "SELECT id FROM shifde.users WHERE primary_email = :e", e=ADMIN_A
        )
        grant(owner_db, a, admin_id, ["project:read"], scope_type="project", scope_id=p1)
    if who == "organization grants only":
        assert _binding_scopes(owner_session, ADMIN_A) == ["tenant"]
    if who != "p1 member":
        scope = {"workspace_ids": [], "project_ids": [str(p1)]}
        key = _key(mcp, world, resource_scope=scope)
    else:
        key = _p1_member_key(owner_session, owner_db, app_db, settings, p1)
    _allow(mcp, world)
    before = _counts(owner_session)
    for tool, hidden, unknown in _cases(str(p1), str(p2), request_2, theirs):
        assert call(mcp, key, tool, hidden) == (True, NOT_FOUND), (tool, hidden)
        assert call(mcp, key, tool, unknown) == (True, NOT_FOUND), (tool, unknown)
    assert _counts(owner_session) == before
    # P1 answers, and names nothing of P2
    board = ok(mcp, key, "board_get", {"project_id": str(p1)})
    assert board["projects"][0]["solutions"][0]["id"] == mine["solution"]
    results = [board]
    for kind in ("solution", "release", "deployment"):
        args = {"project_id": str(p1), "text": COST_TEXT, "specialist": "cost"}
        asked = ok(mcp, key, "assist_ask", {**args, "context": {"kind": kind, "id": mine[kind]}})
        assert (asked["status"], asked["status_reason"]) == ("routed", None)
        results += [asked, ok(mcp, key, "assist_get", {"request_id": asked["id"]})]
    ids_2 = {str(p2), request_2, proposal_2, *theirs.values()}
    shown = json.dumps(results)
    assert {i for i in ids_2 if i in shown} == set()


def test_a_p1_key_gets_not_found_for_another_workspace_and_a_workspace_level_target(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    owner_db: Database,
) -> None:
    """Issue #359: the key's owner holds only organization-level grants; P1 is the control."""
    second = {"slug": "second", "name": "second"}
    ws2 = UUID(post(admin.client, f"{A}/workspaces", admin, second)["id"])
    body = {"slug": "far", "name": "far"}
    far = UUID(post(admin.client, f"{A}/workspaces/second/projects", admin, body)["id"])
    installed(admin, model_id, project="/workspaces/second/projects/far", keys=["cost"])
    a = tenant(owner_session, ORG_A)
    ws, p1 = (UUID(i) for i in project_of(owner_session, ORG_A))
    shared = target(owner_db, a, ws)  # a target of the workspace, in no project
    theirs = _objects(owner_db, a, ws2, far)
    assert _binding_scopes(owner_session, ADMIN_A) == ["tenant"]
    key = _key(mcp, world, resource_scope={"workspace_ids": [], "project_ids": [str(p1)]})
    _allow(mcp, world)
    before = _counts(owner_session)
    unknown, ask = str(uuid7()), {"text": COST_TEXT, "specialist": "cost"}
    for project_id in (str(far), unknown):
        for tool, args in (("board_get", {}), ("assist_ask", ask)):
            got = call(mcp, key, tool, {"project_id": project_id, **args})
            assert got == (True, NOT_FOUND), (tool, project_id)
    for kind, oid in (("target", str(shared)), *theirs.items(), ("target", unknown)):
        context = {"context": {"kind": kind, "id": oid}}
        got = call(mcp, key, "assist_ask", {"project_id": str(p1), **ask, **context})
        assert got == (True, NOT_FOUND), (kind, oid)
    assert _counts(owner_session) == before
    board = ok(mcp, key, "board_get", {"project_id": str(p1)})
    assert [p["project"]["id"] for p in board["projects"]] == [str(p1)]
    assert str(shared) not in json.dumps(board)
    assert ok(mcp, key, "assist_ask", {"project_id": str(p1), **ask})["status"] == "routed"


# ------------------------------------------------------------------ board_get
def test_board_get_is_the_board_route_and_the_boards_own_reads_apply(
    mcp: TestClient, world: World, owner_session: Session, owner_db: Database
) -> None:
    a = tenant(owner_session, ORG_A)
    ws, p = (UUID(i) for i in project_of(owner_session, ORG_A))
    sol = solution(owner_db, a, ws, p)
    t = target(owner_db, a, ws, p)
    deployment(owner_db, a, plan(owner_db, a, t, sol.first), t, "verified")
    args = {"project_id": str(p)}

    def board(*missing: str) -> dict[str, Any]:
        """`board_get` of a key without `missing`: the same document as the route gives it."""
        key = _key(mcp, world, scopes=[s for s in TOOL_SCOPES if s not in missing])
        got = ok(mcp, key, "board_get", args)
        mcp.cookies.clear()
        auth = {"Authorization": f"Bearer {key}"}
        r = mcp.get(f"{A}/workspaces/main/projects/sample/board", headers=auth)
        assert r.status_code == 200, r.text
        assert without_time(got) == without_time(r.json())
        return got

    _allow(mcp, world)
    cell = cells(board(), sol.id, t)
    assert cell["current"] and cell["last_attempt"]
    cell = cells(board("delivery:deployment:read"), sol.id, t)
    assert cell["current"] is None and cell["last_attempt"] is None
    assert [x["solutions"] for x in board("packaging:solution:read")["projects"]] == [[]]


# ------------------------------------------------------------------ permissions
@pytest.mark.parametrize(
    ("permission", "tool"),
    [
        ("project:read", "board_get"),
        ("assist:request:write", "assist_ask"),
        ("assist:request:read", "assist_get"),
    ],
)
def test_a_missing_permission_is_forbidden_and_stores_nothing(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    permission: str,
    tool: str,
) -> None:
    request, _ = seed_proposal(owner_session, ORG_A, "evaluation_run", {"release_id": "x"})
    p = str(project(owner_session, ORG_A))
    args: dict[str, dict[str, Any]] = {
        "board_get": {"project_id": p},
        "assist_ask": {"project_id": p, "text": COST_TEXT},
        "assist_get": {"request_id": request},
    }
    key = _key(mcp, world, scopes=[s for s in TOOL_SCOPES if s != permission])
    _allow(mcp, world)
    before = _counts(owner_session)
    assert refused(mcp, key, tool, args[tool]) == "forbidden"
    # no oracle: an unknown id gets the same answer
    unknown = {k: (str(uuid7()) if k.endswith("_id") else v) for k, v in args[tool].items()}
    a = tenant(owner_session, ORG_A)
    [refusal] = mcp_rows(owner_session, a, "mcp.access_refused")
    assert (refusal["details"]["reason"], refusal["details"]["tool"]) == ("permission", tool)
    assert call(mcp, key, tool, args[tool]) == call(mcp, key, tool, unknown)
    assert mcp_rows(owner_session, tenant(owner_session, ORG_B), "mcp.access_refused") == []
    assert _counts(owner_session) == before


# ------------------------------------------------------------------ assist_ask and assist_get
def test_a_rule_routed_ask_runs_as_the_key_and_assist_get_reads_it(
    mcp: TestClient, world: World, admin: Hdr, model_id: str, owner_session: Session
) -> None:
    key_id, key = make_key(mcp, world, ADMIN_A, A)
    _allow(mcp, world)
    p = str(project(owner_session, ORG_A))
    asked = ok(mcp, key, "assist_ask", {"project_id": p, "text": f"  {COST_TEXT}  "})
    assert (asked["status"], asked["status_reason"]) == ("routed", None)
    assert asked["project_id"] == p and asked["specialist_key"] == "cost" and asked["run_id"]
    assert request_row(owner_session, asked["id"])["text"] == COST_TEXT
    [(requested_by, run_key)] = rows(
        owner_session,
        "SELECT r.requested_by::text, u.api_key_id::text FROM shifde.assist_requests r "
        "JOIN shifde.runs u ON u.id = r.run_id WHERE r.tenant_id = :t",
        t=tenant(owner_session, ORG_A),
    )
    [(principal,)] = rows(
        owner_session, "SELECT id::text FROM shifde.users WHERE primary_email = :e", e=ADMIN_A
    )
    assert requested_by == asked["requested_by"] == principal and run_key == key_id
    got = ok(mcp, key, "assist_get", {"request_id": asked["id"]})
    assert got == {**asked, "updated_at": got["updated_at"]}
    assert got == read(admin.client, admin, asked["id"])  # the route's document
    ok(mcp, key, "board_get", {"project_id": p})
    called = mcp_rows(owner_session, tenant(owner_session, ORG_A), "mcp.tool_called")
    run_ids = [(r["details"]["tool"], r["details"].get("run_id")) for r in called]
    assert run_ids == [
        ("assist_ask", asked["run_id"]),
        ("assist_get", None),
        ("board_get", None),
    ]


def test_a_refused_ask_is_a_result_without_a_run(
    mcp: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    key = _key(mcp, world)
    _allow(mcp, world)
    p = str(project(owner_session, ORG_A))
    asked = ok(mcp, key, "assist_ask", {"project_id": p, "text": COST_TEXT})
    assert (asked["status"], asked["status_reason"]) == ("refused", "specialists_not_installed")
    [called] = mcp_rows(owner_session, tenant(owner_session, ORG_A), "mcp.tool_called")
    assert called["outcome"] == "success" and "run_id" not in called["details"]


def test_assist_get_withholds_a_proposals_payload_and_decides_nothing(
    mcp: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    payload = {
        "target_id": str(uuid7()),
        "release_id": str(uuid7()),
        "application_key": "sales-app",
        "display_name": "Sales app",
    }
    request, proposal = seed_proposal(owner_session, ORG_A, "delivery_plan", payload)
    reader = _key(mcp, world)
    masked = _key(mcp, world, scopes=[s for s in TOOL_SCOPES if s != "delivery:plan:read"])
    _allow(mcp, world)
    before = proposal_row(owner_session, proposal)
    [shown] = ok(mcp, reader, "assist_get", {"request_id": request})["proposals"]
    [hidden] = ok(mcp, masked, "assist_get", {"request_id": request})["proposals"]
    assert (shown["id"], shown["status"], shown["payload"]) == (proposal, "open", payload)
    assert hidden == {**shown, "payload": None}
    assert proposal_row(owner_session, proposal) == before and before["status"] == "open"


# ------------------------------------------------------------------ arguments
@pytest.mark.parametrize(
    "change",
    [
        {"max_cost": 1},
        {"workspace_id": "main"},
        {"context": {"kind": "solution", "id": str(uuid7()), "tenant_id": "x"}},
        {"context": {"kind": "connection", "id": str(uuid7())}},
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 4001},
        {"text": "a\x00b"},
    ],
)
def test_bad_ask_arguments_are_invalid_arguments(
    mcp: TestClient, world: World, admin: Hdr, owner_session: Session, change: dict[str, Any]
) -> None:
    key = _key(mcp, world)
    _allow(mcp, world)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": COST_TEXT, **change}
    before = _counts(owner_session)
    assert refused(mcp, key, "assist_ask", args) == "invalid_arguments"
    assert _counts(owner_session) == before


def test_an_unknown_specialist_stores_nothing_and_4000_characters_are_accepted(
    mcp: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    key = _key(mcp, world)
    _allow(mcp, world)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": "x" * 4000}
    before = _counts(owner_session)
    assert refused(mcp, key, "assist_ask", {**args, "specialist": "nobody"}) == "specialist_unknown"
    assert _counts(owner_session) == before
    assert ok(mcp, key, "assist_ask", args)["status_reason"] == "route_unclear"


def test_no_argument_of_the_three_tools_reaches_an_audit_row_or_a_log(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = _key(mcp, world)
    _allow(mcp, world)
    p = str(project(owner_session, ORG_A))
    capsys.readouterr()
    caplog.clear()
    ask = {"project_id": p, "text": f"{COST_TEXT} {SENTINEL}"}
    asked = ok(mcp, key, "assist_ask", ask)
    assert asked["status"] == "routed" and SENTINEL not in json.dumps(asked)
    ok(mcp, key, "assist_get", {"request_id": asked["id"]})
    ok(mcp, key, "board_get", {"project_id": p})
    bad: list[tuple[str, dict[str, Any]]] = [
        ("assist_ask", {**ask, "specialist": SENTINEL}),
        ("assist_ask", {**ask, SENTINEL: SENTINEL}),
        ("assist_ask", {**ask, "context": {"kind": SENTINEL, "id": SENTINEL}}),
        ("assist_ask", {**ask, "text": f"\x00 {SENTINEL}"}),
        ("assist_get", {"request_id": SENTINEL}),
        ("assist_get", {"request_id": asked["id"], "note": SENTINEL}),
        ("board_get", {"project_id": p, "note": SENTINEL}),
    ]
    for tool, args in bad:
        assert call(mcp, key, tool, args)[0] is True, (tool, args)

    def broken(text_in: str, *_: Any) -> Any:  # an unexpected error whose message holds the text
        raise RuntimeError(text_in)

    with monkeypatch.context() as m:
        m.setattr(routing, "route", broken)
        assert refused(mcp, key, "assist_ask", ask) == "internal_error"
    logged = capsys.readouterr()
    assert "mcp.tool_failed" in logged.out + caplog.text
    assert SENTINEL not in logged.out + logged.err and SENTINEL not in caplog.text
    rows_ = audit_rows(owner_session)
    assert rows_ and all(SENTINEL not in json.dumps(r["details"], default=str) for r in rows_)
    called = [r for r in rows_ if r["action"] == "mcp.tool_called"]
    assert len(called) == 3 + len(bad) + 1


# ------------------------------------------------------------------ the spend cap
def _worst(text_in: str) -> Decimal:
    """The router call's worst case for a request without candidates, at `IN_RATE`/`OUT_RATE`."""
    user = json.dumps({"text": text_in, "candidates": []})
    tokens = estimate_input_tokens([{"content": assist.ROUTER_PROMPT}, {"content": user}])
    return (IN_RATE * tokens + OUT_RATE * assist.ROUTER_MAX_TOKENS) / 1_000_000


def _price(admin: Hdr, model_key: str = "fake-chat") -> None:
    card = rate_card(admin.client, admin, model_key=model_key)
    rate_version(
        admin.client,
        admin,
        card["id"],
        input_rate=float(IN_RATE),
        output_rate=float(OUT_RATE),
    )


def _route_by_model(owner: Session, model: str) -> None:
    set_settings(owner, ORG_A, model_routing_enabled=True, router_model_id=model)


def _requeue(owner: Session, request_id: str) -> None:
    """A crash after the handover started: the request is `handing_over`, its job queued again."""
    set_status(owner, request_id, "handing_over")
    owner.execute(
        text("UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL WHERE id = :j"),
        {"j": request_row(owner, request_id)["job_id"]},
    )
    owner.commit()


def _no_spend(owner: Session, calls: Calls) -> None:
    assert calls.messages == []
    assert rows(owner, "SELECT count(*) FROM shifde.usage_events") == [(0,)]


@pytest.mark.parametrize(
    ("specialist_max_cost", "expected"), [("1", Decimal("0.5")), ("0.2", Decimal("0.2"))]
)
def test_a_rule_routed_ask_is_capped_inline_and_in_recovery(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    caps: list[Decimal | None],
    specialist_max_cost: str,
    expected: Decimal,
) -> None:
    set_settings(owner_session, ORG_A, specialist_max_cost=specialist_max_cost)
    _price(admin)  # no router call is made: its price takes nothing off a rule-routed run
    key = _key(mcp, world)
    _allow(mcp, world, max_cost_per_call=0.5)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": COST_TEXT}
    asked = ok(mcp, key, "assist_ask", args)
    assert asked["status"] == "routed"
    [(_, _, payload)] = route_jobs(owner_session, asked["id"])
    assert Decimal(payload["max_cost"]) == Decimal("0.5") and isinstance(payload["max_cost"], str)
    # the recovery of a handover that started no run creates it under the same cap
    _requeue(owner_session, asked["id"])
    assert run_route_job(mcp, owner_session, asked["id"])
    assert request_row(owner_session, asked["id"])["status"] == "routed"
    assert caps == [expected, expected]
    [(stored,)] = rows(
        owner_session,
        "SELECT content->'overrides'->>'max_cost' FROM shifde.run_artifacts "
        "WHERE run_id = :r AND kind = 'input'",
        r=asked["run_id"],
    )
    assert Decimal(stored) == expected


def test_a_model_routed_ask_leaves_the_run_the_cap_less_the_routers_worst_case(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    caps: list[Decimal | None],
    calls: Calls,
) -> None:
    _route_by_model(owner_session, model_id)
    _price(admin)
    key = _key(mcp, world)
    _allow(mcp, world, max_cost_per_call=0.5)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": VAGUE_TEXT}
    asked = ok(mcp, key, "assist_ask", args)
    assert asked["status"] == "queued" and caps == []
    assert run_route_job(mcp, owner_session, asked["id"])
    done = ok(mcp, key, "assist_get", {"request_id": asked["id"]})
    assert (done["status"], done["route_method"]) == ("routed", "model")
    _requeue(owner_session, asked["id"])
    assert run_route_job(mcp, owner_session, asked["id"])
    assert request_row(owner_session, asked["id"])["status"] == "routed"
    left = Decimal("0.5") - _worst(VAGUE_TEXT)
    assert Decimal("0.2") < left < Decimal("0.5")
    assert caps == [left, left]
    assert len(calls.messages) == 1  # the router, once
    # the organization's own cap still applies when it is the lower one
    set_settings(owner_session, ORG_A, specialist_max_cost="0.1")
    asked = ok(mcp, key, "assist_ask", args)
    assert run_route_job(mcp, owner_session, asked["id"])
    assert caps[2:] == [Decimal("0.1")]


def test_the_http_route_is_uncapped_and_its_job_payload_unchanged(
    admin: Hdr, model_id: str, owner_session: Session, caps: list[Decimal | None]
) -> None:
    set_settings(owner_session, ORG_A, specialist_max_cost="3")
    _route_by_model(owner_session, model_id)
    _price(admin)
    inline = sent(admin.client, admin, {"text": COST_TEXT})
    queued = sent(admin.client, admin, {"text": VAGUE_TEXT}, 202)
    for out in (inline, queued):
        [(_, _, payload)] = route_jobs(owner_session, out["id"])
        assert set(payload) == {"request_id", "principal_id", "principal_type", "api_key_id"}
    assert run_route_job(admin.client, owner_session, queued["id"])
    assert read(admin.client, admin, queued["id"])["status"] == "routed"
    assert caps == [Decimal(3), Decimal(3)]


@pytest.mark.parametrize("why", ["above the cap", "no rate"])
def test_a_router_call_the_cap_cannot_cover_is_budget_denied_before_it_is_sent(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    caps: list[Decimal | None],
    calls: Calls,
    why: str,
) -> None:
    _route_by_model(owner_session, model_id)
    add_budget(owner_session, tenant(owner_session, ORG_A), amount="1000000")  # a call would hold
    owner_session.commit()
    if why == "above the cap":
        _price(admin)
        cost = 0.01
        assert _worst(VAGUE_TEXT) > Decimal("0.01")
    else:
        set_tenant_setting(owner_session, ORG_A, "unknown_cost_policy", "deny")
        cost = 25.0
    key = _key(mcp, world)
    _allow(mcp, world, max_cost_per_call=cost)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": VAGUE_TEXT}
    asked = ok(mcp, key, "assist_ask", args)
    assert asked["status"] == "queued"
    assert run_route_job(mcp, owner_session, asked["id"])
    done = ok(mcp, key, "assist_get", {"request_id": asked["id"]})
    assert (done["status"], done["status_reason"]) == ("failed", "budget_denied")
    assert done["run"] is None and caps == []
    _no_spend(owner_session, calls)
    assert rows(owner_session, "SELECT count(*) FROM shifde.budget_reservations") == [(0,)]
    [(actions,)] = rows(
        owner_session,
        "SELECT array_agg(action ORDER BY chain_seq) FROM shifde.audit_events WHERE target_id = :r",
        r=asked["id"],
    )
    assert actions == ["assist.request.created", "assist.request.failed"]  # never `classifying`


def test_an_unpriced_router_model_leaves_the_cap_unreduced_by_default(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    caps: list[Decimal | None],
    calls: Calls,
) -> None:
    _route_by_model(owner_session, model_id)
    key = _key(mcp, world)
    _allow(mcp, world, max_cost_per_call=0.5)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": VAGUE_TEXT}
    asked = ok(mcp, key, "assist_ask", args)
    assert run_route_job(mcp, owner_session, asked["id"])
    assert request_row(owner_session, asked["id"])["status"] == "routed"
    assert caps == [Decimal("0.5")] and len(calls.messages) == 1


def test_a_recovery_whose_router_price_left_no_cap_starts_no_run(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _route_by_model(owner_session, model_id)
    key = _key(mcp, world)
    _allow(mcp, world, max_cost_per_call=0.01)
    args = {"project_id": str(project(owner_session, ORG_A)), "text": VAGUE_TEXT}
    asked = ok(mcp, key, "assist_ask", args)

    def crash(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the worker died")

    with monkeypatch.context() as m:
        m.setattr(RunService, "create", crash)
        with pytest.raises(RuntimeError):
            run_route_job(mcp, owner_session, asked["id"])
    assert request_row(owner_session, asked["id"])["status"] == "handing_over"
    _price(admin)  # since the router call
    _requeue(owner_session, asked["id"])
    assert run_route_job(mcp, owner_session, asked["id"])
    row = request_row(owner_session, asked["id"])
    assert (row["status"], row["status_reason"]) == ("failed", "budget_denied")
    assert rows(owner_session, "SELECT count(*) FROM shifde.runs") == [(0,)]


def test_an_exhausted_budget_refuses_both_paths_before_any_model_call(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    calls: Calls,
) -> None:
    _route_by_model(owner_session, model_id)
    budget(admin.client, admin, project_scope(owner_session), metric="tokens", amount=0)
    key = _key(mcp, world)
    _allow(mcp, world)
    p = str(project(owner_session, ORG_A))
    asked = ok(mcp, key, "assist_ask", {"project_id": p, "text": COST_TEXT})
    assert (asked["status"], asked["status_reason"]) == ("failed", "budget_denied")
    queued = ok(mcp, key, "assist_ask", {"project_id": p, "text": VAGUE_TEXT})
    assert queued["status"] == "queued"
    assert run_route_job(mcp, owner_session, queued["id"])
    assert request_row(owner_session, queued["id"])["status"] == "failed"
    _no_spend(owner_session, calls)
