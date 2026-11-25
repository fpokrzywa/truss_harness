"""The deployment step journal behind `POST /api/executor/v1/jobs/{job_id}/checkpoint` (design
section 4; B-2669): intent before write, no changed input digest, no conflicting receipt, an
intent of an earlier attempt is an unknown outcome, the checkpoint sequence only moves forward,
and a stale fence cannot write.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from shifde.core.db import Database
from tests.delivery.conftest import ExecutorSide
from tests.delivery.routes import (
    Flow,
    bearer,
    deployment_row,
    enqueue,
    enqueue_on_target,
    expire,
    job_row,
    job_url,
    journal_rows,
    lease,
    make_deployment,
    proof,
)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
RECEIPT = {"app": "orders", "handles": {"deploy": "op-1"}}


class Step:
    """One leased job and its checkpoint sequence."""

    def __init__(self, client: TestClient, ex: ExecutorSide, leased: dict[str, Any]) -> None:
        self.client, self.ex, self.leased = client, ex, leased
        self.job = leased["job"]["id"]
        self.sequence = 0

    def post(
        self, state: str, digest: str = DIGEST, step: str = "deploy_app", **extra: Any
    ) -> httpx.Response:
        self.sequence += 1
        body = {
            **proof(self.leased),
            "sequence": self.sequence,
            "step_key": step,
            "state": state,
            "input_digest": digest,
            **extra,
        }
        return self.client.post(
            job_url(self.job, "checkpoint"), json=body, headers=bearer(self.ex.token)
        )


def _step(client: TestClient, ex: ExecutorSide, app_db: Database, deployment: UUID | None) -> Step:
    if deployment is None:
        enqueue(app_db, ex.tenant, "observe")
    else:
        enqueue_on_target(app_db, ex.tenant, "observe", deployment)
    r = lease(client, ex.token)
    assert r.status_code == 200, r.text
    return Step(client, ex, r.json())


def _ok(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _code(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status and r.json()["code"] == code, r.text


def test_intent_then_complete(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    assert _ok(step.post("intent")) == {
        "step_key": "deploy_app",
        "input_digest": DIGEST,
        "state": "intent",
        "receipt": None,
    }
    assert _ok(step.post("complete", receipt=RECEIPT)) == {
        "step_key": "deploy_app",
        "input_digest": DIGEST,
        "state": "complete",
        "receipt": RECEIPT,
    }
    (row,) = journal_rows(owner_db, executor_a.tenant)
    assert row[2:9] == ("deploy_app", DIGEST, "complete", RECEIPT, UUID(step.job), 1, row[8])
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 2


def test_complete_without_intent(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    _code(step.post("complete", receipt=RECEIPT), 409, "no_intent")
    assert journal_rows(owner_db, executor_a.tenant) == []
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 0


def test_a_changed_input_digest_is_refused(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    _ok(step.post("intent"))
    before = journal_rows(owner_db, executor_a.tenant)
    _code(step.post("intent", OTHER_DIGEST), 409, "input_digest_changed")
    _code(step.post("complete", OTHER_DIGEST, receipt=RECEIPT), 409, "input_digest_changed")
    _ok(step.post("complete", receipt=RECEIPT))
    _code(step.post("intent", OTHER_DIGEST), 409, "input_digest_changed")
    _code(step.post("complete", OTHER_DIGEST, receipt=RECEIPT), 409, "input_digest_changed")
    assert before[0][4] == "intent"
    assert journal_rows(owner_db, executor_a.tenant)[0][3:6] == (DIGEST, "complete", RECEIPT)


def test_a_conflicting_receipt_is_refused_and_identical_reposts_are_accepted(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    intent = _ok(step.post("intent"))
    assert _ok(step.post("intent")) == intent  # same job and fence: unchanged
    done = _ok(step.post("complete", receipt=RECEIPT))
    after = journal_rows(owner_db, executor_a.tenant)
    assert _ok(step.post("complete", receipt=RECEIPT)) == done
    assert _ok(step.post("intent")) == done  # the step is already complete
    _code(step.post("complete", receipt={"app": "other"}), 409, "conflicting_receipt")
    assert journal_rows(owner_db, executor_a.tenant) == after


def test_an_intent_of_another_job_or_fence_is_an_unknown_outcome(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    deployment = make_deployment(owner_db, executor_a.tenant)
    first = _step(client, executor_a, app_db, deployment)
    _ok(first.post("intent"))
    # another job of the same deployment
    other = _step(client, executor_a, app_db, deployment)
    _code(other.post("intent"), 409, "step_outcome_unknown")
    # the same job, leased again after its lease expired (fence 2)
    expire(owner_db, first.job)
    expire(owner_db, other.job)
    r = lease(client, executor_a.token, kinds=["observe"])
    again = Step(client, executor_a, r.json())
    assert again.job in (first.job, other.job) and again.leased["fence"] == 2
    _code(again.post("intent"), 409, "step_outcome_unknown")
    # completing it is still possible: the receipt settles the step
    _ok(again.post("complete", receipt=RECEIPT))


def test_the_sequence_only_moves_forward(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    step.sequence = 4
    _ok(step.post("intent", step="one"))  # sequence 5
    for sequence in (5, 3):
        step.sequence = sequence - 1
        _code(step.post("intent", step="two"), 409, "stale_sequence")
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 5
    step.sequence = 5
    _code(step.post("complete", step="two", receipt=RECEIPT), 409, "no_intent")  # sequence 6
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 5  # a refusal does not advance it
    step.sequence = 5
    _ok(step.post("intent", step="two"))
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 6


def test_a_job_without_a_deployment_has_no_journal(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, None)
    _code(step.post("intent"), 409, "no_journal")
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 0


def test_a_receipt_is_accepted_after_the_target_is_archived(
    flow: Flow, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    deployment = make_deployment(owner_db, executor_a.tenant)
    step = _step(flow.client, executor_a, app_db, deployment)
    _ok(step.post("intent"))
    flow.archive(deployment_row(owner_db, deployment)["target_id"])
    done = _ok(step.post("complete", receipt=RECEIPT))
    assert done["state"] == "complete" and done["receipt"] == RECEIPT
    after = journal_rows(owner_db, executor_a.tenant)
    assert [row[2:6] for row in after] == [("deploy_app", DIGEST, "complete", RECEIPT)]
    assert _ok(step.post("complete", receipt=RECEIPT)) == done
    assert journal_rows(owner_db, executor_a.tenant) == after
    assert job_row(owner_db, step.job)["checkpoint_seq"] == 3


def test_a_recorded_intent_is_accepted_again_after_the_target_is_archived(
    flow: Flow, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    deployment = make_deployment(owner_db, executor_a.tenant)
    step = _step(flow.client, executor_a, app_db, deployment)
    _ok(step.post("intent", step="one"))
    done = _ok(step.post("complete", step="one", receipt=RECEIPT))
    intent = _ok(step.post("intent", step="two"))
    other = _step(flow.client, executor_a, app_db, deployment)  # another job of the deployment
    before = journal_rows(owner_db, executor_a.tenant)
    flow.archive(deployment_row(owner_db, deployment)["target_id"])
    assert _ok(step.post("intent", step="two")) == intent
    assert _ok(step.post("intent", step="one")) == done
    for key in ("one", "two"):
        _code(step.post("intent", OTHER_DIGEST, step=key), 409, "input_digest_changed")
    _code(step.post("intent", step="three"), 409, "target_unavailable")  # a new step
    _code(other.post("intent", step="two"), 409, "step_outcome_unknown")
    # the same job, leased again after its lease expired (fence 2)
    expire(owner_db, step.job)
    expire(owner_db, other.job)
    r = lease(flow.client, executor_a.token, kinds=["observe"])
    again = Step(flow.client, executor_a, r.json())
    again.sequence = 100
    assert again.job in (step.job, other.job) and again.leased["fence"] == 2
    _code(again.post("intent", step="two"), 409, "step_outcome_unknown")
    assert journal_rows(owner_db, executor_a.tenant) == before


def test_an_intent_of_a_job_without_a_target_is_refused(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    deployment = make_deployment(owner_db, executor_a.tenant)
    enqueue(app_db, executor_a.tenant, "observe", deployment_id=deployment)
    step = Step(client, executor_a, lease(client, executor_a.token).json())
    _code(step.post("intent"), 409, "target_unavailable")
    assert journal_rows(owner_db, executor_a.tenant) == []
    row = job_row(owner_db, step.job)
    assert (row["status"], row["checkpoint_seq"]) == ("leased", 0)


def test_a_stale_fence_cannot_checkpoint(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    """Design section 4, the fourth crash test: a superseded executor writes nothing."""
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    expire(owner_db, step.job)
    fresh = Step(client, executor_a, lease(client, executor_a.token).json())
    assert fresh.job == step.job and fresh.leased["fence"] == 2
    _code(step.post("intent"), 409, "lease_lost")
    assert journal_rows(owner_db, executor_a.tenant) == []
    _ok(fresh.post("intent"))
    _code(step.post("complete", receipt=RECEIPT), 409, "lease_lost")
    assert journal_rows(owner_db, executor_a.tenant)[0][4] == "intent"


def test_receipt_is_required_for_complete_and_refused_for_intent(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    step = _step(client, executor_a, app_db, make_deployment(owner_db, executor_a.tenant))
    for state, extra in (("complete", {}), ("intent", {"receipt": RECEIPT})):
        r = step.post(state, **extra)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", (state, r.text)
    for field, value in (("input_digest", "A" * 64), ("step_key", "1bad"), ("sequence", 0)):
        r = step.post("intent", **{field: value})
        assert r.status_code == 422, (field, r.text)
    assert journal_rows(owner_db, executor_a.tenant) == []
