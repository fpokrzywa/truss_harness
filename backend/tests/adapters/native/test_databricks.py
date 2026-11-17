"""The FDE SDK Databricks adapter tests (1fd902b `tests/adapters/test_databricks.py`), synchronous,
over the fake platform. Tests 10, 12, 13 and 14 used the dropped `LocalDeliveryService`; they run
at the adapter level here with the same intent.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

import shifde.adapters.native.planning as planning
from shifde.adapters.native.base import (
    InMemoryJournal,
    NeedsAttention,
    create_archive,
    read_archive,
)
from shifde.adapters.native.contracts import (
    ApplyRequest,
    DatabricksApplicationSpec,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ScaffoldRequest,
    ValidateRequest,
    VerifyRequest,
    utcnow,
)
from shifde.adapters.native.databricks.adapter import app_name
from shifde.adapters.native.planning import seal_plan
from tests.adapters.native.conftest import Setup


def test_scaffold_build_produces_immutable_source_without_credentials(setup: Setup) -> None:
    artifact = setup.artifact()
    files = read_archive(setup.build.artifacts.read(artifact.archive, setup.build.tenant_id))
    assert {"databricks.yml", "app/app.py", "app/app.yaml", "app/requirements.txt"} <= files.keys()
    assert b"/health" in files["app/app.py"]
    assert b"SELECT 1" in files["app/app.py"]
    assert b"DATABRICKS_TOKEN" not in b"".join(files.values())
    assert artifact.archive.digest == artifact.source_digest


def test_application_controlled_bundle_hooks_are_rejected(setup: Setup) -> None:
    build = setup.build
    source = setup.adapter.scaffold(ScaffoldRequest(spec=DatabricksApplicationSpec()), build)
    files = read_archive(build.artifacts.read(source, build.tenant_id))
    files["databricks.yml"] += b"\nscripts:\n  steal: echo secret\n"
    unsafe = build.artifacts.put(build.tenant_id, create_archive(files))
    validation = setup.adapter.validate(
        ValidateRequest(spec=DatabricksApplicationSpec(), source=unsafe), build
    )
    assert not validation.passed


def test_deploy_starts_app_after_bundle_and_checks_actual_health(setup: Setup) -> None:
    adapter, platform = setup.adapter, setup.platform
    artifact, plan, deploy = setup.prepare()
    receipt = adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [item[0] for item in platform.actions] == ["deploy", "run"]
    assert platform.actions[1][1] == "app"
    assert platform.actions[0][2] == platform.actions[1][2]
    assert str(artifact.application_id) in platform.actions[0][2]
    name = app_name(artifact.application_id, setup.read.target.id)
    assert receipt.resources[0].native_id == platform.apps[name]["id"]
    assert adapter.observe(ObserveRequest(receipt=receipt), setup.read).state == "succeeded"
    assert adapter.verify(VerifyRequest(receipt=receipt), setup.read).passed


def test_cli_success_does_not_hide_startup_failure(setup: Setup) -> None:
    setup.platform.crash_app = True
    artifact, plan, deploy = setup.prepare()
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.adapter.observe(ObserveRequest(receipt=receipt), deploy).state == "failed"
    assert not setup.adapter.verify(VerifyRequest(receipt=receipt), deploy).passed


def test_uncertain_native_acceptance_is_not_replayed(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.fail_after_accept = True
    with pytest.raises(TimeoutError):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    with pytest.raises(NeedsAttention):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(setup.platform.actions) == 1
    outcome = setup.adapter.reconcile(ReconcileRequest(plan=plan), deploy)
    assert outcome.state == "ambiguous"


def test_stale_and_altered_artifacts_are_rejected_before_mutation(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    with pytest.raises(ValueError):
        setup.adapter.apply(
            ApplyRequest(artifact=artifact.model_copy(update={"spec_digest": "b" * 64}), plan=plan),
            deploy,
        )
    with pytest.raises(ValueError):
        setup.adapter.apply(
            ApplyRequest(
                artifact=artifact,
                plan=plan.model_copy(update={"expires_at": utcnow() - timedelta(seconds=1)}),
            ),
            deploy,
        )
    assert setup.platform.actions == []


def test_preexisting_unowned_app_cannot_be_adopted_by_name(setup: Setup) -> None:
    application_id = uuid4()
    name = app_name(application_id, setup.read.target.id)
    setup.platform.apps[name] = {"name": name, "id": "someone-elses-app"}
    with pytest.raises(PermissionError):
        setup.prepare(application_id)


def test_health_requires_runtime_warehouse_check(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    setup.platform.health_bad = True
    assert not setup.adapter.verify(VerifyRequest(receipt=receipt), deploy).passed


def test_start_cannot_report_success_using_previous_deployment(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.no_new_deployment = True
    with pytest.raises(NeedsAttention):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def test_local_service_persists_ownership_across_restart(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.adapter.verify(VerifyRequest(receipt=receipt), setup.read).passed
    restarted = setup.new_adapter()
    read = type(setup.read)(
        tenant_id=setup.read.tenant_id,
        target=setup.read.target,
        owned_resources=tuple(receipt.resources),
    )
    updated_plan = restarted.plan(PlanRequest(artifact=artifact), read)
    assert updated_plan.steps[0].action == "update"
    assert updated_plan.snapshot.resources[0].native_id == receipt.resources[0].native_id


def test_pending_failed_deployment_cannot_be_hidden_by_previous_active_app(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    app = setup.platform.apps[receipt.operation_handles["app_name"]]
    app["active_deployment"]["deployment_id"] = "previous"
    app["pending_deployment"] = {
        "deployment_id": receipt.operation_handles["deployment_id"],
        "status": {"state": "FAILED"},
    }
    assert setup.adapter.observe(ObserveRequest(receipt=receipt), deploy).state == "failed"


def test_new_plan_cannot_bypass_an_uncertain_prior_operation(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.fail_after_accept = True
    with pytest.raises(TimeoutError):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    setup.platform.fail_after_accept = False
    fresh = seal_plan(plan.model_copy(update={"id": uuid4()}))
    for candidate in (plan, fresh):
        with pytest.raises(NeedsAttention):
            setup.adapter.apply(ApplyRequest(artifact=artifact, plan=candidate), deploy)
    assert len(setup.platform.actions) == 1
    assert setup.adapter.reconcile(ReconcileRequest(plan=fresh), deploy).state == "ambiguous"


def test_restart_with_changed_toolchain_rejects_before_native_mutation(setup: Setup) -> None:
    artifact, plan, _ = setup.prepare()
    restarted = setup.new_adapter()
    deploy = setup.deploy_context(toolchain_digest="b" * 64)
    with pytest.raises(ValueError, match="toolchain"):
        restarted.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.platform.actions == []


def test_completed_receipt_can_recover_bookkeeping_after_start_ttl(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, plan, deploy = setup.prepare()
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    monkeypatch.setattr(planning, "utcnow", lambda: plan.expires_at + timedelta(minutes=1))
    replayed = setup.new_adapter().apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert replayed == receipt
    assert len(setup.platform.actions) == 2
    # The start TTL still bars new native work through a fresh journal.
    with pytest.raises(ValueError, match="expired"):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), setup.deploy_context())
    assert len(setup.platform.actions) == 2


def test_checkpoint_resume_cannot_start_unproven_remote_source(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, plan, deploy = setup.prepare()
    original_intent = deploy.journal.intent

    def crash_before_run(key: str, inputs: dict[str, str]) -> bool:
        if key == "bundle-run":
            raise SystemExit("injected executor death before start")
        return original_intent(key, inputs)

    monkeypatch.setattr(deploy.journal, "intent", crash_before_run)
    with pytest.raises(SystemExit):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    monkeypatch.setattr(deploy.journal, "intent", original_intent)
    app = setup.platform.apps[str(plan.details["app_name"])]
    app["source_code_path"] = "/Workspace/externally-modified-source"
    with pytest.raises(NeedsAttention, match="provenance"):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [item[0] for item in setup.platform.actions] == ["deploy"]


def test_completed_journal_cannot_be_reused_for_another_sealed_plan(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    changed = seal_plan(plan.model_copy(update={"id": uuid4()}))
    with pytest.raises(ValueError, match="inputs"):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=changed), deploy)
    assert len(setup.platform.actions) == 2


# --------------------------------------------------------------------------- fault switches
def test_fault_fail_after_accept_writes_once_then_needs_attention(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.fail_after_accept = True
    with pytest.raises(TimeoutError):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.platform.actions == [("deploy", None, str(plan.details["root_path"]))]
    assert str(plan.details["app_name"]) in setup.platform.apps
    with pytest.raises(NeedsAttention):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(setup.platform.actions) == 1


def test_fault_crash_app_observes_failed(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.crash_app = True
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.adapter.observe(ObserveRequest(receipt=receipt), setup.read).state == "failed"


def test_fault_no_new_deployment_needs_attention(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.no_new_deployment = True
    with pytest.raises(NeedsAttention, match="deployment handle"):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [item[0] for item in setup.platform.actions] == ["deploy", "run"]


def test_fault_health_bad_fails_verify(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.health_bad = True
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.adapter.observe(ObserveRequest(receipt=receipt), setup.read).state == "succeeded"
    report = setup.adapter.verify(VerifyRequest(receipt=receipt), setup.read)
    assert not report.passed
    assert {c.name: c.status for c in report.checks} == {
        "native-runtime": "passed",
        "runtime-warehouse": "failed",
    }


def test_fault_hang_times_out_and_the_next_apply_needs_attention(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.platform.hang = True
    with pytest.raises(TimeoutError):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [item[0] for item in setup.platform.actions] == ["deploy"]
    assert str(plan.details["app_name"]) in setup.platform.apps
    with pytest.raises(NeedsAttention):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(setup.platform.actions) == 1


def test_fault_hang_release_unblocks_without_confirming(setup: Setup) -> None:
    setup.platform.hang_timeout = 60.0
    artifact, plan, deploy = setup.prepare()
    setup.platform.hang = True
    setup.platform.release()
    with pytest.raises(TimeoutError):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(setup.platform.actions) == 1


# --------------------------------------------------------------------------- journal
def test_in_memory_journal_records_intent_before_completion() -> None:
    tenant, operation = uuid4(), uuid4()
    journal = InMemoryJournal(tenant, operation)
    assert journal.intent("bundle-deploy", {"plan": "x"})
    assert journal.has_intent("bundle-deploy")
    assert journal.completed("bundle-deploy") is None
    with pytest.raises(NeedsAttention):
        journal.intent("bundle-deploy", {"plan": "x"})
    with pytest.raises(ValueError, match="inputs"):
        journal.intent("bundle-deploy", {"plan": "y"})
    journal.complete("bundle-deploy", {"native_id": "n"})
    assert journal.intent("bundle-deploy", {"plan": "x"}) is False
    journal.complete("bundle-deploy", {"native_id": "n"})
    with pytest.raises(ValueError, match="conflicting"):
        journal.complete("bundle-deploy", {"native_id": "other"})
