"""SP7-3 (design 5.1, 5.3, 5.5 and section 11): assist requests.

A rule-routed request is answered 201 `routed` with its run; it is listed under its project and
read under the organization, never with its text. The checks of 5.1 run before anything is stored:
a viewer gets 403, an archived project 409, an unknown specialist 422 and a context in P2, in
client B or unknown 404 `context not found`; the text is 1..4000 characters after strip. With
model routing off an undecided request is refused at once (`route_ambiguous` naming its
candidates, `route_unclear`); with it on it is stored `queued` with one `assist.route` job and
answered 202. Every transition writes its audit action, and the request text appears in no audit
row and no log record.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from tests._support.assist_fixtures import (
    PICKS_COST,
    assist_model,
    audit_of,
    read,
    request_row,
    route_jobs,
    run_route_job,
    send,
    sent,
)
from tests._support.fullapp import World
from tests._support.p2util import post
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import P2, PROJECT, add_project, enable, installed, project_member, rows
from tests.assist.helpers import set_settings as settings_of

VIEWER_ROLE = "01900000-0000-7000-8000-000000000005"
SECRET = "zebra-quokka-7731"  # a marker of the request text, searched in audit rows and logs


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    h = user_session(client, world.passwords, ADMIN)
    enable(h)
    return h


@pytest.fixture
def model_id(admin: Hdr) -> str:
    return assist_model(admin.client, admin)


def _count(owner: Session) -> int:
    return int(rows(owner, "SELECT count(*) FROM shifde.assist_requests")[0][0])


def _solution(h: Hdr, org: str = A, project: str = PROJECT) -> str:
    made = post(h.client, f"{org}{project}/solutions", h,
                {"name": f"s-{uuid7().hex[-6:]}", "target_kind": "databricks.app/v1"})  # fmt: skip
    return str(made["id"])


def test_a_rule_routed_request_is_routed_listed_and_read(
    admin: Hdr, model_id: str, owner_session: Session
) -> None:
    c = admin.client
    [cost] = installed(admin, model_id, keys=["cost"])
    out = sent(c, admin, {"text": "  how much did we spend last week  "})
    assert (out["status"], out["status_reason"]) == ("routed", None), out
    assert (out["specialist_key"], out["route_method"], out["rule_id"]) == ("cost", "rule", "cost")
    assert out["router_version"] == "assist.router/1" and out["candidates"] == ["cost"]
    assert out["missing_permissions"] is None and out["proposals"] == []
    assert out["context"] is None and out["requested_specialist"] is None
    assert out["run"]["id"] == out["run_id"] and out["run"]["status"] != "failed"
    assert "text" not in out and "input" not in out["run"]
    assert request_row(owner_session, out["id"])["text"] == "how much did we spend last week"
    run = c.get(f"{A}/runs/{out['run_id']}", headers=admin).json()
    assert (run["agent_id"], run["agent_version_id"]) == (
        cost["agent_id"],
        cost["agent_version_id"],
    )
    # read under the organization, listed under the project
    assert read(c, admin, out["id"]) == {
        **out,
        "updated_at": read(c, admin, out["id"])["updated_at"],
    }
    listed = c.get(f"{A}{PROJECT}/assist/requests", headers=admin)
    assert listed.status_code == 200, listed.text
    assert [i["id"] for i in listed.json()["items"]] == [out["id"]]
    assert "text" not in listed.json()["items"][0]
    routed = c.get(f"{A}{PROJECT}/assist/requests", headers=admin, params={"status": "refused"})
    assert routed.json()["items"] == []
    actions = [a for a, _ in audit_of(owner_session, out["id"])]
    assert actions == ["assist.request.created", "assist.request.handing_over",
                       "assist.request.routed"]  # fmt: skip
    # the handing_over transaction queued the recovery job; it finds the request routed
    [(_, status, payload)] = route_jobs(owner_session, out["id"])
    assert status == "queued" and payload["request_id"] == out["id"]
    assert run_route_job(c, owner_session, out["id"])
    assert read(c, admin, out["id"])["status"] == "routed"


def test_the_list_pages_by_id(admin: Hdr) -> None:
    c = admin.client
    made = [sent(c, admin, {"text": f"hello {i}"})["id"] for i in range(3)]
    first = c.get(f"{A}{PROJECT}/assist/requests", headers=admin, params={"limit": 2}).json()
    assert [i["id"] for i in first["items"]] == made[::-1][:2] and first["next_cursor"]
    rest = c.get(f"{A}{PROJECT}/assist/requests", headers=admin,
                 params={"limit": 2, "cursor": first["next_cursor"]}).json()  # fmt: skip
    assert [i["id"] for i in rest["items"]] == [made[0]] and rest["next_cursor"] is None


def test_a_viewer_gets_403_and_nothing_is_stored(
    client: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    p1 = admin.client.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    viewer = project_member(client, world, "p1-viewer@demo-a.local", p1, VIEWER_ROLE)
    r = send(viewer.client, viewer, {"text": "how much did we spend"})
    assert r.status_code == 403, r.text
    assert _count(owner_session) == 0
    # a viewer reads the project's requests
    assert viewer.client.get(f"{A}{PROJECT}/assist/requests", headers=viewer).status_code == 200


def test_an_archived_project_takes_no_request(admin: Hdr, owner_session: Session) -> None:
    c = admin.client
    add_project(admin, "other")
    project = c.get(f"{A}{P2}", headers=admin).json()
    r = c.patch(f"{A}{P2}", headers=admin,
                json={"status": "archived", "row_version": project["row_version"]})  # fmt: skip
    assert r.status_code == 200, r.text
    refused = send(c, admin, {"text": "how much did we spend"}, project=P2)
    assert refused.status_code == 409 and refused.json()["code"] == "scope_archived", refused.text
    assert _count(owner_session) == 0


def test_an_unknown_specialist_is_422(admin: Hdr, owner_session: Session) -> None:
    r = send(admin.client, admin, {"text": "hello", "specialist": "astrologer"})
    assert r.status_code == 422 and r.json()["code"] == "specialist_unknown", r.text
    assert _count(owner_session) == 0


def test_a_context_outside_the_project_is_404(
    client: TestClient, world: World, admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    add_project(admin, "other")
    in_p2 = _solution(admin, project=P2)
    b = user_session(client, world.passwords, ADMIN_B)
    in_b = _solution(b, org=B)
    unknown = str(uuid7())
    for kind, oid in (("solution", in_p2), ("solution", in_b), ("solution", unknown),
                      ("release", unknown), ("target", unknown), ("plan", unknown),
                      ("deployment", unknown), ("evaluation_run", unknown)):  # fmt: skip
        r = send(c, admin, {"text": "what is this", "context": {"kind": kind, "id": oid}})
        assert r.status_code == 404, (kind, r.text)
        assert r.json()["detail"] == "context not found"
    assert _count(owner_session) == 0
    bad = send(c, admin, {"text": "hi", "context": {"kind": "connection", "id": unknown}})
    assert bad.status_code == 422, bad.text
    # a solution of the project is a context; only {kind, id} is stored
    mine = _solution(admin)
    out = sent(c, admin, {"text": "what is this", "context": {"kind": "solution", "id": mine}})
    assert out["context"] == {"kind": "solution", "id": mine}
    assert request_row(owner_session, out["id"])["context"] == {"kind": "solution", "id": mine}


def test_the_text_is_1_to_4000_characters_after_strip(admin: Hdr, owner_session: Session) -> None:
    c = admin.client
    for text in ("", "   \n ", "x" * 4001):
        r = send(c, admin, {"text": text})
        assert r.status_code == 422, r.text
    assert send(c, admin, {"text": "hi", "extra": 1}).status_code == 422
    assert _count(owner_session) == 0
    out = sent(c, admin, {"text": "  " + "x" * 4000 + "  "})
    assert len(request_row(owner_session, out["id"])["text"]) == 4000


def test_undecided_requests_are_refused_with_model_routing_off(
    admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    ambiguous = sent(c, admin, {"text": "evaluate the cost of this agent"})
    assert (ambiguous["status"], ambiguous["status_reason"]) == ("refused", "route_ambiguous")
    assert ambiguous["candidates"] == ["cost", "evaluation"] and ambiguous["specialist_key"] is None
    unclear = sent(c, admin, {"text": "tell me a joke"})
    assert (unclear["status"], unclear["status_reason"]) == ("refused", "route_unclear")
    assert unclear["candidates"] == []
    assert route_jobs(owner_session, ambiguous["id"]) == []
    events = audit_of(owner_session, ambiguous["id"])
    assert [a for a, _ in events] == ["assist.request.created", "assist.request.refused"]
    assert events[1][1]["candidates"] == ["cost", "evaluation"]
    assert events[1][1]["status_reason"] == "route_ambiguous"


def test_an_undecided_request_is_queued_with_model_routing_on(
    admin: Hdr, model_id: str, owner_session: Session
) -> None:
    settings_of(owner_session, "demo-a", model_routing_enabled=True, router_model_id=model_id)
    r = send(admin.client, admin, {"text": "evaluate the cost of this agent"})
    assert r.status_code == 202, r.text
    out = r.json()
    assert (out["status"], out["run"], out["specialist_key"]) == ("queued", None, None)
    [(job_id, status, payload)] = route_jobs(owner_session, out["id"])
    assert status == "queued" and str(job_id) == str(
        request_row(owner_session, out["id"])["job_id"]
    )
    assert set(payload) == {"request_id", "principal_id", "principal_type", "api_key_id"}
    assert [a for a, _ in audit_of(owner_session, out["id"])] == ["assist.request.created"]


def test_the_text_is_in_no_audit_row_and_no_log_record(
    admin: Hdr, model_id: str, owner_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    c = admin.client
    installed(admin, model_id, keys=["cost"])
    settings_of(owner_session, "demo-a", model_routing_enabled=True, router_model_id=model_id)
    caplog.set_level(logging.DEBUG)
    made = [
        sent(c, admin, {"text": f"how much did we spend {SECRET}"}),  # rule, routed
        sent(c, admin, {"text": f"tell me a joke {SECRET} {PICKS_COST}"}, 202),  # the model
        sent(c, admin, {"text": f"evaluate the cost {SECRET}"}, 202),
    ]
    assert run_route_job(c, owner_session, made[1]["id"])
    assert read(c, admin, made[1]["id"])["status"] == "routed"
    for m in made:
        for action, details in audit_of(owner_session, m["id"]):
            assert SECRET not in str(details), action
    assert rows(owner_session, "SELECT count(*) FROM shifde.audit_events "
                "WHERE details::text LIKE :s", s=f"%{SECRET}%") == [(0,)]  # fmt: skip
    for record in caplog.records:
        assert SECRET not in record.getMessage() and SECRET not in str(record.args)
        assert SECRET not in str(getattr(record, "__dict__", {}))


def test_each_transition_writes_its_audit_action(
    admin: Hdr, model_id: str, owner_session: Session
) -> None:
    c = admin.client
    installed(admin, model_id, keys=["cost"])
    settings_of(owner_session, "demo-a", model_routing_enabled=True, router_model_id=model_id)
    out = sent(c, admin, {"text": f"tell me a joke {PICKS_COST}"}, 202)
    assert run_route_job(c, owner_session, out["id"])
    events = audit_of(owner_session, out["id"])
    assert [a for a, _ in events] == [
        "assist.request.created",
        "assist.request.classifying",
        "assist.request.handing_over",
        "assist.request.routed",
    ]
    for _, details in events:
        assert {"specialist_key", "route_method", "rule_id", "router_version",
                "status_reason"} <= set(details)  # fmt: skip
    assert events[-1][1]["route_method"] == "model" and events[-1][1]["specialist_key"] == "cost"
    failed = sent(c, admin, {"text": "tell me a joke"}, 202)
    settings_of(owner_session, "demo-a", router_model_id=None, model_routing_enabled=False)
    assert run_route_job(c, owner_session, failed["id"])
    assert [a for a, _ in audit_of(owner_session, failed["id"])] == [
        "assist.request.created",
        "assist.request.failed",
    ]
