"""Deployments (design section 4, section 9 items 5-11 and 18; B-2681..B-2687): approval creates
the deployment and its one-attempt apply job, the gateway hooks drive it from `queued` to
`verified` or `failed`, the settle step expires, fails and cancels, native-resource ownership
refuses adoption, and the cancel, reconcile and mark-failed routes with their permissions, audit
and the status guard.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.core.db import Database
from shifde.core.errors import Conflict
from shifde.core.ids import uuid7
from shifde.delivery import gateway
from tests.connections.routes import as_user, system_role_user
from tests.delivery.routes import (
    BOTH_CHECKS,
    Flow,
    binding,
    checkpoint,
    deployment_jobs,
    deployment_row,
    deployments,
    expire,
    job_result,
    lease,
    lease_kind,
    make_due,
    owner_sql,
    plan_row,
    plan_value,
    plans,
    receipt_value,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People


def _code(r: Any, status: int, code: str) -> None:  # Any: an httpx response
    assert r.status_code == status and r.json()["code"] == code, r.text


def _counts(owner_db: Database, tenant: UUID) -> tuple[int, int, int]:
    """(approvals, deployments, apply jobs) of `tenant`, as the schema owner."""
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_approvals WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_deployments WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t "
                "AND kind = 'apply')"
            ),
            {"t": tenant},
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


def _observing(flow: Flow, owner_db: Database) -> tuple[dict[str, Any], str]:
    """A deployment that applied and is `observing` (its first observe job due now)."""
    plan, deployment = flow.deployment()
    flow.apply(plan)
    assert deployment_row(owner_db, deployment)["status"] == "observing"
    make_due(owner_db, deployment)
    return plan, deployment


def _poll(flow: Flow, kind: str, **result: Any) -> None:
    leased = lease_kind(flow.client, flow.token, kind)
    r = job_result(flow.client, flow.token, leased, **result)
    assert r.status_code == 200, r.text


def _unknown(flow: Flow, owner_db: Database) -> str:
    """A deployment whose executor died after an intent: `unknown_outcome` on read."""
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    expire(owner_db, leased["job"]["id"])
    assert flow.get(deployment)["status"] == "unknown_outcome"
    return deployment


# ------------------------------------------------------------------ approval
def test_approve_creates_one_queued_deployment_and_one_apply_job(
    flow: Flow, world: World, owner_db: Database
) -> None:
    target = flow.target()
    plan = flow.sealed(target["id"])
    approved = flow.approve(plan)
    deployment = approved["deployment_id"]
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "queued" and row["plan_id"] == UUID(plan["id"])
    assert row["target_id"] == UUID(target["id"]) and row["application_key"] == "sales-app"
    (job,) = deployment_jobs(owner_db, deployment)
    assert job["kind"] == "apply" and job["status"] == "queued" and job["max_attempts"] == 1
    assert job["plan_id"] == UUID(plan["id"]) and job["target_id"] == UUID(target["id"])
    assert job["deadline"] == plan_row(owner_db, plan["id"])["expires_at"]
    assert _counts(owner_db, world.a.tenant) == (1, 1, 1)
    with owner_db.session() as o:
        details = o.execute(
            text(
                "SELECT details FROM shifde.audit_events WHERE tenant_id = :t "
                "AND action = 'delivery.plan.approved'"
            ),
            {"t": world.a.tenant},
        ).scalar_one()
    assert details["deployment_id"] == deployment and details["apply_job_id"] == str(job["id"])
    # reject creates nothing
    other = flow.sealed(target["id"], "other-app")
    h = as_user(flow.client, flow.approver)
    r = flow.client.post(f"{plans(flow.slug)}/{other['id']}/reject", json={}, headers=h)
    assert r.status_code == 200 and r.json()["deployment_id"] is None, r.text
    assert _counts(owner_db, world.a.tenant) == (2, 1, 1)


def test_a_failed_enqueue_during_approve_leaves_nothing(
    flow: Flow, world: World, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = flow.sealed(flow.target()["id"])

    def refuse(*_: object, **__: object) -> UUID:
        raise Conflict("queue refused")

    monkeypatch.setattr(gateway, "enqueue_job", refuse)
    h = as_user(flow.client, flow.approver)
    r = flow.client.post(
        f"{plans(flow.slug)}/{plan['id']}/approve",
        json={"plan_digest": plan["digest"]},
        headers=h,
    )
    _code(r, 409, "conflict")
    assert _counts(owner_db, world.a.tenant) == (0, 0, 0)
    assert plan_row(owner_db, plan["id"])["status"] == "sealed"


# ------------------------------------------------------------------ the happy path
def test_the_full_path_to_verified(flow: Flow, owner_db: Database) -> None:
    plan, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert leased["job"]["deployment_id"] == deployment
    assert deployment_row(owner_db, deployment)["status"] == "leased"
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    assert deployment_row(owner_db, deployment)["status"] == "applying"
    done = checkpoint(flow.client, flow.token, leased, 2, "complete", {"ok": True})
    assert done.status_code == 200, done.text
    value = receipt_value(flow.tenant, plan["target_id"], plan["artifact_digest"])
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, deployment)
    assert (
        row["status"] == "observing"
        and row["receipt"]["artifact_digest"] == value["artifact_digest"]
    )
    (observe,) = deployment_jobs(owner_db, deployment, "observe")
    due = observe["run_after"] - observe["db_now"]
    assert timedelta(seconds=10) < due <= timedelta(seconds=15)
    assert observe["deadline"] == row["observe_deadline"] and observe["max_attempts"] == 4
    assert abs(row["observe_deadline"] - observe["db_now"] - timedelta(minutes=20)) < timedelta(
        seconds=5
    )
    assert lease(flow.client, flow.token, kinds=["observe"]).status_code == 204  # not yet due
    make_due(owner_db, deployment)
    _poll(flow, "observe", status="succeeded", value={"state": "running"})
    jobs = deployment_jobs(owner_db, deployment, "observe")
    assert [j["status"] for j in jobs] == ["done", "queued"]
    assert deployment_row(owner_db, deployment)["status"] == "observing"
    make_due(owner_db, deployment)
    _poll(flow, "observe", status="succeeded", value={"state": "succeeded"})
    assert deployment_row(owner_db, deployment)["status"] == "verifying"
    (verify,) = deployment_jobs(owner_db, deployment, "verify")
    assert verify["deadline"] == row["observe_deadline"]
    make_due(owner_db, deployment)
    _poll(flow, "verify", status="succeeded", value={"checks": BOTH_CHECKS})
    out = flow.get(deployment)
    assert out["status"] == "verified" and out["status_reason"] is None
    assert out["verification"]["checks"][0]["name"] == "native-runtime"
    assert [(s["step_key"], s["state"]) for s in out["journal"]] == [("bundle_deploy", "complete")]


@pytest.mark.parametrize("warehouse", ["failed", "skipped"])
def test_verified_needs_both_checks_passed(flow: Flow, owner_db: Database, warehouse: str) -> None:
    _, deployment = _observing(flow, owner_db)
    _poll(flow, "observe", status="succeeded", value={"state": "succeeded"})
    make_due(owner_db, deployment)
    checks = [
        {"name": "native-runtime", "status": "passed"},
        {"name": "runtime-warehouse", "status": warehouse, "required": False},
    ]
    _poll(flow, "verify", status="succeeded", value={"checks": checks})
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "failed" and row["status_reason"] == "verification failed"
    assert row["verification"]["checks"][1]["status"] == warehouse


def test_a_platform_failure_or_a_failed_observe_job_fails_the_deployment(
    flow: Flow, owner_db: Database
) -> None:
    _, first = _observing(flow, owner_db)
    _poll(flow, "observe", status="succeeded", value={"state": "failed"})
    row = deployment_row(owner_db, first)
    assert (row["status"], row["status_reason"]) == ("failed", "the platform reported a failure")
    _, second = _observing(flow, owner_db)
    _poll(flow, "observe", status="failed", error_code="execution_failed")
    row = deployment_row(owner_db, second)
    assert (row["status"], row["status_reason"]) == ("failed", "observe failed: execution_failed")


def test_observing_past_the_deadline_fails(flow: Flow, owner_db: Database) -> None:
    # a `running` result after the deadline
    _, first = _observing(flow, owner_db)
    leased = lease_kind(flow.client, flow.token, "observe")
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_deployments SET observe_deadline = now() - interval '1 second' "
        "WHERE id = :d",
        d=first,
    )
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"state": "running"})
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, first)
    assert (row["status"], row["status_reason"]) == ("failed", "did not become healthy")
    assert [j["status"] for j in deployment_jobs(owner_db, first, "observe")] == ["done"]
    # no job holds a lease when the deadline passes: the settle step fails it on read
    _, second = _observing(flow, owner_db)
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_deployments SET observe_deadline = now() - interval '1 second' "
        "WHERE id = :d",
        d=second,
    )
    out = flow.get(second)
    assert (out["status"], out["status_reason"]) == ("failed", "did not become healthy")
    assert [j["status"] for j in deployment_jobs(owner_db, second, "observe")] == ["cancelled"]


def test_an_observe_job_lost_four_times_fails_the_deployment(
    flow: Flow, owner_db: Database
) -> None:
    _, deployment = _observing(flow, owner_db)
    for attempt in range(1, 5):
        leased = lease_kind(flow.client, flow.token, "observe")
        assert leased["job"]["attempts"] == attempt
        expire(owner_db, leased["job"]["id"])
    out = flow.get(deployment)
    assert (out["status"], out["status_reason"]) == ("failed", "executor lost")
    (job,) = deployment_jobs(owner_db, deployment, "observe")
    assert job["status"] == "lost" and job["attempts"] == 4


# ------------------------------------------------------------------ expiry, cancel, no retry
def test_an_apply_past_the_plan_expiry_is_not_leased_and_expires(
    flow: Flow, owner_db: Database
) -> None:
    _, deployment = flow.deployment()
    (job,) = deployment_jobs(owner_db, deployment)
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_jobs SET deadline = now() - interval '1 second' WHERE id = :j",
        j=job["id"],
    )
    assert lease(flow.client, flow.token, kinds=["apply"]).status_code == 204
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "expired"
    assert row["status_reason"] == "the plan expired before the apply was leased"
    assert deployment_jobs(owner_db, deployment)[0]["status"] == "cancelled"


def test_cancel_only_before_the_apply_is_leased(
    flow: Flow, world: World, owner_db: Database
) -> None:
    _, queued = flow.deployment()
    r = flow.act(queued, "cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled", r.text
    assert deployment_jobs(owner_db, queued)[0]["status"] == "cancelled"
    _code(flow.act(queued, "cancel"), 409, "deployment_not_cancellable")
    _, leased = flow.deployment()
    lease_kind(flow.client, flow.token, "apply")
    _code(flow.act(leased, "cancel"), 409, "deployment_not_cancellable")
    assert deployment_row(owner_db, leased)["status"] == "leased"
    with owner_db.session() as o:
        actions = o.execute(
            text(
                "SELECT action, target_id::text FROM shifde.audit_events WHERE tenant_id = :t "
                "AND target_type = 'delivery_deployment'"
            ),
            {"t": world.a.tenant},
        ).all()
    assert [tuple(a) for a in actions] == [("delivery.deployment.cancelled", queued)]


def test_an_apply_job_is_never_requeued(flow: Flow, owner_db: Database) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    expire(owner_db, leased["job"]["id"])
    for _ in range(2):
        assert lease(flow.client, flow.token).status_code == 204
    (job,) = deployment_jobs(owner_db, deployment)
    assert job["status"] == "lost" and job["attempts"] == 1
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "no platform write was attempted")


# ------------------------------------------------------------------ ownership and receipts
def test_native_resources_are_owned_and_never_adopted(flow: Flow, owner_db: Database) -> None:
    plan, deployment = flow.deployment()
    flow.apply(plan)
    with owner_db.session() as o:
        owned = o.execute(
            text(
                "SELECT target_id::text, application_key, resource_type, native_id, "
                "deployment_id::text FROM shifde.delivery_native_resources"
            )
        ).all()
    assert [tuple(r) for r in owned] == [
        (plan["target_id"], "sales-app", "databricks_app", "sales-app", deployment)
    ]

    def snapshot(native_id: str) -> dict[str, Any]:
        value = plan_value()
        value["snapshot"]["resources"] = [binding(plan["target_id"], native_id)]
        return value

    refused = "refusing to adopt an unowned native app"
    for key, native_id in (("other-app", "sales-app"), ("other-app", "unknown-app")):
        adopted = flow.sealed(plan["target_id"], key, snapshot(native_id))
        assert (adopted["status"], adopted["status_reason"]) == ("plan_failed", refused)
    # the owning application may plan again with its own app in the snapshot
    again = flow.sealed(plan["target_id"], "sales-app", snapshot("sales-app"))
    assert again["status"] == "sealed"


def test_a_receipt_naming_another_applications_resource_fails(
    flow: Flow, owner_db: Database
) -> None:
    plan, _ = flow.deployment()
    flow.apply(plan)
    other, deployment = flow.deployment(plan["target_id"], "other-app")
    flow.apply(other)  # its receipt names `sales-app`, owned by `sales-app`
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == (
        "failed",
        "resource owned by another application",
    )
    with owner_db.session() as o:
        owners = o.execute(
            text("SELECT application_key FROM shifde.delivery_native_resources")
        ).all()
    assert [r[0] for r in owners] == ["sales-app"]


def test_a_receipt_for_another_target_or_artifact_is_refused(
    flow: Flow, owner_db: Database
) -> None:
    plan, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    good = receipt_value(flow.tenant, plan["target_id"], plan["artifact_digest"])
    other = str(uuid7())
    for name, value in (
        ("target", {**good, "target_id": other}),
        ("artifact", {**good, "artifact_digest": "0" * 64}),
        ("organization", {**good, "tenant_id": other}),
        ("resource", {**good, "resources": [binding(other)]}),
    ):
        r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
        assert r.status_code == 422, (name, r.text)
    assert deployment_row(owner_db, deployment)["status"] == "leased"
    assert deployment_jobs(owner_db, deployment)[0]["status"] == "leased"
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=good)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ reconcile and mark-failed
def test_reconcile_stores_ambiguous_evidence_and_keeps_the_unknown_outcome(
    flow: Flow, world: World, owner_db: Database
) -> None:
    deployment = _unknown(flow, owner_db)
    r = flow.act(deployment, "reconcile")
    assert r.status_code == 200 and r.json()["status"] == "unknown_outcome", r.text
    _code(flow.act(deployment, "reconcile"), 409, "reconcile_pending")
    leased = lease_kind(flow.client, flow.token, "reconcile")
    assert leased["job"]["deployment_id"] == deployment and leased["job"]["max_attempts"] == 4
    value = {"state": "ambiguous", "evidence": "the app exists; its revision is unknown"}
    assert job_result(flow.client, flow.token, leased, status="succeeded", value=value).is_success
    out = flow.get(deployment)
    assert out["status"] == "unknown_outcome"
    assert out["reconcile_evidence"] == {
        **value,
        "resources": [],
        "job_id": leased["job"]["id"],
    }
    # a second reconcile may run once the first is done; a failed job is evidence too
    assert flow.act(deployment, "reconcile").status_code == 200
    leased = lease_kind(flow.client, flow.token, "reconcile")
    r = job_result(flow.client, flow.token, leased, status="failed", error_code="execution_failed")
    assert r.status_code == 200, r.text
    assert flow.get(deployment)["reconcile_evidence"] == {
        "status": "failed",
        "error_code": "execution_failed",
        "job_id": leased["job"]["id"],
    }
    _, queued = flow.deployment(flow.target()["id"])
    _code(flow.act(queued, "reconcile"), 409, "deployment_not_unknown")
    with owner_db.session() as o:
        n = o.execute(
            text(
                "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t "
                "AND action = 'delivery.deployment.reconcile_requested'"
            ),
            {"t": world.a.tenant},
        ).scalar_one()
    assert n == 2


def test_mark_failed_needs_a_note_and_the_resolve_permission(
    flow: Flow, world: World, owner_db: Database
) -> None:
    deployment = _unknown(flow, owner_db)
    for body in (None, {}, {"note": ""}, {"note": "   "}, {"note": "x" * 2001}):
        r = flow.act(deployment, "mark-failed", body)
        assert r.status_code == 422, (body, r.text)
    developer = system_role_user(owner_db, world.a.tenant, "developer")
    assert flow.read(deployment, developer).status_code == 200  # read, not resolve
    for action, body in (
        ("mark-failed", {"note": "checked"}),
        ("reconcile", None),
        ("cancel", None),
    ):
        r = flow.act(deployment, action, body, email=developer)
        assert r.status_code == 403, (action, r.text)
    assert deployment_row(owner_db, deployment)["status"] == "unknown_outcome"
    r = flow.act(deployment, "mark-failed", {"note": "  the app was never created  "})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "resolved_failed"
    assert out["resolved_note"] == "the app was never created"
    assert out["resolved_by"] is not None and out["resolved_at"] is not None
    _code(flow.act(deployment, "mark-failed", {"note": "again"}), 409, "deployment_not_unknown")
    with owner_db.session() as o:
        audit = o.execute(
            text(
                "SELECT actor_id::text, details FROM shifde.audit_events WHERE tenant_id = :t "
                "AND action = 'delivery.deployment.marked_failed'"
            ),
            {"t": world.a.tenant},
        ).one()
    assert audit.actor_id == out["resolved_by"]
    assert audit.details["note"] == "the app was never created"
    assert audit.details["deployment_id"] == deployment


def test_the_status_guard_refuses_leaving_an_unknown_outcome_except_to_resolved_failed(
    flow: Flow, world: World, owner_db: Database, app_db: Database
) -> None:
    deployment = _unknown(flow, owner_db)
    for status in ("verified", "cancelled", "failed", "queued"):
        with app_session(app_db, world.a.tenant) as s, pytest.raises(DBAPIError) as exc:
            s.execute(
                text("UPDATE shifde.delivery_deployments SET status = :s WHERE id = :d"),
                {"s": status, "d": deployment},
            )
        assert getattr(exc.value.orig, "sqlstate", None) == "42501", status
    # resolved_failed needs who, why and when (the table CHECK)
    with app_session(app_db, world.a.tenant) as s, pytest.raises(DBAPIError) as exc:
        s.execute(
            text("UPDATE shifde.delivery_deployments SET status = 'resolved_failed' WHERE id = :d"),
            {"d": deployment},
        )
    assert "ck_delivery_deployments__resolved" in str(exc.value)
    assert deployment_row(owner_db, deployment)["status"] == "unknown_outcome"


# ------------------------------------------------------------------ list and permissions
def test_list_filters_and_scope(flow: Flow, people: People, owner_db: Database) -> None:
    target = flow.target()
    _, first = flow.deployment(target["id"])
    _, second = flow.deployment()
    flow.act(first, "cancel")
    h = as_user(flow.client, flow.requester)
    listed = flow.client.get(f"{deployments(flow.slug)}?limit=200", headers=h).json()
    assert [d["id"] for d in listed["items"]] == [first, second]
    assert "journal" not in listed["items"][0]
    by_status = flow.client.get(deployments(flow.slug), params={"status": "queued"}, headers=h)
    assert [d["id"] for d in by_status.json()["items"]] == [second]
    by_target = flow.client.get(deployments(flow.slug), params={"target": target["id"]}, headers=h)
    assert [d["id"] for d in by_target.json()["items"]] == [first]
    viewer = as_user(flow.client, people.viewer_a)
    assert flow.client.get(deployments(flow.slug), headers=viewer).status_code == 403
    unknown = flow.client.get(f"{deployments(flow.slug)}/{uuid7()}", headers=viewer)
    hidden = flow.client.get(f"{deployments(flow.slug)}/{first}", headers=viewer)
    assert unknown.status_code == hidden.status_code == 404
    assert hidden.json()["detail"] == unknown.json()["detail"] == "deployment not found"


def test_a_refused_read_runs_no_housekeeping(
    flow: Flow, people: People, owner_db: Database
) -> None:
    """Permission before housekeeping (lead note 5, #69 point 3): a caller who cannot read the
    deployment does not settle its expired apply lease."""
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    expire(owner_db, leased["job"]["id"])
    r = flow.read(deployment, people.viewer_a)
    _code(r, 404, "not_found")
    assert deployment_row(owner_db, deployment)["status"] == "leased"
    assert deployment_jobs(owner_db, deployment)[0]["status"] == "leased"
    h = as_user(flow.client, people.viewer_a)
    assert flow.client.get(deployments(flow.slug), headers=h).status_code == 403
    assert deployment_row(owner_db, deployment)["status"] == "leased"
    # a reader settles it
    assert flow.get(deployment)["status"] == "failed"
