"""SP5-2 (design 5.1, 5.4, 5.6, section 7 and 9.2): app request routes.

A create on the fake Databricks workspace answers 202 with one queued `appgen.draft` job. Each
refusal of 5.1 queues nothing: another client's or another project's connection (the 404 an
unknown connection gets), a connection that is not Databricks, not read-only, not active or not
discovered, a table not in its latest discovery (`loc` names it) and an unknown model. The list
pages newest first and filters by status; an edit stores revision n+1 with its diagnostics (never
blocking), refuses a stale base and a request that is not drafted, and 422s a schema error;
discard and retry move only from their states (409 otherwise), and of two racing discards one
wins.
"""

from __future__ import annotations

import copy
import threading
from typing import Any  # Any: JSON bodies

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from tests._support.appgen_fixtures import SAMPLE_DRAFT
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, ADMIN_B, B, connection
from tests._support.p5util import Hdr, user_session
from tests.appgen.helpers import (
    INVALID,
    TABLES,
    add_project,
    body,
    create,
    created,
    databricks,
    draft_jobs,
    drafted,
    drafting_model,
    get,
    request_url,
    requests_url,
    run_job,
    wire,
)


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    wire(client)
    return user_session(client, world.passwords, ADMIN)


def _code(r: Any, status: int, code: str) -> dict[str, Any]:  # Any: an httpx response
    assert r.status_code == status, r.text
    out: dict[str, Any] = r.json()
    assert out["code"] == code, out
    return out


def _audit(owner: Session, request_id: str) -> list[str]:
    owner.rollback()
    rows = owner.execute(
        text("SELECT action FROM shifde.audit_events WHERE target_id = :r ORDER BY chain_seq"),
        {"r": request_id},
    ).all()
    owner.rollback()
    return [r[0] for r in rows]


def test_a_create_answers_202_with_one_queued_draft_job(admin: Hdr, owner_session: Session) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    out = created(c, admin, body(cid, model, tables=["MAIN.default.Orders"]))
    assert out["status"] == "queued" and out["status_reason"] is None
    assert out["tables"] == ["main.default.orders"] and out["revisions"] == []
    assert out["latest_revision_no"] is None and out["model_id"] == model
    jobs = draft_jobs(owner_session)
    assert len(jobs) == 1 and jobs[0][1] == "queued"
    payload = jobs[0][2]
    assert payload["request_id"] == out["id"] and payload["principal_type"] == "user"
    assert set(payload) == {"request_id", "principal_id", "principal_type", "api_key_id"}
    assert _audit(owner_session, out["id"]) == ["appgen.request.created"]


def test_each_create_refusal_queues_nothing(
    client: TestClient, admin: Hdr, world: World, owner_session: Session
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    unknown = _code(create(c, admin, body(str(uuid7()), model)), 404, "not_found")
    assert unknown["detail"] == "connection not found"
    # another client's connection: the answer an unknown id gets
    admin_b = user_session(client, world.passwords, ADMIN_B)
    b_conn = databricks(admin_b.client, admin_b, "b-lake", B)
    other = _code(create(c, admin, body(b_conn, model)), 404, "not_found")
    assert {k: v for k, v in other.items() if k != "request_id"} == {
        k: v for k, v in unknown.items() if k != "request_id"
    }
    # another project's connection
    add_project(c, admin, "other")
    p2_conn = databricks(c, admin, "p2-lake", scope={"workspace": "main", "project": "other"})
    _code(create(c, admin, body(p2_conn, model)), 404, "not_found")
    # not Databricks
    rest = connection(
        c,
        admin,
        {
            "name": "rest",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "config": {"base_url": "https://api.example.test"},
        },
    )
    _code(create(c, admin, body(rest["id"], model)), 422, "connection_not_databricks")
    writable = databricks(c, admin, "rw-lake", access_mode="read_write", discover=False)
    _code(create(c, admin, body(writable, model)), 422, "connection_not_read_only")
    draft = databricks(c, admin, "draft-lake", active=False, discover=False)
    _code(create(c, admin, body(draft, model)), 422, "connection_not_active")
    undiscovered = databricks(c, admin, "new-lake", discover=False)
    _code(create(c, admin, body(undiscovered, model)), 422, "connection_not_discovered")
    missing = _code(
        create(c, admin, body(cid, model, tables=[*TABLES, "main.default.nope"])),
        422,
        "table_not_discovered",
    )
    assert [e["loc"] for e in missing["errors"]] == [["body", "tables", "main.default.nope"]]
    unknown_model = _code(create(c, admin, body(cid, str(uuid7()))), 404, "not_found")
    assert unknown_model["detail"] == "model not found"
    assert draft_jobs(owner_session) == []
    assert owner_session.execute(text("SELECT count(*) FROM shifde.appgen_requests")).scalar() == 0


def test_the_list_pages_newest_first_and_filters_by_status(
    admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    ids = [created(c, admin, body(cid, model))["id"] for _ in range(3)]
    assert run_job(c, owner_session, ids[0])
    first = c.get(requests_url(), headers=admin, params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert [r["id"] for r in page["items"]] == [ids[2], ids[1]] and page["next_cursor"]
    rest = c.get(requests_url(), headers=admin, params={"limit": 2, "cursor": page["next_cursor"]})
    assert [r["id"] for r in rest.json()["items"]] == [ids[0]] and rest.json()[
        "next_cursor"
    ] is None
    assert rest.json()["items"][0]["latest_revision_no"] == 1
    queued = c.get(requests_url(), headers=admin, params={"status": "queued"}).json()
    assert [r["id"] for r in queued["items"]] == [ids[2], ids[1]]
    assert c.get(requests_url(), headers=admin, params={"status": "bogus"}).status_code == 422
    assert c.get(requests_url(), headers=admin, params={"limit": 201}).status_code == 422


def test_an_edit_adds_a_revision_with_its_diagnostics(admin: Hdr, owner_session: Session) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    out = drafted(c, admin, owner_session, body(cid, model))
    rid = out["id"]
    edited = copy.deepcopy(SAMPLE_DRAFT)
    edited["tools"][0]["sql"] = "select * from main.other.secret"  # an error, saved anyway
    r = c.post(request_url(rid, tail="/drafts"), headers=admin,
               json={"base_revision_no": 1, "draft": edited})  # fmt: skip
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["revision_no"] == 2 and rev["source"] == "edited" and rev["draft"] == edited
    assert ("tools.0.sql", "sql_table_not_allowed", "error") in [
        (d["path"], d["code"], d["severity"]) for d in rev["validation"]
    ]
    stale = c.post(request_url(rid, tail="/drafts"), headers=admin,
                   json={"base_revision_no": 1, "draft": SAMPLE_DRAFT})  # fmt: skip
    _code(stale, 409, "stale_revision")
    bad = c.post(request_url(rid, tail="/drafts"), headers=admin,
                 json={"base_revision_no": 2, "draft": {**SAMPLE_DRAFT, "extra": 1}})  # fmt: skip
    assert bad.status_code == 422, bad.text
    full = get(c, admin, rid)
    assert [v["revision_no"] for v in full["revisions"]] == [1, 2]
    assert full["latest_revision_no"] == 2
    assert _audit(owner_session, rid)[-1] == "appgen.draft.revised"
    # only while drafted
    discarded = c.post(request_url(rid, tail="/discard"), headers=admin)
    assert discarded.status_code == 200 and discarded.json()["status"] == "discarded"
    late = c.post(request_url(rid, tail="/drafts"), headers=admin,
                  json={"base_revision_no": 2, "draft": SAMPLE_DRAFT})  # fmt: skip
    _code(late, 409, "request_not_editable")


def test_discard_and_retry_move_only_from_their_states(
    client: TestClient, admin: Hdr, world: World, owner_session: Session
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    queued = created(c, admin, body(cid, model))["id"]
    _code(c.post(request_url(queued, tail="/discard"), headers=admin), 409,
          "request_not_discardable")  # fmt: skip
    _code(c.post(request_url(queued, tail="/retry"), headers=admin), 409, "request_not_failed")
    failed = created(c, admin, body(cid, model, brief=f"broken {INVALID}"))["id"]
    assert run_job(c, owner_session, failed)
    assert get(c, admin, failed)["status_reason"] == "invalid_model_output"
    retried = c.post(request_url(failed, tail="/retry"), headers=admin)
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "queued" and retried.json()["status_reason"] is None
    jobs = [j for j in draft_jobs(owner_session) if j[2]["request_id"] == failed]
    assert len(jobs) == 2 and jobs[1][1] == "queued"
    assert run_job(c, owner_session, failed)  # the new job: a new model call, failed again
    assert get(c, admin, failed)["status"] == "failed"
    assert _audit(owner_session, failed) == [
        "appgen.request.created",
        "appgen.request.drafting",
        "appgen.request.failed",
        "appgen.request.retried",
        "appgen.request.drafting",
        "appgen.request.failed",
    ]
    # two racing discards: exactly one wins
    answers: list[int] = []
    people = [user_session(client, world.passwords, ADMIN) for _ in range(2)]

    def discard(who: Hdr) -> None:
        answers.append(
            who.client.post(request_url(failed, tail="/discard"), headers=who).status_code
        )

    threads = [threading.Thread(target=discard, args=(who,)) for who in people]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(answers) == [200, 409], answers
    assert _audit(owner_session, failed).count("appgen.request.discarded") == 1
    _code(c.post(request_url(failed, tail="/retry"), headers=admin), 409, "request_not_failed")
