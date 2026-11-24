"""SP7-4 (design sections 6, 9.6): a person accepts a specialist's proposal through the owning
service, as themselves, in the request's project.

A delivery-plan proposal accepted by an FDE (developer keys plus `delivery:plan:approve`) becomes a
`requested` plan requested by that FDE, `result_ref {kind: delivery_plan, id}`; the FDE's own
approval of it is 403 `self_approval_forbidden` and a second administrator's approval of the
sealed plan succeeds. A second accept answers the proposal unchanged; a dismissed or failed one is
409 `proposal_not_open`, and two concurrent accepts create one plan. The evaluation-run and
app-request kinds accept the same way. A payload changed since (another client's or another
project's release) fails the proposal with the service's code and creates nothing anywhere.
Organization B's administrator gets 404 on A's proposal, a P1-only developer 404 on P2's, a viewer
403 on accept; a viewer reads the request with every payload and `result_ref` null, a developer
with the read keys sees them. Each test has a database of its own (the full-app harness).
"""

from __future__ import annotations

import threading
from typing import Any  # Any: JSON bodies and row values

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import new_user, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, VIEWER, A, B
from tests._support.p5util import Hdr, limited_user, user_session
from tests.appgen.helpers import TABLES, databricks, wire
from tests.assist.helpers import (
    add_project,
    decide,
    model,
    project_member,
    project_of,
    proposal_row,
    rows,
    seed_proposal,
    set_payload,
)
from tests.delivery.releases import make_release
from tests.delivery.routes import (
    address,
    create_executor,
    issue,
    lease_plan,
    plan_result,
    plans,
    register,
    target_body,
)
from tests.evaluation.helpers import case
from tests.evaluation.helpers import release as evaluated_release

TENANT_ADMIN = "01900000-0000-7000-8000-000000000001"
PASSWORD = "a-long-password-1"
FDE = "fde@demo-a.local"
FDE_PERMS = [
    "workspace:read", "project:read", "assist:request:read", "assist:proposal:accept",
    "packaging:solution:read", "delivery:target:read", "delivery:plan:read",
    "delivery:plan:request", "delivery:plan:approve", "delivery:deployment:read",
]  # fmt: skip


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    return user_session(client, world.passwords, ADMIN)


def _ids(owner: Session, org_slug: str = "demo-a", project: str = "sample") -> tuple[Any, Any]:
    ws, proj = project_of(owner, org_slug, project)
    return ws, proj


def _release(
    owner_db: Database, owner: Session, org_slug: str = "demo-a", project: str = "sample"
) -> str:
    ws, proj = _ids(owner, org_slug, project)
    return str(make_release(owner_db, tenant_id(owner, org_slug), ws, proj).id)


def _target(admin: Hdr, org_slug: str = "demo-a", project: str = "sample") -> str:
    body = target_body("main", address(), project=project)
    out = register(admin.client, admin, org_slug, body)
    return str(out["id"])


def _plan_payload(target_id: str, release_id: str) -> dict[str, Any]:
    return {"target_id": target_id, "release_id": release_id, "application_key": "sales-app",
            "display_name": "Sales app"}  # fmt: skip


def _fde(client: TestClient, world: World, owner: Session, admin: Hdr) -> Hdr:
    limited_user(admin.client, owner, admin, world.passwords, FDE, FDE_PERMS)
    return user_session(client, world.passwords, FDE)


def _plans(owner: Session) -> list[tuple[Any, ...]]:
    return rows(owner, "SELECT id::text, tenant_id, requested_by::text, status "
                       "FROM shifde.delivery_plans ORDER BY id")  # fmt: skip


def _ok(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def test_an_fde_accepts_a_plan_proposal_and_a_second_administrator_approves(
    client: TestClient, world: World, admin: Hdr, owner_session: Session, owner_db: Database
) -> None:
    target, release = _target(admin), _release(owner_db, owner_session)
    executor = create_executor(admin.client, admin, "demo-a")
    token = issue(admin.client, admin, "demo-a", executor["id"])["secret"]
    fde = _fde(client, world, owner_session, admin)
    _, proposal = seed_proposal(owner_session, "demo-a", "delivery_plan",
                                _plan_payload(target, release))  # fmt: skip
    out = _ok(decide(fde, proposal, "accept"))
    assert out["status"] == "accepted" and out["result_ref"]["kind"] == "delivery_plan"
    plan_id = out["result_ref"]["id"]
    [(pid, _, requested_by, status)] = _plans(owner_session)
    fde_id = rows(owner_session, "SELECT id::text FROM shifde.users WHERE primary_email = :e",
                  e=FDE)[0][0]  # fmt: skip
    assert (pid, requested_by, status) == (plan_id, fde_id, "requested")
    assert out["decided_by"] == fde_id and out["decided_at"] is not None
    actions = rows(owner_session, "SELECT action, details FROM shifde.audit_events "
                                  "WHERE target_id = :p ORDER BY chain_seq", p=proposal)  # fmt: skip
    assert [a for a, _ in actions] == ["assist.proposal.accepted"]
    assert actions[0][1]["result_ref"] == out["result_ref"] and "payload" not in actions[0][1]
    # the accepting person is the plan's requester: their own approval is refused
    refused = fde.client.post(f"{plans('demo-a')}/{plan_id}/approve", headers=fde,
                              json={"plan_digest": "0" * 64})  # fmt: skip
    assert refused.status_code == 403 and refused.json()["code"] == "self_approval_forbidden"
    # the executor seals it; a second administrator approves
    gateway = TestClient(client.app, base_url="https://testserver")  # no session: bearer only
    leased = lease_plan(gateway, token)
    assert leased["job"]["plan_id"] == plan_id
    assert plan_result(gateway, token, leased).status_code == 200
    new_user(admin.client, admin, "demo-a", "admin2@demo-a.local", PASSWORD, [TENANT_ADMIN])
    world.passwords["admin2@demo-a.local"] = PASSWORD
    second = user_session(client, world.passwords, "admin2@demo-a.local")
    sealed = _ok(second.client.get(f"{plans('demo-a')}/{plan_id}", headers=second))
    approved = second.client.post(f"{plans('demo-a')}/{plan_id}/approve", headers=second,
                                  json={"plan_digest": sealed["digest"]})  # fmt: skip
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    # accepting again answers the proposal unchanged and creates nothing
    again = _ok(decide(fde, proposal, "accept"))
    assert again == out
    assert len(_plans(owner_session)) == 1


def test_accept_after_dismiss_and_dismiss_after_accept_are_409(
    admin: Hdr, owner_session: Session, owner_db: Database
) -> None:
    target, release = _target(admin), _release(owner_db, owner_session)
    _, first = seed_proposal(
        owner_session, "demo-a", "delivery_plan", _plan_payload(target, release)
    )
    dismissed = _ok(decide(admin, first, "dismiss"))
    assert dismissed["status"] == "dismissed"
    assert _ok(decide(admin, first, "dismiss")) == dismissed  # idempotent
    r = decide(admin, first, "accept")
    assert r.status_code == 409 and r.json()["code"] == "proposal_not_open"
    _, second = seed_proposal(
        owner_session, "demo-a", "delivery_plan", _plan_payload(target, release)
    )
    assert _ok(decide(admin, second, "accept"))["status"] == "accepted"
    r = decide(admin, second, "dismiss")
    assert r.status_code == 409 and r.json()["code"] == "proposal_not_open"
    assert len(_plans(owner_session)) == 1


def test_two_concurrent_accepts_create_one_plan(
    client: TestClient, world: World, admin: Hdr, owner_session: Session, owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from shifde.delivery import plans as plans_module

    target, release = _target(admin), _release(owner_db, owner_session)
    _, proposal = seed_proposal(owner_session, "demo-a", "delivery_plan",
                                _plan_payload(target, release))  # fmt: skip
    entered = threading.Event()
    original = plans_module.PlanService.request

    def slow(self: Any, body: Any) -> Any:  # Any: the service and its body
        entered.set()
        threading.Event().wait(0.5)  # the second accept arrives while this one holds the claim
        return original(self, body)

    monkeypatch.setattr(plans_module.PlanService, "request", slow)
    sessions = [user_session(client, world.passwords, ADMIN) for _ in range(2)]
    answers: list[httpx.Response] = []

    def accept(h: Hdr) -> None:
        answers.append(decide(h, proposal, "accept"))

    first = threading.Thread(target=accept, args=(sessions[0],))
    first.start()
    assert entered.wait(10)
    accept(sessions[1])
    first.join(30)
    codes = sorted(r.status_code for r in answers)
    assert codes == [200, 409], [r.text for r in answers]
    assert next(r for r in answers if r.status_code == 409).json()["code"] == "proposal_not_open"
    assert len(_plans(owner_session)) == 1
    assert proposal_row(owner_session, proposal)["status"] == "accepted"


def test_an_evaluation_run_proposal_is_accepted_through_evaluation(
    admin: Hdr, owner_session: Session
) -> None:
    rel = evaluated_release(admin, owner_session, [case("c1", "hello", ("contains", "fake"))])
    _, proposal = seed_proposal(owner_session, "demo-a", "evaluation_run", {"release_id": rel.id})
    out = _ok(decide(admin, proposal, "accept"))
    assert out["status"] == "accepted" and out["result_ref"]["kind"] == "evaluation_run"
    [(release_id, requested_by)] = rows(
        owner_session, "SELECT release_id::text, requested_by::text FROM shifde.evaluation_runs "
        "WHERE id = :e", e=out["result_ref"]["id"],
    )  # fmt: skip
    assert release_id == rel.id and requested_by == out["decided_by"]


def test_an_app_request_proposal_is_accepted_through_appgen(
    admin: Hdr, owner_session: Session
) -> None:
    wire(admin.client)
    connection = databricks(admin.client, admin, scope={"workspace": "main", "project": "sample"})
    payload = {"connection_id": connection, "tables": TABLES, "brief": "A sales app",
               "model_id": model(admin)}  # fmt: skip
    _, proposal = seed_proposal(owner_session, "demo-a", "appgen_request", payload)
    out = _ok(decide(admin, proposal, "accept"))
    assert out["status"] == "accepted" and out["result_ref"]["kind"] == "appgen_request"
    [(project_id, requested_by)] = rows(
        owner_session, "SELECT project_id::text, requested_by::text FROM shifde.appgen_requests "
        "WHERE id = :r", r=out["result_ref"]["id"],
    )  # fmt: skip
    assert project_id == project_of(owner_session, "demo-a")[1]
    assert requested_by == out["decided_by"]


@pytest.mark.parametrize("where", ["client B", "project P2"])
def test_a_payload_changed_to_a_foreign_release_fails_and_creates_nothing(
    admin: Hdr, owner_session: Session, owner_db: Database, where: str
) -> None:
    target, release = _target(admin), _release(owner_db, owner_session)
    _, plan_proposal = seed_proposal(owner_session, "demo-a", "delivery_plan",
                                     _plan_payload(target, release))  # fmt: skip
    _, eval_proposal = seed_proposal(owner_session, "demo-a", "evaluation_run",
                                     {"release_id": release})  # fmt: skip
    if where == "client B":
        foreign = _release(owner_db, owner_session, "demo-b")
    else:
        add_project(admin, "other")
        foreign = _release(owner_db, owner_session, "demo-a", "other")
    set_payload(owner_session, plan_proposal, _plan_payload(target, foreign))
    set_payload(owner_session, eval_proposal, {"release_id": foreign})
    for proposal in (plan_proposal, eval_proposal):
        out = _ok(decide(admin, proposal, "accept"))
        assert (out["status"], out["status_reason"], out["result_ref"]) == (
            "failed", "not_found", None,
        )  # fmt: skip
        [(action,)] = rows(owner_session, "SELECT action FROM shifde.audit_events "
                                          "WHERE target_id = :p", p=proposal)  # fmt: skip
        assert action == "assist.proposal.failed"
        r = decide(admin, proposal, "accept")
        assert r.status_code == 409 and r.json()["code"] == "proposal_not_open"
        assert _ok(decide(admin, proposal, "dismiss"))["status"] == "dismissed"
    assert _plans(owner_session) == []
    # only the passed runs make_release inserted: no evaluation was requested anywhere
    assert rows(owner_session, "SELECT count(*) FROM shifde.evaluation_runs "
                               "WHERE status <> 'passed'") == [(0,)]  # fmt: skip


def test_bs_administrator_gets_404_on_as_proposal(
    client: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    _, proposal = seed_proposal(
        owner_session, "demo-a", "evaluation_run", {"release_id": str(uuid7())}
    )
    before = proposal_row(owner_session, proposal)
    b = user_session(client, world.passwords, ADMIN_B)
    for action in ("accept", "dismiss"):
        for org in (B, A):
            r, unknown = decide(b, proposal, action, org), decide(b, str(uuid7()), action, org)
            assert r.status_code == unknown.status_code == 404, r.text
            if org == B:
                assert r.json()["detail"] == unknown.json()["detail"] == "proposal not found"
    assert proposal_row(owner_session, proposal) == before


def test_a_p1_only_developer_gets_404_on_p2s_proposal_and_a_viewer_403(
    client: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    p1 = project_of(owner_session, "demo-a")[1]
    add_project(admin, "other")
    _, p2_proposal = seed_proposal(owner_session, "demo-a", "evaluation_run",
                                   {"release_id": str(uuid7())}, project="other")  # fmt: skip
    developer = project_member(client, world, "p1-dev@demo-a.local", p1)
    for action in ("accept", "dismiss"):
        r, unknown = decide(developer, p2_proposal, action), decide(developer, str(uuid7()), action)
        assert r.status_code == unknown.status_code == 404
        assert r.json()["detail"] == unknown.json()["detail"] == "proposal not found"
    _, p1_proposal = seed_proposal(owner_session, "demo-a", "evaluation_run",
                                   {"release_id": str(uuid7())})  # fmt: skip
    viewer = user_session(client, world.passwords, VIEWER)
    for action in ("accept", "dismiss"):
        r = decide(viewer, p1_proposal, action)
        assert r.status_code == 403, r.text
    assert proposal_row(owner_session, p1_proposal)["status"] == "open"
    assert proposal_row(owner_session, p2_proposal)["status"] == "open"


def test_payload_and_result_ref_are_shown_only_with_the_read_keys(
    client: TestClient, world: World, admin: Hdr, owner_session: Session, owner_db: Database
) -> None:
    target, release = _target(admin), _release(owner_db, owner_session)
    request, proposal = seed_proposal(owner_session, "demo-a", "delivery_plan",
                                      _plan_payload(target, release))  # fmt: skip
    accepted = _ok(decide(admin, proposal, "accept"))
    url = f"{A}/assist/requests/{request}"
    viewer = user_session(client, world.passwords, VIEWER)
    [seen] = _ok(viewer.client.get(url, headers=viewer))["proposals"]
    assert (seen["payload"], seen["result_ref"], seen["status"]) == (None, None, "accepted")
    developer = project_member(client, world, "p1-reader@demo-a.local",
                               project_of(owner_session, "demo-a")[1])  # fmt: skip
    [shown] = _ok(developer.client.get(url, headers=developer))["proposals"]
    assert shown["payload"] == _plan_payload(target, release)
    assert shown["result_ref"] == accepted["result_ref"]
    [mine] = _ok(admin.client.get(url, headers=admin))["proposals"]
    assert mine == accepted
