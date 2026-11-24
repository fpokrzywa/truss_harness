"""The FDE SDK Databricks adapter tests (1fd902b `tests/adapters/test_databricks.py`), synchronous,
over the fake platform. Tests 10, 12, 13 and 14 used the dropped `LocalDeliveryService`; they run
at the adapter level here with the same intent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any  # Any: bundle YAML
from uuid import uuid4

import pytest
import yaml

import shifde.adapters.native.planning as planning
from shifde.adapters.native.base import (
    DeployContext,
    InMemoryJournal,
    NeedsAttention,
    create_archive,
    read_archive,
)
from shifde.adapters.native.contracts import (
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    DatabricksApplicationSpec,
    DeploymentPlan,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ScaffoldRequest,
    ValidateRequest,
    VerifyRequest,
    utcnow,
)
from shifde.adapters.native.databricks.adapter import app_name
from shifde.adapters.native.databricks.source import (
    GENERATED_APP_YAML,
    deployment_bundle,
    scaffold_files,
    validate_source,
)
from shifde.adapters.native.planning import seal_plan
from shifde.appgen.render import render
from tests.adapters.native.conftest import WAREHOUSE, Setup
from tests.appgen.rendered import ENDPOINT, SAMPLE_DIGEST, SAMPLE_MANIFEST, SAMPLE_RESOLVED


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
    assert receipt.resources[0].native_id == setup.apps[name]["id"]
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
    setup.apps[name] = {"name": name, "id": "someone-elses-app"}
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
    app = setup.apps[receipt.operation_handles["app_name"]]
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
    app = setup.apps[str(plan.details["app_name"])]
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
    assert str(plan.details["app_name"]) in setup.apps
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
    assert str(plan.details["app_name"]) in setup.apps
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


# --------------------------------------------------------------------------- generated app (SP5-6)
SPEC = DatabricksApplicationSpec()
MANAGED_YML = scaffold_files(SPEC)["databricks.yml"]


def _generated_files() -> dict[str, bytes]:
    """The rendered sample app as the executor hands it to the adapter (no `release.json`)."""
    rendered = render(SAMPLE_MANIFEST, SAMPLE_DIGEST, SAMPLE_RESOLVED)
    return {name: data for name, data in rendered.items() if name != "release.json"}


def _generated(
    setup: Setup, endpoint: str | None = ENDPOINT, contents: dict[str, bytes] | None = None
) -> tuple[Setup, BuildArtifact, DeploymentPlan, DeployContext]:
    """`setup` on a target bound to `endpoint` (no binding if None), with a planned build of the
    rendered sample app (or of `contents`)."""
    target = setup.read.target
    bindings = {**target.bindings, **({"serving_endpoint": endpoint} if endpoint else {})}
    read = replace(setup.read, target=target.model_copy(update={"bindings": bindings}))
    bound = replace(setup, read=read)
    archive = create_archive(contents or _generated_files())
    source = bound.build.artifacts.put(bound.build.tenant_id, archive)
    artifact = bound.adapter.build(
        BuildRequest(spec=SPEC, source=source, application_id=uuid4()), bound.build
    )
    plan = bound.adapter.plan(PlanRequest(artifact=artifact), bound.read)
    return bound, artifact, plan, bound.deploy_context()


def _deployed_bundles(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The `databricks.yml` of every CLI deploy, as the fake receives it."""
    seen: list[dict[str, Any]] = []
    execute = setup.platform.execute

    def capture(action: str, directory: Path, **options: Any) -> dict[str, Any]:  # Any: CLI
        if action == "deploy":
            seen.append(yaml.safe_load((directory / "databricks.yml").read_text(encoding="utf-8")))
        return execute(action, directory, **options)

    monkeypatch.setattr(setup.platform, "execute", capture)
    return seen


def test_generated_app_yaml_is_the_appgen_template() -> None:
    template = files("shifde.appgen").joinpath("templates").joinpath("app.yaml.txt")
    assert template.read_bytes() == GENERATED_APP_YAML


def test_validate_source_accepts_a_rendered_app_and_the_template() -> None:
    assert validate_source(_generated_files(), SPEC) is True
    assert validate_source(scaffold_files(SPEC), SPEC) is False


@pytest.mark.parametrize(
    "change",
    [
        {"app/.env": b"TOKEN=x"},
        {"app/static/.hidden/x.js": b""},
        {"extra/tool.py": b"print(1)"},
        {"app/app.yaml": GENERATED_APP_YAML + b"  - name: EXTRA\n    value: x\n"},
        {"databricks.yml": MANAGED_YML + b"scripts:\n  steal: echo secret\n"},
        {"databricks.yml": MANAGED_YML + b"include:\n  - extra/*.yml\n"},
    ],
    ids=["hidden-file", "hidden-dir", "outside-app", "app-yaml", "hook", "include"],
)
def test_validate_source_refuses_a_changed_generated_source(change: dict[str, bytes]) -> None:
    with pytest.raises(ValueError):
        validate_source({**_generated_files(), **change}, SPEC)


def test_a_generated_source_needs_every_generated_file() -> None:
    contents = _generated_files()
    del contents["app/config/screens.json"]
    with pytest.raises(ValueError, match="missing"):
        validate_source(contents, SPEC)


def test_deployment_bundle_adds_the_serving_endpoint_only_when_named() -> None:
    options = {"host": "https://w.example", "app_name": "a", "root_path": "/r", "warehouse_id": "w"}
    template = deployment_bundle(SPEC, **options)["resources"]["apps"]["app"]["resources"]
    assert [r["name"] for r in template] == ["sql-warehouse"]
    generated = deployment_bundle(SPEC, **options, serving_endpoint="ep")
    assert generated["resources"]["apps"]["app"]["resources"][1:] == [
        {"name": "serving-endpoint", "serving_endpoint": {"name": "ep", "permission": "CAN_QUERY"}}
    ]


def test_apply_binds_the_serving_endpoint_for_a_generated_source_only(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _deployed_bundles(setup, monkeypatch)
    bound, artifact, plan, deploy = _generated(setup)
    bound.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    # the template, on a target that names an endpoint too
    template, t_artifact, t_plan, t_deploy = _generated(setup, contents=scaffold_files(SPEC))
    template.adapter.apply(ApplyRequest(artifact=t_artifact, plan=t_plan), t_deploy)
    warehouse = {
        "name": "sql-warehouse",
        "sql_warehouse": {"id": WAREHOUSE, "permission": "CAN_USE"},
    }
    endpoint = {
        "name": "serving-endpoint",
        "serving_endpoint": {"name": ENDPOINT, "permission": "CAN_QUERY"},
    }
    assert [b["resources"]["apps"]["app"]["resources"] for b in seen] == [
        [warehouse, endpoint],
        [warehouse],
    ]


def test_a_generated_source_without_the_binding_fails_before_the_cli(setup: Setup) -> None:
    bound, artifact, plan, deploy = _generated(setup, endpoint=None)
    with pytest.raises(ValueError, match="serving_endpoint"):
        bound.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert setup.platform.actions == []


def _verify_generated(
    setup: Setup, release_digest: str | None = SAMPLE_DIGEST
) -> dict[str, tuple[str, str]]:
    """name -> (status, evidence) of verify on the deployed sample app."""
    bound, artifact, plan, deploy = _generated(setup)
    receipt = bound.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    report = bound.adapter.verify(
        VerifyRequest(receipt=receipt, release_digest=release_digest), bound.read
    )
    return {c.name: (c.status, c.evidence) for c in report.checks}


def test_verify_passes_a_generated_app_with_all_three_checks(setup: Setup) -> None:
    assert _verify_generated(setup) == {
        "native-runtime": ("passed", ""),
        "runtime-warehouse": ("passed", ""),
        "app-deep-health": ("passed", ""),
    }


def test_verify_without_a_release_digest_is_the_template_report(setup: Setup) -> None:
    assert set(_verify_generated(setup, None)) == {"native-runtime", "runtime-warehouse"}


def test_fault_deep_health_bad_fails_app_deep_health(setup: Setup) -> None:
    setup.platform.deep_health_bad = True
    checks = _verify_generated(setup)
    first = sorted({t.lower() for r in SAMPLE_MANIFEST["data_reads"] for t in r["tables"]})[0]
    assert checks["app-deep-health"] == (
        "failed",
        f"table:{first}: the app cannot read {first}",
    )
    assert checks["runtime-warehouse"] == ("passed", "")


def test_verify_fails_a_release_digest_other_than_the_plan_s(setup: Setup) -> None:
    assert _verify_generated(setup, "9" * 64)["app-deep-health"] == (
        "failed",
        "the release digest differs from the plan's",
    )


def test_verify_skips_app_deep_health_when_the_runtime_failed(setup: Setup) -> None:
    setup.platform.crash_app = True
    checks = _verify_generated(setup)
    assert checks["native-runtime"][0] == "failed"
    assert checks["app-deep-health"] == ("skipped", "")


def test_a_deployment_without_a_recorded_release_fails_deep_health(setup: Setup) -> None:
    """The template app answers no deep health (the fake's 404)."""
    artifact, plan, deploy = setup.prepare()
    receipt = setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    report = setup.adapter.verify(
        VerifyRequest(receipt=receipt, release_digest=SAMPLE_DIGEST), setup.read
    )
    checks = {c.name: (c.status, c.evidence) for c in report.checks}
    assert checks["app-deep-health"] == ("failed", "deep health request failed")
