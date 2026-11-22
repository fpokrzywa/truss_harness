"""The runner against the in-process gateway and the fake platform (B-2688..B-2695): plan and
apply through the adapter, a broker refusal before any platform call, the artifact digest, write
once with an unknown outcome, a lost lease stopping every further write, and no token in any log,
output or `repr`."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: JSON bodies
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from shifde.adapters.native.contracts import JobContext, JobLease, LeasedJob, utcnow
from shifde.adapters.native.databricks.adapter import DatabricksAdapter
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.core.db import Database
from shifde.delivery import gateway
from shifde.executor.client import GatewayClient, LeaseLost
from shifde.executor.runner import LeaseState, application_id, toolchain_digest
from tests.delivery.conftest import ExecutorSide
from tests.delivery.routes import Flow, deployment_jobs, deployment_row, expire, job_row
from tests.executor.support import (
    CREDENTIAL_TOKEN,
    ask_plan,
    credentials_file,
    deploy_actions,
    fake_platform,
    make_runner,
)


def _approved(
    flow: Flow, tmp_path: Path, fake: FakeDatabricksPlatform
) -> tuple[dict[str, Any], str, Path]:
    """A target, its credentials file, a plan sealed by the runner and approved: (plan,
    deployment id, credentials file)."""
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    plan = ask_plan(flow, target["id"])
    assert make_runner(flow.client, flow.token, creds, fake).run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed
    approved = flow.approve(sealed)
    return approved, approved["deployment_id"], creds


def test_plan_seals_with_the_fake_toolchain(flow: Flow, tmp_path: Path) -> None:
    fake = fake_platform()
    approved, _, _ = _approved(flow, tmp_path, fake)
    assert (
        approved["toolchain_digest"] == toolchain_digest("fake") != toolchain_digest("databricks")
    )
    assert [s["key"] for s in approved["steps"]] == ["bundle-deploy", "bundle-run"]
    assert approved["steps"][0]["action"] == "create"
    assert fake.actions == []  # planning reads only


def test_apply_writes_once_and_marks_the_fake(flow: Flow, tmp_path: Path) -> None:
    fake = fake_platform()
    approved, deployment_id, creds = _approved(flow, tmp_path, fake)
    runner = make_runner(flow.client, flow.token, creds, fake)
    assert runner.run_once()
    body = flow.get(deployment_id)
    assert body["status"] == "observing", body
    assert body["receipt"]["operation_handles"]["platform"] == "fake"
    assert body["receipt"]["application_id"] == str(
        application_id(approved["target_id"], "sales-app")
    )
    assert [(s["step_key"], s["state"]) for s in body["journal"]] == [
        ("bundle-deploy", "complete"),
        ("bundle-run", "complete"),
    ]
    assert all(s["receipt"]["platform"] == "fake" for s in body["journal"])
    assert (deploy_actions(fake, "deploy"), deploy_actions(fake, "run")) == (1, 1)


def test_a_broker_refusal_fails_the_job_before_any_platform_call(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    fake = fake_platform()
    _, deployment_id, _ = _approved(flow, tmp_path, fake)
    other = credentials_file(
        tmp_path, "https://adb-other.cloud.databricks.com", tenant=flow.tenant, name="other.json"
    )
    assert make_runner(flow.client, flow.token, other, fake).run_once()
    row = deployment_row(owner_db, deployment_id)
    assert row["status"] == "failed"
    jobs = [j for j in deployment_jobs(owner_db, deployment_id) if j["kind"] == "apply"]
    assert jobs[0]["result"] == {"status": "failed", "error_code": "credential_unavailable"}
    assert fake.actions == []


def test_a_credentials_entry_of_another_tenant_fails_the_job_before_any_platform_call(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    fake = fake_platform()
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    plan = ask_plan(flow, target["id"])
    assert make_runner(flow.client, flow.token, creds, fake).run_once()
    deployment_id = flow.approve(flow.get_plan(plan["id"]))["deployment_id"]
    # the same host and reference, but the entry belongs to another organization
    foreign = credentials_file(tmp_path, target["address"], tenant=uuid4(), name="foreign.json")
    assert make_runner(flow.client, flow.token, foreign, fake).run_once()
    assert deployment_row(owner_db, deployment_id)["status"] == "failed"
    jobs = [j for j in deployment_jobs(owner_db, deployment_id) if j["kind"] == "apply"]
    assert jobs[0]["result"] == {"status": "failed", "error_code": "credential_unavailable"}
    assert fake.actions == []


def test_a_context_naming_another_job_fails_before_any_credential_grant(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_platform()
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    context = GatewayClient.context
    grants: list[JobLease] = []

    def other_job(self: GatewayClient, lease: JobLease) -> JobContext:
        ctx = context(self, lease)
        return ctx.model_copy(update={"job": ctx.job.model_copy(update={"id": uuid4()})})

    monkeypatch.setattr(GatewayClient, "context", other_job)
    monkeypatch.setattr(GatewayClient, "credential_grant", lambda _, lease: grants.append(lease))
    plan = ask_plan(flow, target["id"])
    assert make_runner(flow.client, flow.token, creds, fake).run_once()
    failed = flow.get_plan(plan["id"])
    assert failed["status"] == "plan_failed", failed
    assert failed["status_reason"] == "plan job failed: execution_failed"
    assert grants == []
    assert fake.actions == []


def _plan_job_fails_on(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: Any
) -> FakeDatabricksPlatform:
    """A plan job whose context `update(ctx)` rewrites ends `execution_failed` before any
    credential grant; the fake platform."""
    fake = fake_platform()
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    context = GatewayClient.context
    grants: list[JobLease] = []
    monkeypatch.setattr(GatewayClient, "context", lambda self, lease: update(context(self, lease)))
    monkeypatch.setattr(GatewayClient, "credential_grant", lambda _, lease: grants.append(lease))
    plan = ask_plan(flow, target["id"])
    assert make_runner(flow.client, flow.token, creds, fake).run_once()
    failed = flow.get_plan(plan["id"])
    assert failed["status"] == "plan_failed", failed
    assert failed["status_reason"] == "plan job failed: execution_failed"
    assert grants == []
    return fake


def test_a_context_whose_plan_names_another_tenant_fails_before_any_credential_grant(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def other_plan_tenant(ctx: JobContext) -> JobContext:
        assert ctx.plan is not None and ctx.plan.tenant_id == flow.tenant
        return ctx.model_copy(update={"plan": ctx.plan.model_copy(update={"tenant_id": uuid4()})})

    assert _plan_job_fails_on(flow, tmp_path, monkeypatch, other_plan_tenant).actions == []


def test_a_context_whose_target_is_not_the_pinned_tenant_fails_before_any_credential_grant(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def other_tenant(ctx: JobContext) -> JobContext:
        # target and plan agree with each other, but not with the executor's organization
        other = uuid4()
        assert ctx.plan is not None and ctx.target.tenant_id == flow.tenant
        return ctx.model_copy(
            update={
                "target": ctx.target.model_copy(update={"tenant_id": other}),
                "plan": ctx.plan.model_copy(update={"tenant_id": other}),
            }
        )

    assert _plan_job_fails_on(flow, tmp_path, monkeypatch, other_tenant).actions == []


def test_a_context_whose_deployment_names_another_tenant_fails_before_any_credential_grant(
    flow: Flow, tmp_path: Path, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_platform()
    _, deployment_id, creds = _approved(flow, tmp_path, fake)
    context = GatewayClient.context
    grants: list[JobLease] = []

    def other_deployment_tenant(self: GatewayClient, lease: JobLease) -> JobContext:
        ctx = context(self, lease)
        assert ctx.deployment is not None and ctx.deployment.tenant_id == flow.tenant
        deployment = ctx.deployment.model_copy(update={"tenant_id": uuid4()})
        return ctx.model_copy(update={"deployment": deployment})

    monkeypatch.setattr(GatewayClient, "context", other_deployment_tenant)
    monkeypatch.setattr(GatewayClient, "credential_grant", lambda _, lease: grants.append(lease))
    assert make_runner(flow.client, flow.token, creds, fake).run_once()
    assert deployment_row(owner_db, deployment_id)["status"] == "failed"
    jobs = [j for j in deployment_jobs(owner_db, deployment_id) if j["kind"] == "apply"]
    assert jobs[0]["result"] == {"status": "failed", "error_code": "execution_failed"}
    assert grants == []
    assert fake.actions == []


def test_an_artifact_that_is_not_the_plans_is_refused_before_the_adapter(
    flow: Flow, tmp_path: Path, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_platform()
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    called: list[str] = []
    monkeypatch.setattr(DatabricksAdapter, "plan", lambda *a: called.append("plan"))
    other = b"not the plan's artifact"
    for served in (
        gateway.Artifact(content=other, digest=hashlib.sha256(other).hexdigest()),  # not the plan's
        gateway.Artifact(content=other, digest="0" * 64),  # the header does not match
    ):
        monkeypatch.setattr(gateway, "artifact", lambda *a, served=served: served)
        plan = ask_plan(flow, target["id"], application_key=f"app-{uuid4().hex[:8]}")
        assert make_runner(flow.client, flow.token, creds, fake).run_once()
        failed = flow.get_plan(plan["id"])
        assert failed["status"] == "plan_failed", failed
        assert failed["status_reason"] == "plan job failed: execution_failed"
    assert called == []


def test_a_write_accepted_then_lost_is_an_unknown_outcome_and_never_repeated(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    fake = fake_platform()
    _, deployment_id, creds = _approved(flow, tmp_path, fake)
    fake.fail_after_accept = True
    runner = make_runner(flow.client, flow.token, creds, fake)
    assert runner.run_once()
    row = deployment_row(owner_db, deployment_id)
    assert row["status"] == "unknown_outcome", row["status_reason"]
    assert deploy_actions(fake, "deploy") == 1 and deploy_actions(fake, "run") == 0
    fake.fail_after_accept = False
    assert not runner.run_once()
    assert deploy_actions(fake, "deploy") == 1
    assert deployment_row(owner_db, deployment_id)["status"] == "unknown_outcome"


def test_a_lease_lost_after_the_first_intent_stops_every_further_write(
    flow: Flow, tmp_path: Path, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_platform()
    _, deployment_id, creds = _approved(flow, tmp_path, fake)
    apply_job = next(j for j in deployment_jobs(owner_db, deployment_id) if j["kind"] == "apply")
    execute = fake.execute

    def lose_lease(action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if action == "deploy":
            expire(owner_db, apply_job["id"])  # the lease runs out while the write is in flight
        return execute(action, *args, **kwargs)

    monkeypatch.setattr(fake, "execute", lose_lease)
    runner = make_runner(flow.client, flow.token, creds, fake)
    assert runner.run_once()
    assert deploy_actions(fake, "deploy") == 1 and deploy_actions(fake, "run") == 0
    job = job_row(owner_db, apply_job["id"])
    assert job["result"] is None  # no result was posted, succeeded or failed
    journal = flow.get(deployment_id)["journal"]
    assert [(s["step_key"], s["state"]) for s in journal] == [("bundle-deploy", "intent")]
    assert not runner.run_once()  # the lease call sweeps the lost apply
    assert deployment_row(owner_db, deployment_id)["status"] == "unknown_outcome"
    assert deploy_actions(fake, "deploy") == 1 and deploy_actions(fake, "run") == 0


def test_the_write_guard_refuses_once_the_lease_is_lost_or_ran_out() -> None:
    lease = JobLease(
        job=LeasedJob(
            id=uuid4(),
            kind="apply",
            plan_id=uuid4(),
            deployment_id=uuid4(),
            target_id=uuid4(),
            deadline=None,
            attempts=1,
            max_attempts=1,
        ),
        lease_id=uuid4(),
        fence=1,
        lease_until=utcnow() + timedelta(seconds=45),
        request_id=uuid4(),
    )
    state = LeaseState(lease=lease, lease_until=lease.lease_until)
    state.guard()
    state.lost = True
    with pytest.raises(LeaseLost):
        state.guard()
    ran_out = LeaseState(lease=lease, lease_until=utcnow() - timedelta(seconds=1))
    with pytest.raises(LeaseLost):
        ran_out.guard()
    assert ran_out.lost


def test_no_token_reaches_a_log_the_output_or_a_repr(
    flow: Flow,
    tmp_path: Path,
    client: TestClient,
    executor_a: ExecutorSide,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = fake_platform()
    _, deployment_id, creds = _approved(flow, tmp_path, fake)
    runner = make_runner(flow.client, flow.token, creds, fake)
    assert runner.run_once()
    assert flow.get(deployment_id)["status"] == "observing"
    out, err = capsys.readouterr()
    texts = (caplog.text, out, err, repr(runner.broker), repr(runner.client), repr(runner))
    for text in texts:
        assert executor_a.token not in text
        assert CREDENTIAL_TOKEN not in text
    assert "running apply job" in caplog.text  # the runner did log
