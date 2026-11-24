"""The runner on a Snowflake target (SP6-3): a test-only Native App artifact through plan, apply,
observe and verify on `FakeSnowflakeNativePlatform` over the in-process gateway, and every
credential refusal (an entry of another organization, an entry of the other platform) failing the
job `credential_unavailable` before any statement reaches the account. No key material reaches a
log, a job result or a receipt.

The plan, its approval and deployment are written by `tests.delivery.snowflake` (no Snowflake
release exists before SP6-4); the verify mapping to `verified` is SP6-6's, so the end to end run
stops at the stored verification report."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any  # Any: JSON bodies and rows
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from shifde.adapters.native.snowflake.adapter import STEPS
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from shifde.core.db import Database
from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker, snowflake_account
from shifde.executor.runner import FakePlatform, Runner, toolchain_digest
from tests.connections.routes import as_user
from tests.delivery.routes import (
    Flow,
    deployment_jobs,
    deployment_row,
    job_row,
    make_due,
    plan_row,
    register,
    snowflake_address,
    snowflake_body,
)
from tests.delivery.snowflake import approve, snowflake_plan
from tests.executor.support import (
    SNOWFLAKE_USER,
    credentials_file,
    fake_platform,
    snowflake_credentials_file,
    snowflake_key_pem,
)

PEM = snowflake_key_pem()
SNOWFLAKE_CHECKS = {"native-version", "runtime-health", "app-deep-health"}


def _target(flow: Flow) -> dict[str, Any]:  # Any: a TargetOut body
    h = as_user(flow.client, flow.requester)
    made: dict[str, Any] = register(
        flow.client, h, flow.slug, snowflake_body(flow.workspace, snowflake_address())
    )
    flow.client.cookies.clear()
    return made


def _runner(flow: Flow, creds: Path, platform: FakePlatform) -> Runner:
    gateway = GatewayClient("https://testserver", flow.token, http=flow.client)
    me = gateway.whoami()
    return Runner(
        gateway,
        CredentialBroker.from_file(creds),
        me.executor_id,
        platform,
        tenant_id=me.tenant_id,
    )


def _account_fake(address: str) -> FakeSnowflakeNativePlatform:
    """A Snowflake fake that accepts `PEM`'s JWTs for the target's account."""
    key = serialization.load_pem_private_key(PEM.encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return FakeSnowflakeNativePlatform(
        environment="test",
        public_key=key.public_key(),
        account=snowflake_account(address),
        user=SNOWFLAKE_USER,
    )


def _native_types(owner_db: Database, deployment_id: object) -> list[str]:
    with owner_db.session() as o:
        return sorted(
            o.execute(
                text(
                    "SELECT resource_type FROM shifde.delivery_native_resources "
                    "WHERE deployment_id = :d"
                ),
                {"d": deployment_id},
            ).scalars()
        )


def test_plan_apply_observe_and_verify_run_on_the_snowflake_fake(
    flow: Flow,
    tmp_path: Path,
    app_db: Database,
    owner_db: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    target = _target(flow)
    creds = snowflake_credentials_file(tmp_path, target["address"], PEM, tenant=flow.tenant)
    platform = FakePlatform(fake_platform(), environment="test")
    runner = _runner(flow, creds, platform)

    # (a) the plan job seals the plan with the eight Native App steps, reading only
    plan = snowflake_plan(app_db, flow.tenant, target["id"])
    assert runner.run_once()
    sealed = plan_row(owner_db, plan.id)
    assert sealed["status"] == "sealed", sealed["status_reason"]
    assert [s["key"] for s in sealed["steps"]] == list(STEPS)
    assert sealed["toolchain_digest"] == toolchain_digest("fake", "snowflake")
    assert toolchain_digest("fake", "snowflake") not in (
        toolchain_digest("databricks", "snowflake"),
        toolchain_digest("fake"),
    )
    fake = platform.snowflake
    assert fake is not None and fake.log and fake.writes() == []

    # (b) apply writes each step once, receipts marked `platform: fake`
    deployment_id = approve(app_db, flow.tenant, plan.id)
    assert runner.run_once()
    body = flow.get(deployment_id)
    assert body["status"] == "observing", body
    assert {s["step_key"]: s["state"] for s in body["journal"]} == dict.fromkeys(STEPS, "complete")
    assert all(s["receipt"]["platform"] == "fake" for s in body["journal"])
    assert body["receipt"]["operation_handles"]["platform"] == "fake"
    writes = fake.writes()
    assert len(writes) == len(set(writes)) == 7  # `setup-validate` writes nothing
    for prefix in (
        "CREATE APPLICATION PACKAGE",
        "CREATE SCHEMA",
        "CREATE STAGE",
        "COPY ",
        "ALTER APPLICATION PACKAGE",
        'CREATE APPLICATION "',
        "GRANT APPLICATION ROLE",
    ):
        assert sum(1 for w in writes if w.startswith(prefix)) == 1, prefix
    assert _native_types(owner_db, deployment_id) == sorted(
        [
            "snowflake.application",
            "snowflake.application_package",
            "snowflake.application_version",
            "snowflake.stage",
        ]
    )

    # (c) observe moves the deployment to `verifying`
    make_due(owner_db, deployment_id)
    assert runner.run_once()
    assert flow.get(deployment_id)["status"] == "verifying"

    # (d) verify posts the three Snowflake checks, all passed
    make_due(owner_db, deployment_id)
    assert runner.run_once()
    verification = deployment_row(owner_db, deployment_id)["verification"]
    assert {c["name"]: c["status"] for c in verification["checks"]} == dict.fromkeys(
        SNOWFLAKE_CHECKS, "passed"
    )
    assert fake.writes() == writes  # observe and verify read only

    # no key material in the logs, the job results or the stored receipt
    results = [j["result"] for j in deployment_jobs(owner_db, deployment_id)]
    results.append(job_row(owner_db, plan.job_id)["result"])
    stored = deployment_row(owner_db, deployment_id)["receipt"]
    for leak in (PEM[40:120], "PRIVATE KEY", str(tmp_path), "credentials.json.p8"):
        for blob in (caplog.text, str(results), str(stored), str(body)):
            assert leak not in blob


def _foreign_fails(
    flow: Flow, tmp_path: Path, app_db: Database, owner_db: Database, creds: Path, target: Any
) -> tuple[UUID, FakeSnowflakeNativePlatform]:
    """A plan job of `target` run with `creds`: the plan's id and the (untouched) fake."""
    fake = _account_fake(target["address"])
    platform = FakePlatform(fake_platform(), snowflake=fake, environment="test")
    plan = snowflake_plan(app_db, flow.tenant, target["id"])
    assert _runner(flow, creds, platform).run_once()
    return plan.id, fake


def test_an_entry_of_another_organization_fails_before_any_statement(
    flow: Flow, tmp_path: Path, app_db: Database, owner_db: Database
) -> None:
    target = _target(flow)
    # the same host and reference, but the entry belongs to another organization
    foreign = snowflake_credentials_file(tmp_path, target["address"], PEM, tenant=uuid4())
    plan_id, fake = _foreign_fails(flow, tmp_path, app_db, owner_db, foreign, target)
    failed = plan_row(owner_db, plan_id)
    assert failed["status"] == "plan_failed"
    assert failed["status_reason"] == "plan job failed: credential_unavailable"
    assert fake.log == []


def test_a_databricks_entry_under_the_targets_reference_fails_before_any_statement(
    flow: Flow, tmp_path: Path, app_db: Database, owner_db: Database
) -> None:
    target = _target(flow)
    databricks = credentials_file(
        tmp_path, target["address"], tenant=flow.tenant, ref=target["credential_ref"]
    )
    plan_id, fake = _foreign_fails(flow, tmp_path, app_db, owner_db, databricks, target)
    assert plan_row(owner_db, plan_id)["status_reason"] == (
        "plan job failed: credential_unavailable"
    )
    assert fake.log == []


def test_an_apply_with_an_entry_of_another_organization_writes_nothing(
    flow: Flow, tmp_path: Path, app_db: Database, owner_db: Database
) -> None:
    target = _target(flow)
    creds = snowflake_credentials_file(tmp_path, target["address"], PEM, tenant=flow.tenant)
    fake = _account_fake(target["address"])
    platform = FakePlatform(fake_platform(), snowflake=fake, environment="test")
    plan = snowflake_plan(app_db, flow.tenant, target["id"])
    assert _runner(flow, creds, platform).run_once()
    deployment_id = approve(app_db, flow.tenant, plan.id)
    seen = list(fake.log)
    foreign = snowflake_credentials_file(
        tmp_path, target["address"], PEM, tenant=uuid4(), name="foreign.json"
    )
    assert _runner(flow, foreign, platform).run_once()
    assert deployment_row(owner_db, deployment_id)["status"] == "failed"
    jobs = [j for j in deployment_jobs(owner_db, deployment_id) if j["kind"] == "apply"]
    assert jobs[0]["result"] == {"status": "failed", "error_code": "credential_unavailable"}
    assert fake.log == seen
