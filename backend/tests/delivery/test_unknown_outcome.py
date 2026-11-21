"""The C1 unknown-outcome rule (design section 4 and its four crash tests, section 9 items 8, 9 and
10; B-2683, B-2684): an executor that dies after any journal step leaves the deployment
`unknown_outcome`, one that dies before any leaves it `failed`, a superseded lease writes nothing,
and the outcome is seen on read alone (the sweep and settle step run on every deployment read).
A target with an unresolved unknown outcome takes no new plan and leases none of its queued
applies until a person marks the outcome failed. None of these outcomes is ever `cancelled`,
`verified` or re-queued.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies, rows

from fastapi.testclient import TestClient

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.connections.routes import as_user
from tests.delivery.routes import (
    STEP_DIGEST,
    Flow,
    bearer,
    checkpoint,
    deployment_jobs,
    deployment_row,
    expire,
    job_result,
    job_url,
    journal_rows,
    lease,
    lease_kind,
    plans,
)

NO_WRITE = "no platform write was attempted"


def _code(r: Any, status: int, code: str) -> None:  # Any: an httpx response
    assert r.status_code == status and r.json()["code"] == code, r.text


def _never_retried(owner_db: Database, deployment: str) -> None:
    """The apply job ran once and is not queued again; the outcome is not healthy or cancelled."""
    (job,) = deployment_jobs(owner_db, deployment)
    assert job["kind"] == "apply" and job["attempts"] == 1
    assert job["status"] in ("lost", "done")
    assert deployment_row(owner_db, deployment)["status"] not in ("cancelled", "verified")


def _died(flow: Flow, owner_db: Database, leased: dict[str, Any]) -> dict[str, Any]:
    """The executor stops heartbeating: its lease expires. The outcome is read through the
    deployment route alone, with no further lease call."""
    expire(owner_db, leased["job"]["id"])
    return flow.get(leased["job"]["deployment_id"])


# ------------------------------------------------------------------ the four crash tests
def test_crash_1_before_any_intent_is_failed(flow: Flow, owner_db: Database) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    out = _died(flow, owner_db, leased)
    assert (out["status"], out["status_reason"]) == ("failed", NO_WRITE)
    assert out["journal"] == []
    _never_retried(owner_db, deployment)


def test_crash_2_between_intent_and_complete_is_unknown(flow: Flow, owner_db: Database) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    out = _died(flow, owner_db, leased)
    assert out["status"] == "unknown_outcome"
    assert [(s["step_key"], s["state"]) for s in out["journal"]] == [("bundle_deploy", "intent")]
    _never_retried(owner_db, deployment)


def test_crash_3_after_complete_before_the_result_is_unknown(
    flow: Flow, owner_db: Database
) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    done = checkpoint(flow.client, flow.token, leased, 2, "complete", {"ok": True})
    assert done.status_code == 200, done.text
    out = _died(flow, owner_db, leased)
    assert out["status"] == "unknown_outcome"
    assert [(s["state"], s["receipt"]) for s in out["journal"]] == [("complete", {"ok": True})]
    _never_retried(owner_db, deployment)


def test_crash_4_a_stale_fence_cannot_checkpoint(
    flow: Flow, client: TestClient, owner_db: Database
) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    job = leased["job"]["id"]
    intent = {
        "sequence": 1,
        "step_key": "bundle_deploy",
        "state": "intent",
        "input_digest": STEP_DIGEST,
    }
    for name, stale in (
        ("stale fence", {"lease_id": leased["lease_id"], "fence": leased["fence"] + 1}),
        ("old lease", {"lease_id": str(uuid7()), "fence": leased["fence"]}),
    ):
        r = client.post(
            job_url(job, "checkpoint"), json={**intent, **stale}, headers=bearer(flow.token)
        )
        _code(r, 409, "lease_lost")
        assert journal_rows(owner_db, flow.tenant) == [], name
    # the lease ends: the superseded executor's intent is refused too and writes nothing
    expire(owner_db, job)
    _code(checkpoint(client, flow.token, leased, 1, "intent"), 409, "lease_lost")
    assert journal_rows(owner_db, flow.tenant) == []
    assert deployment_row(owner_db, deployment)["status"] == "leased"


# ------------------------------------------------------------------ executor-reported failures
def test_a_failed_apply_result_after_an_intent_is_unknown_and_without_one_failed(
    flow: Flow, owner_db: Database
) -> None:
    _, written = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    r = job_result(flow.client, flow.token, leased, status="failed", error_code="execution_failed")
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, written)["status"] == "unknown_outcome"
    _never_retried(owner_db, written)
    mark = flow.act(written, "mark-failed", {"note": "checked by hand"})
    assert mark.status_code == 200, mark.text
    _, untouched = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    r = job_result(
        flow.client, flow.token, leased, status="failed", error_code="credential_unavailable"
    )
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, untouched)
    assert (row["status"], row["status_reason"]) == ("failed", NO_WRITE)
    _never_retried(owner_db, untouched)


def test_a_succeeded_apply_with_an_unfinished_step_is_unknown(
    flow: Flow, owner_db: Database
) -> None:
    plan, deployment = flow.deployment()
    flow.apply(plan, complete=False)
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "unknown_outcome" and row["receipt"] is None
    assert deployment_jobs(owner_db, deployment, "observe") == []


# ------------------------------------------------------------------ an unresolved target
def test_an_unresolved_target_takes_no_plan_and_holds_its_applies_until_marked_failed(
    flow: Flow, owner_db: Database
) -> None:
    target = flow.target()["id"]
    _, first = flow.deployment(target, "sales-app")
    _, second = flow.deployment(target, "orders-app")
    leased = lease_kind(flow.client, flow.token, "apply")
    assert leased["job"]["deployment_id"] == first
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    out = _died(flow, owner_db, leased)
    assert out["status"] == "unknown_outcome"
    # the queued apply on the same target is held, and says why
    assert lease(flow.client, flow.token, kinds=["apply"]).status_code == 204
    held = deployment_row(owner_db, second)
    assert held["status"] == "queued"
    assert held["status_reason"] == "blocked by unresolved unknown outcome"
    assert deployment_jobs(owner_db, second)[0]["status"] == "queued"
    # no new plan on the target
    h = as_user(flow.client, flow.requester)
    body = {
        "target_id": target,
        "release_id": str(flow.release(target)),
        "application_key": "more-app",
        "display_name": "More",
    }
    r = flow.client.post(plans(flow.slug), json=body, headers=h)
    _code(r, 409, "target_unresolved")
    # a person marks the outcome failed: plans are accepted and the held apply is leased
    assert flow.act(first, "mark-failed", {"note": "verified by hand"}).status_code == 200
    again = lease_kind(flow.client, flow.token, "apply")
    assert again["job"]["deployment_id"] == second
    row = deployment_row(owner_db, second)
    assert row["status"] == "leased" and row["status_reason"] is None
    h = as_user(flow.client, flow.requester)
    assert flow.client.post(plans(flow.slug), json=body, headers=h).status_code == 201
    assert deployment_row(owner_db, first)["status"] == "resolved_failed"
    _never_retried(owner_db, first)
