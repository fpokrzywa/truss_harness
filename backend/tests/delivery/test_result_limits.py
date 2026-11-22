"""Executor result limits and the `unknown_outcome` audit row (issue #72, B-2736): a result value
over a contract limit is refused with 422 and changes nothing, one exactly at it is accepted; every
move of a deployment to `unknown_outcome` (a failed apply after an intent, a succeeded apply with
an open intent, a lost apply in the settle step) writes exactly one audit row, and a move to
`failed` or a repeated settle step writes none.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies, rows

from sqlalchemy import text

from shifde.adapters.native.contracts import (
    EVIDENCE_MAX,
    HANDLES_MAX,
    ITEMS_MAX,
    NAME_MAX,
    NATIVE_ID_MAX,
)
from shifde.core.db import Database
from tests.delivery.routes import (
    BOTH_CHECKS,
    Flow,
    binding,
    checkpoint,
    deployment_jobs,
    deployment_row,
    expire,
    job_result,
    lease,
    lease_kind,
    make_due,
    receipt_value,
)

UNKNOWN = "delivery.deployment.unknown_outcome"


def _refused(r: Any, *loc: str) -> None:  # Any: an httpx response
    assert r.status_code == 422, r.text
    assert any(e["loc"][: 2 + len(loc)] == ["body", "value", *loc] for e in r.json()["errors"])


def _observing(flow: Flow, owner_db: Database) -> str:
    plan, deployment = flow.deployment()
    flow.apply(plan)
    make_due(owner_db, deployment)
    return deployment


def _verifying(flow: Flow, owner_db: Database) -> str:
    deployment = _observing(flow, owner_db)
    leased = lease_kind(flow.client, flow.token, "observe")
    r = job_result(
        flow.client, flow.token, leased, status="succeeded", value={"state": "succeeded"}
    )
    assert r.status_code == 200, r.text
    make_due(owner_db, deployment)
    return deployment


def _unknown(flow: Flow, owner_db: Database) -> str:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    expire(owner_db, leased["job"]["id"])
    assert flow.get(deployment)["status"] == "unknown_outcome"
    return deployment


def _audit_rows(owner_db: Database, deployment: object) -> list[Any]:  # Any: RowMappings
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT tenant_id, actor_type, actor_id, target_type, target_id, "
                    "workspace_id, project_id, details FROM shifde.audit_events "
                    "WHERE action = :a AND target_id = :d"
                ),
                {"a": UNKNOWN, "d": str(deployment)},
            )
            .mappings()
            .all()
        )


def _one_row(
    flow: Flow, owner_db: Database, deployment: str, source: str, job_id: str
) -> dict[str, Any]:
    """The deployment's one `unknown_outcome` row, checked against the deployment and its
    target; its details."""
    (row,) = _audit_rows(owner_db, deployment)
    dep = deployment_row(owner_db, deployment)
    with owner_db.session() as o:
        scope = o.execute(
            text("SELECT workspace_id, project_id FROM shifde.delivery_targets WHERE id = :g"),
            {"g": dep["target_id"]},
        ).one()
    assert row["tenant_id"] == flow.tenant == dep["tenant_id"]
    assert (row["actor_type"], row["actor_id"]) == ("anonymous", None)
    assert (row["target_type"], row["target_id"]) == ("delivery_deployment", deployment)
    assert (row["workspace_id"], row["project_id"]) == tuple(scope)
    details: dict[str, Any] = row["details"]
    assert details == {
        "deployment_id": deployment,
        "plan_id": str(dep["plan_id"]),
        "target_id": str(dep["target_id"]),
        "application_key": dep["application_key"],
        "reason": dep["status_reason"],
        "source": source,
        "job_id": job_id,
        "target": f"delivery_deployment:{dep['application_key']}",
    }
    return details


# ------------------------------------------------------------------ result limits
def test_reconcile_evidence_is_limited(flow: Flow, owner_db: Database) -> None:
    deployment = _unknown(flow, owner_db)
    assert flow.act(deployment, "reconcile").status_code == 200
    leased = lease_kind(flow.client, flow.token, "reconcile")
    over = {"state": "ambiguous", "evidence": "x" * (EVIDENCE_MAX + 1)}
    _refused(
        job_result(flow.client, flow.token, leased, status="succeeded", value=over), "evidence"
    )
    at = {"state": "ambiguous", "evidence": "x" * EVIDENCE_MAX}
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=at)
    assert r.status_code == 200, r.text
    assert flow.get(deployment)["reconcile_evidence"]["evidence"] == at["evidence"]


def test_an_apply_receipt_over_a_limit_is_refused_and_changes_nothing(
    flow: Flow, owner_db: Database
) -> None:
    plan, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    target = plan["target_id"]
    good = receipt_value(flow.tenant, target, plan["artifact_digest"])
    before = deployment_row(owner_db, deployment)
    for loc, value in (
        (("operation_handles",), {**good, "operation_handles": {"d": "h" * (NATIVE_ID_MAX + 1)}}),
        (("operation_handles",), {**good, "operation_handles": {"k" * (NAME_MAX + 1): "h"}}),
        (
            ("operation_handles",),
            {**good, "operation_handles": {f"op{i}": "h" for i in range(HANDLES_MAX + 1)}},
        ),
        (
            ("resources", "0", "native_id"),
            {**good, "resources": [binding(target, "n" * (NATIVE_ID_MAX + 1))]},
        ),
        (
            ("resources", "0", "key"),
            {**good, "resources": [{**binding(target), "key": "k" * (NAME_MAX + 1)}]},
        ),
        (("resources",), {**good, "resources": [binding(target, f"a{i}") for i in range(201)]}),
    ):
        r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
        _refused(r, *loc)
    assert deployment_row(owner_db, deployment) == before
    (job,) = deployment_jobs(owner_db, deployment)
    assert job["status"] == "leased" and job["result"] is None
    at = {
        **good,
        "resources": [binding(target, "n" * NATIVE_ID_MAX)],
        "operation_handles": {
            ("k" * (NAME_MAX - 3) + f"{i:03}"): "h" * NATIVE_ID_MAX for i in range(HANDLES_MAX)
        },
    }
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=at)
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "observing"
    assert row["receipt"]["resources"][0]["native_id"] == "n" * NATIVE_ID_MAX


def test_verify_check_evidence_is_limited(flow: Flow, owner_db: Database) -> None:
    deployment = _verifying(flow, owner_db)
    leased = lease_kind(flow.client, flow.token, "verify")
    checks = [{**BOTH_CHECKS[0], "evidence": "e" * (EVIDENCE_MAX + 1)}, BOTH_CHECKS[1]]
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    _refused(r, "checks", "0", "evidence")
    many = [{"name": f"c{i}", "status": "passed"} for i in range(ITEMS_MAX + 1)]
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": many})
    _refused(r, "checks")
    assert deployment_row(owner_db, deployment)["status"] == "verifying"
    checks = [{**BOTH_CHECKS[0], "evidence": "e" * EVIDENCE_MAX}, BOTH_CHECKS[1]]
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "verified"
    assert row["verification"]["checks"][0]["evidence"] == "e" * EVIDENCE_MAX


def test_observe_diagnostics_are_limited(flow: Flow, owner_db: Database) -> None:
    deployment = _observing(flow, owner_db)
    leased = lease_kind(flow.client, flow.token, "observe")
    for loc, diagnostics in (
        (("diagnostics", "0"), ["d" * (EVIDENCE_MAX + 1)]),
        (("diagnostics",), ["d"] * (ITEMS_MAX + 1)),
    ):
        value = {"state": "running", "diagnostics": diagnostics}
        _refused(job_result(flow.client, flow.token, leased, status="succeeded", value=value), *loc)
    assert deployment_row(owner_db, deployment)["status"] == "observing"
    value = {"state": "running", "diagnostics": ["d" * EVIDENCE_MAX] * ITEMS_MAX}
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ the unknown_outcome audit row
def test_a_failed_apply_after_an_intent_writes_one_audit_row(
    flow: Flow, owner_db: Database
) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    r = job_result(flow.client, flow.token, leased, status="failed", error_code="execution_failed")
    assert r.status_code == 200, r.text
    details = _one_row(flow, owner_db, deployment, "apply_result", leased["job"]["id"])
    assert details["reason"] == (
        "apply failed after a platform write may have happened: execution_failed"
    )
    assert lease(flow.client, flow.token).status_code == 204  # the settle step adds no row
    assert len(_audit_rows(owner_db, deployment)) == 1


def test_a_succeeded_apply_with_an_open_intent_writes_one_audit_row(
    flow: Flow, owner_db: Database
) -> None:
    plan, deployment = flow.deployment()
    leased = flow.apply(plan, complete=False)
    details = _one_row(flow, owner_db, deployment, "apply_result", leased["job"]["id"])
    assert details["reason"] == "a platform write did not complete"


def test_a_lost_apply_after_an_intent_writes_one_audit_row(flow: Flow, owner_db: Database) -> None:
    _, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    expire(owner_db, leased["job"]["id"])
    assert lease(flow.client, flow.token).status_code == 204  # the sweep and the settle step
    assert deployment_row(owner_db, deployment)["status"] == "unknown_outcome"
    details = _one_row(flow, owner_db, deployment, "executor_lost", leased["job"]["id"])
    assert details["reason"] == "the executor was lost after a platform write may have happened"
    # the settle step runs again, on another lease and on a user's read: still one row
    assert lease(flow.client, flow.token).status_code == 204
    assert flow.get(deployment)["status"] == "unknown_outcome"
    assert len(_audit_rows(owner_db, deployment)) == 1


def test_a_move_to_failed_writes_no_unknown_outcome_row(flow: Flow, owner_db: Database) -> None:
    _, reported = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    r = job_result(flow.client, flow.token, leased, status="failed", error_code="execution_failed")
    assert r.status_code == 200, r.text
    _, lost = flow.deployment(flow.target()["id"])
    leased = lease_kind(flow.client, flow.token, "apply")
    expire(owner_db, leased["job"]["id"])
    assert flow.get(lost)["status"] == "failed"
    for deployment in (reported, lost):
        assert deployment_row(owner_db, deployment)["status"] == "failed"
        assert _audit_rows(owner_db, deployment) == []
    with owner_db.session() as o:
        n = o.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t AND action = :a"),
            {"t": flow.tenant, "a": UNKNOWN},
        ).scalar_one()
    assert n == 0
