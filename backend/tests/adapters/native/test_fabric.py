"""The FDE SDK Fabric adapter tests (1fd902b `tests/adapters/test_fabric.py`), synchronous, over
`FakeFabricWorkspace`, plus the SETH behaviour of `fabric.solution/v1` (template, step keys, fault
switches, foreign items, the two verify checks).

Dropped from the SDK, with the reason:
- the `fabric.iq/v1` parametrization of `test_fabric_scaffold_and_build_never_execute_source`,
  `test_iq_adapter_enforces_feature_gate_even_when_client_factory_omits_iq` and
  `test_iq_tmdl_uses_explicit_environment_bindings`: `fabric.iq/v1` and the Ontology item type
  are out of scope;
- the credential-profile half of `test_required_execution_cannot_pass_on_metadata_alone`
  (`allow_notebook_execution`): SETH mints Entra client-credentials tokens and has no profile.

Adapted: the SDK's `fail_poll` (operation polls answer 503) is the `lro_failed` switch, and
`lose_response` is `fail_after_accept="lakehouse-metadata-submit"`; every apply runs the one
verification job, so `upgraded` keeps `verification_notebook` and edits the second notebook cell,
never the generated first one.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any  # Any: notebook JSON
from uuid import uuid4

import httpx
import pytest

import shifde.adapters.native.fabric as fabric_package
from shifde.adapters.native.base import NeedsAttention, create_archive, read_archive
from shifde.adapters.native.contracts import (
    ApplyRequest,
    BuildRequest,
    DatabricksApplicationSpec,
    FabricItem,
    FabricSolutionSpec,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ScaffoldRequest,
    SnowflakeApplicationSpec,
    ValidateRequest,
    VerifyRequest,
    digest,
)
from shifde.adapters.native.databricks.adapter import DatabricksAdapter
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.adapters.native.fabric.adapter import FabricAdapter, ForeignItem, validate_receipt
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.fabric.source import exit_cell, fabric_step, template_spec
from tests.adapters.native.conftest import SfSetup
from tests.adapters.native.fabric_support import (
    LAKE,
    NOTEBOOK,
    RELEASE,
    FabricSetup,
    make_fabric_setup,
)

CREATE_STEPS = [
    "lakehouse-metadata-submit",
    "lakehouse-metadata-complete",
    "notebook-metadata-submit",
    "notebook-metadata-complete",
    "fabric-verification-submit",
    "fabric-finalize",
]
UPDATE_STEPS = [
    "lakehouse-metadata-submit",
    "lakehouse-metadata-complete",
    "notebook-metadata-submit",
    "notebook-metadata-complete",
    "notebook-definition-submit",
    "notebook-definition-complete",
    "fabric-verification-submit",
    "fabric-finalize",
]


@pytest.fixture
def fabric_fake() -> FakeFabricWorkspace:
    return FakeFabricWorkspace(hang_timeout=0.05)


@pytest.fixture
def fabric(fabric_fake: FakeFabricWorkspace) -> FabricSetup:
    return make_fabric_setup(fabric_fake)


def _notebook(fake: FakeFabricWorkspace, workspace: str) -> dict[str, Any]:
    return next(i for i in fake.items[workspace].values() if i["type"] == "Notebook")


def _stored_notebook(fake: FakeFabricWorkspace, identity: str) -> dict[str, Any]:
    payload = fake.definitions[identity]["parts"][0]["payload"]
    notebook: dict[str, Any] = json.loads(base64.b64decode(payload))
    return notebook


def _checks(report: Any) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


# --------------------------------------------------------------------------- package
def test_fabric_maturity_is_experimental_and_not_live_validated() -> None:
    assert fabric_package.MATURITY == "experimental"
    assert fabric_package.LIVE_VALIDATED is False


def test_fabric_package_names_only_the_two_allowed_hosts() -> None:
    package = Path(fabric_package.__file__).parent
    for path in package.rglob("*.py"):
        hosts = set(re.findall(r"https://([a-z0-9.-]+)", path.read_text(encoding="utf-8")))
        allowed = {"api.fabric.microsoft.com", "login.microsoftonline.com"}
        if path.name == "fake.py":
            allowed.add("untrusted.example")
        assert hosts <= allowed, path.name


# --------------------------------------------------------------------------- source
def test_fabric_template_spec_has_the_lakehouse_and_the_verification_notebook() -> None:
    spec = template_spec(RELEASE)
    assert [(i.key, i.item_type, i.definition_path, i.name_binding) for i in spec.items] == [
        ("lakehouse", "Lakehouse", "lake.json", "lakehouse_name"),
        ("notebook", "Notebook", "main.ipynb", "notebook_name"),
    ]
    notebook = spec.items[1]
    assert notebook.depends_on == ["lakehouse"] and notebook.lakehouse == "lakehouse"
    assert spec.verification_notebook == "notebook" and spec.release_digest == RELEASE


def test_fabric_scaffold_and_build_never_execute_source(fabric: FabricSetup) -> None:
    artifact = fabric.artifact()
    files = read_archive(fabric.build.artifacts.read(artifact.archive, fabric.build.tenant_id))
    templates = resource_files("shifde.adapters.native.fabric").joinpath("templates")
    assert set(files) == {"lake.json", "main.ipynb"}
    assert files["lake.json"] == templates.joinpath("lake.json").read_bytes()
    notebook = json.loads(files["main.ipynb"])
    template = json.loads(templates.joinpath("main.ipynb").read_bytes())
    assert notebook["cells"][0]["source"] == [f'mssparkutils.notebook.exit("{RELEASE}")']
    assert {**notebook, "cells": notebook["cells"][1:]} == template
    assert artifact.archive.digest == artifact.source_digest
    assert fabric.fake.requests == []


def _rewrite_notebook(files: dict[str, bytes], change: Any) -> None:
    notebook = json.loads(files["main.ipynb"])
    change(notebook)
    files["main.ipynb"] = json.dumps(notebook).encode()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda f: _rewrite_notebook(
                f, lambda n: n["cells"].__setitem__(0, exit_cell(digest("another")))
            ),
            id="fabric-wrong-digest",
        ),
        pytest.param(
            lambda f: _rewrite_notebook(f, lambda n: n["cells"].pop(0)), id="fabric-missing-cell"
        ),
        pytest.param(lambda f: f.__setitem__(".env", b"X=1\n"), id="fabric-hidden-file"),
        pytest.param(
            lambda f: f.__setitem__("notes/.secret.json", b"{}"), id="fabric-hidden-segment"
        ),
        pytest.param(lambda f: f.__setitem__("extra.py", b"print(1)\n"), id="fabric-extra-file"),
        pytest.param(
            lambda f: f.__setitem__("lake.json", b'{"description":"x","hooks":[]}'),
            id="fabric-lakehouse-shape",
        ),
        pytest.param(lambda f: f.pop("lake.json"), id="fabric-missing-item"),
    ],
)
def test_fabric_validation_refuses_a_changed_source(fabric: FabricSetup, edit: Any) -> None:
    spec = template_spec(RELEASE)
    source = fabric.adapter.scaffold(ScaffoldRequest(spec=spec), fabric.build)
    files = read_archive(fabric.build.artifacts.read(source, fabric.build.tenant_id))
    edit(files)
    changed = fabric.build.artifacts.put(fabric.build.tenant_id, create_archive(files))
    report = fabric.adapter.validate(ValidateRequest(spec=spec, source=changed), fabric.build)
    assert not report.passed and report.diagnostics[0].code == "INVALID_FABRIC_SOURCE"
    with pytest.raises(ValueError):
        fabric.adapter.build(
            BuildRequest(application_id=uuid4(), spec=spec, source=changed), fabric.build
        )


def _item(key: str, item_type: str, path: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "item_type": item_type, "definition_path": path, **extra}


@pytest.mark.parametrize(
    ("items", "verification"),
    [
        ([_item("a", "Notebook", "a.ipynb"), _item("a", "Notebook", "b.ipynb")], "a"),
        ([_item("a", "Notebook", "a.ipynb", depends_on=["missing"])], "a"),
        (
            [
                _item("a", "Notebook", "a.ipynb", depends_on=["b"]),
                _item("b", "Notebook", "b.ipynb", depends_on=["a"]),
            ],
            "a",
        ),
        ([_item("a", "Notebook", "nb"), _item("b", "Lakehouse", "nb/lake.json")], "a"),
        ([_item("a", "Notebook", ".")], "a"),
        (
            [
                _item("a", "Notebook", "a.ipynb", name_binding="same"),
                _item("b", "Lakehouse", "b.json", name_binding="same"),
            ],
            "a",
        ),
        (
            [_item("a", "Notebook", "a.ipynb", lakehouse="b"), _item("b", "Lakehouse", "b.json")],
            "a",
        ),
        ([_item("a", "Notebook", "a.ipynb"), _item("b", "Lakehouse", "b.json")], "b"),
        ([_item("a", "Notebook", "a.ipynb")], "missing"),
        ([_item("a", "Ontology", "ontology")], "a"),
    ],
)
def test_fabric_spec_refuses_an_invalid_item_graph(items: Any, verification: str) -> None:
    with pytest.raises(ValueError):
        FabricSolutionSpec.model_validate(
            {"items": items, "release_digest": RELEASE, "verification_notebook": verification}
        )


def test_fabric_spec_refuses_the_legacy_bindings_and_requires_the_release_digest() -> None:
    base = template_spec(RELEASE).model_dump(mode="json")
    with pytest.raises(ValueError):
        FabricSolutionSpec.model_validate({**base, "bindings": {"lakehouse": "x"}})
    with pytest.raises(ValueError):
        FabricSolutionSpec.model_validate({k: v for k, v in base.items() if k != "release_digest"})
    assert fabric_step("x" * 70, "metadata-submit").endswith("-metadata-submit")
    assert len(fabric_step("x" * 70, "metadata-submit")) <= 64


def test_fabric_and_databricks_adapters_refuse_each_others_spec(fabric: FabricSetup) -> None:
    databricks = DatabricksApplicationSpec()
    with pytest.raises(ValueError):
        fabric.adapter.scaffold(ScaffoldRequest(spec=databricks), fabric.build)
    with pytest.raises(ValueError):
        fabric.adapter.inspect(
            InspectRequest(application_id=uuid4(), app_name="x", spec=databricks), fabric.read
        )
    platform = FakeDatabricksPlatform(service_principal="sp")
    other = DatabricksAdapter(cli=platform, workspace_factory=platform.workspace)
    with pytest.raises(ValueError):
        other.scaffold(ScaffoldRequest(spec=template_spec(RELEASE)), fabric.build)
    source = fabric.adapter.scaffold(ScaffoldRequest(spec=template_spec(RELEASE)), fabric.build)
    with pytest.raises(ValueError):
        other.validate(ValidateRequest(spec=template_spec(RELEASE), source=source), fabric.build)
    artifact = fabric.artifact()
    with pytest.raises(ValueError):
        other.plan(PlanRequest(artifact=artifact), fabric.read)
    assert fabric.fake.requests == [] and platform.actions == []


def test_fabric_and_snowflake_adapters_refuse_each_others_spec(
    fabric: FabricSetup, sf: SfSetup
) -> None:
    snowflake = SnowflakeApplicationSpec()
    with pytest.raises(ValueError, match="another kind"):
        fabric.adapter.scaffold(ScaffoldRequest(spec=snowflake), fabric.build)
    source = fabric.adapter.scaffold(ScaffoldRequest(spec=template_spec(RELEASE)), fabric.build)
    with pytest.raises(ValueError, match="another kind"):
        fabric.adapter.validate(ValidateRequest(spec=snowflake, source=source), fabric.build)
    with pytest.raises(ValueError, match="snowflake.native-app/v1"):
        sf.adapter.validate(
            ValidateRequest(spec=template_spec(RELEASE), source=source), fabric.build
        )
    with pytest.raises(ValueError, match="snowflake.native-app/v1"):
        sf.adapter.plan(PlanRequest(artifact=fabric.artifact()), fabric.read)
    with pytest.raises(ValueError, match="another kind"):
        fabric.adapter.plan(PlanRequest(artifact=sf.artifact()), sf.read)
    assert fabric.fake.requests == [] and sf.platform.log == []


def test_fabric_spec_round_trips_through_the_kind_discriminator(fabric: FabricSetup) -> None:
    artifact = fabric.artifact()
    again = type(artifact).model_validate(artifact.model_dump(mode="json"))
    assert isinstance(again.spec, FabricSolutionSpec) and again == artifact
    request = ScaffoldRequest.model_validate({"spec": {"kind": "databricks.app/v1"}})
    assert isinstance(request.spec, DatabricksApplicationSpec)


# --------------------------------------------------------------------------- apply and verify
def test_fabric_template_apply_journals_the_steps_and_verifies_over_the_fake(
    fabric: FabricSetup,
) -> None:
    artifact, plan, ctx = fabric.prepare()
    assert [step.key for step in plan.steps] == CREATE_STEPS
    assert plan.details == {
        "workspace": fabric.workspace,
        "names": {"lakehouse": LAKE, "notebook": NOTEBOOK},
        "kind": "fabric.solution/v1",
        "release_digest": RELEASE,
    }
    receipt = fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert list(ctx.journal.steps) == CREATE_STEPS
    assert all(entry.state == "complete" for entry in ctx.journal.steps.values())
    assert receipt.operation_handles["platform"] == "fake"
    assert receipt.operation_handles["release_digest"] == RELEASE
    report = fabric.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert _checks(report) == {"native-items": "passed", "runtime-health": "passed"}
    assert report.passed
    observed = fabric.adapter.observe(
        ObserveRequest(receipt=receipt, spec=artifact.spec), fabric.read
    )
    assert observed.state == "succeeded"
    job = fabric.fake.jobs[receipt.operation_handles["job_id"]]
    assert job["status"] == "Completed" and job["exitValue"] == RELEASE


def test_fabric_create_201_and_202_preserves_dependency_order_and_rebinds_notebook(
    fabric: FabricSetup,
) -> None:
    artifact, receipt, ctx = fabric.apply()
    creates = [
        r
        for r in fabric.fake.requests
        if r[0] == "POST" and r[2].split("/")[-1] in {"lakehouses", "notebooks"}
    ]
    assert [r[2].split("/")[-1] for r in creates] == ["lakehouses", "notebooks"]
    notebook = json.loads(base64.b64decode(creates[1][3]["definition"]["parts"][0]["payload"]))
    lake_id = next(r.native_id.split(":")[-1] for r in receipt.resources if r.key == "lakehouse")
    assert notebook["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == lake_id
    assert ctx.journal.completed("notebook-metadata-submit")["handles"]["operation_id"]
    assert not any("untrusted" in path for _, _, path, _ in fabric.fake.requests)
    assert {host for _, host, _, _ in fabric.fake.requests} == {"api.fabric.microsoft.com"}


def test_fabric_update_plan_steps_and_retained_native_ids(fabric: FabricSetup) -> None:
    artifact, first, _ = fabric.apply()
    artifact, plan, ctx = fabric.upgraded(first, artifact.application_id)
    assert [step.key for step in plan.steps] == UPDATE_STEPS
    second = fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert list(ctx.journal.steps) == UPDATE_STEPS
    assert first.resources == second.resources
    assert len(fabric.fake.items[fabric.workspace]) == 2
    assert fabric.adapter.verify(VerifyRequest(receipt=second, spec=artifact.spec), ctx).passed
    stored = _stored_notebook(fabric.fake, _notebook(fabric.fake, fabric.workspace)["id"])
    assert stored["cells"][1]["source"] == ["assert spark.range(2).count() == 2\n"]


def test_fabric_required_execution_cannot_pass_on_metadata_alone(fabric: FabricSetup) -> None:
    artifact, receipt, _ = fabric.apply()
    job = fabric.fake.jobs[receipt.operation_handles["job_id"]]
    job["status"] = "InProgress"
    request = VerifyRequest(receipt=receipt, spec=artifact.spec)
    report = fabric.adapter.verify(request, fabric.read)
    assert _checks(report) == {"native-items": "passed", "runtime-health": "failed"}
    assert not report.passed
    observe = ObserveRequest(receipt=receipt, spec=artifact.spec)
    assert fabric.adapter.observe(observe, fabric.read).state == "running"
    job["status"] = "Completed"
    assert fabric.adapter.verify(request, fabric.read).passed


def test_fabric_missing_capacity_blocks_before_any_native_mutation(
    fabric_fake: FakeFabricWorkspace,
) -> None:
    setup = make_fabric_setup(fabric_fake, capacity=False)
    with pytest.raises(ValueError, match="capacity"):
        setup.adapter.plan(PlanRequest(artifact=setup.artifact()), setup.read)
    assert fabric_fake.writes == [] and fabric_fake.items[setup.workspace] == {}


def test_fabric_notebook_parameter_metadata_drift_is_not_reported_healthy(
    fabric: FabricSetup,
) -> None:
    artifact, receipt, _ = fabric.apply()
    identity = _notebook(fabric.fake, fabric.workspace)["id"]
    part = fabric.fake.definitions[identity]["parts"][0]
    doc = json.loads(base64.b64decode(part["payload"]))
    doc["cells"][1]["metadata"] = {"tags": ["parameters"]}
    part["payload"] = base64.b64encode(json.dumps(doc).encode()).decode()
    report = fabric.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert _checks(report) == {"native-items": "failed", "runtime-health": "passed"}
    observe = ObserveRequest(receipt=receipt, spec=artifact.spec)
    assert fabric.adapter.observe(observe, fabric.read).state == "unknown"


@pytest.mark.parametrize("field", ["displayName", "description"])
def test_fabric_drifted_item_fails_native_items(fabric: FabricSetup, field: str) -> None:
    artifact, receipt, _ = fabric.apply()
    lake = next(i for i in fabric.fake.items[fabric.workspace].values() if i["type"] == "Lakehouse")
    lake[field] = "Renamed"
    report = fabric.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert _checks(report)["native-items"] == "failed" and not report.passed


def test_fabric_deleted_item_fails_native_items(fabric: FabricSetup) -> None:
    artifact, receipt, _ = fabric.apply()
    lake = next(i for i in fabric.fake.items[fabric.workspace].values() if i["type"] == "Lakehouse")
    del fabric.fake.items[fabric.workspace][lake["id"]]
    report = fabric.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert _checks(report) == {"native-items": "failed", "runtime-health": "passed"}


@pytest.mark.parametrize("exit_value", [digest("another release"), None])
def test_fabric_wrong_or_missing_exit_value_fails_runtime_health(
    fabric: FabricSetup, exit_value: str | None
) -> None:
    artifact, receipt, _ = fabric.apply()
    job = fabric.fake.jobs[receipt.operation_handles["job_id"]]
    if exit_value is None:
        del job["exitValue"]
    else:
        job["exitValue"] = exit_value
    report = fabric.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert _checks(report) == {"native-items": "passed", "runtime-health": "failed"}
    observe = ObserveRequest(receipt=receipt, spec=artifact.spec)
    assert fabric.adapter.observe(observe, fabric.read).state == "failed"


def test_fabric_fake_fails_a_job_whose_first_cell_is_not_the_exit_cell(
    fabric: FabricSetup,
) -> None:
    artifact, receipt, _ = fabric.apply()
    identity = _notebook(fabric.fake, fabric.workspace)["id"]
    part = fabric.fake.definitions[identity]["parts"][0]
    doc = json.loads(base64.b64decode(part["payload"]))
    doc["cells"][0]["source"] = ["print('no exit')"]
    part["payload"] = base64.b64encode(json.dumps(doc).encode()).decode()
    assert fabric.fake._run(identity) == {"status": "Failed"}


def test_fabric_reconcile_is_always_ambiguous(fabric: FabricSetup) -> None:
    artifact, plan, _ = fabric.prepare()
    result = fabric.adapter.reconcile(ReconcileRequest(plan=plan), fabric.read)
    assert result.state == "ambiguous"


def test_fabric_refuses_removing_a_managed_item(fabric: FabricSetup) -> None:
    from dataclasses import replace

    artifact, receipt, _ = fabric.apply()
    extra = receipt.resources[0].model_copy(update={"key": "retired"})
    read = replace(fabric.read, owned_resources=(*receipt.resources, extra))
    count = len(fabric.fake.requests)
    with pytest.raises(ValueError, match="removing"):
        fabric.adapter.plan(PlanRequest(artifact=artifact), read)
    assert len(fabric.fake.requests) == count


def test_fabric_replaying_a_completed_apply_needs_attention(fabric: FabricSetup) -> None:
    artifact, plan, ctx = fabric.prepare()
    fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    writes = len(fabric.fake.writes)
    with pytest.raises(NeedsAttention):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert len(fabric.fake.writes) == writes


# --------------------------------------------------------------------------- fault switches
@pytest.mark.parametrize(
    "step", ["lakehouse-metadata-submit", "notebook-metadata-submit", "fabric-verification-submit"]
)
def test_fabric_fail_after_accept_on_create_is_never_replayed(
    fabric: FabricSetup, step: str
) -> None:
    artifact, plan, ctx = fabric.prepare()
    fabric.fake.fail_after_accept = step
    with pytest.raises(httpx.ReadTimeout):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    writes = list(fabric.fake.writes)
    assert ctx.journal.has_intent(step) and ctx.journal.completed(step) is None
    fabric.fake.fail_after_accept = None
    with pytest.raises(NeedsAttention):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert fabric.fake.writes == writes
    assert len({(m, p) for m, _, p, _ in writes if not p.endswith("/instances")}) == len(
        [w for w in writes if not w[2].endswith("/instances")]
    )


def test_fabric_fail_after_accept_on_a_definition_update_is_never_replayed(
    fabric: FabricSetup,
) -> None:
    artifact, first, _ = fabric.apply()
    artifact, plan, ctx = fabric.upgraded(first, artifact.application_id)
    fabric.fake.fail_after_accept = "notebook-definition-submit"
    with pytest.raises(httpx.ReadTimeout):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    writes = list(fabric.fake.writes)
    assert sum(1 for w in writes if w[2].endswith("/updateDefinition")) == 1
    fabric.fake.fail_after_accept = None
    with pytest.raises(NeedsAttention):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert fabric.fake.writes == writes


def test_fabric_lro_failed_is_durable_before_poll_failure(fabric: FabricSetup) -> None:
    artifact, plan, ctx = fabric.prepare()
    fabric.fake.lro_failed = True
    with pytest.raises(RuntimeError, match="did not succeed"):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert ctx.journal.completed("lakehouse-metadata-complete")["resources"]
    assert ctx.journal.completed("notebook-metadata-submit")["handles"]["operation_id"]
    assert ctx.journal.completed("notebook-metadata-complete") is None
    writes = list(fabric.fake.writes)
    assert len(writes) == 2
    fabric.fake.lro_failed = False
    with pytest.raises(NeedsAttention):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert fabric.fake.writes == writes


def test_fabric_foreign_item_refuses_inspect_plan_and_apply_before_any_write(
    fabric: FabricSetup,
) -> None:
    artifact, plan, ctx = fabric.prepare()
    fabric.fake.seed_foreign_item(fabric.workspace, "Notebook", NOTEBOOK)
    with pytest.raises(ForeignItem) as refused:
        fabric.adapter.plan(PlanRequest(artifact=artifact), fabric.read)
    assert refused.value.code == "foreign_item" and isinstance(refused.value, PermissionError)
    with pytest.raises(ForeignItem):
        fabric.adapter.inspect(
            InspectRequest(application_id=artifact.application_id, app_name="", spec=artifact.spec),
            fabric.read,
        )
    with pytest.raises(ForeignItem):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert fabric.fake.writes == []
    assert not any(ctx.journal.has_intent(step.key) for step in plan.steps)


def test_fabric_foreign_item_next_to_an_owned_item_refuses_the_update(fabric: FabricSetup) -> None:
    artifact, first, _ = fabric.apply()
    fabric.fake.seed_foreign_item(fabric.workspace, "Lakehouse", NOTEBOOK)
    writes = len(fabric.fake.writes)
    with pytest.raises(ForeignItem):
        fabric.upgraded(first, artifact.application_id)
    assert len(fabric.fake.writes) == writes


def test_fabric_job_failed_fails_runtime_health(fabric: FabricSetup) -> None:
    fabric.fake.job_failed = True
    artifact, receipt, _ = fabric.apply()
    assert sum(1 for w in fabric.fake.writes if w[2].endswith("/instances")) == 1
    report = fabric.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert _checks(report) == {"native-items": "passed", "runtime-health": "failed"}
    observe = ObserveRequest(receipt=receipt, spec=artifact.spec)
    assert fabric.adapter.observe(observe, fabric.read).state == "failed"


def test_fabric_hang_times_out_and_is_never_replayed(fabric: FabricSetup) -> None:
    artifact, plan, ctx = fabric.prepare()
    fabric.fake.hang = True
    with pytest.raises(httpx.ReadTimeout):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    writes = list(fabric.fake.writes)
    assert len(writes) == 1 and len(fabric.fake.items[fabric.workspace]) == 1
    fabric.fake.hang = False
    with pytest.raises(NeedsAttention):
        fabric.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert fabric.fake.writes == writes


def test_fabric_hang_release_unblocks_the_write(fabric_fake: FakeFabricWorkspace) -> None:
    fabric_fake.hang_timeout = 30
    setup = make_fabric_setup(fabric_fake)
    artifact, plan, ctx = setup.prepare()
    fabric_fake.hang = True
    errors: list[BaseException] = []

    def run() -> None:
        try:
            setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
        except BaseException as error:  # noqa: BLE001 - collected for the assertion
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    fabric_fake.release()
    worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], httpx.ReadTimeout)
    assert len(fabric_fake.writes) == 1


def test_fabric_spec_item_types_are_lakehouse_and_notebook_only() -> None:
    with pytest.raises(ValueError):
        FabricItem(key="o", item_type="Ontology", definition_path="o")


# --------------------------------------------------------------------------- fake evidence
@pytest.mark.parametrize("env", ["development", "test"])
def test_fabric_fake_receipt_is_accepted_in_development_and_test(
    fabric: FabricSetup, env: str
) -> None:
    artifact, receipt, _ = fabric.apply()
    spec = template_spec(RELEASE)
    validate_receipt(receipt, spec, fabric.target, env=env)
    adapter = FabricAdapter(client_factory=fabric.fake.client_factory, env=env)
    assert adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read).passed


@pytest.mark.parametrize("env", ["production", "staging"])
def test_fabric_fake_receipt_is_refused_outside_development_and_test(
    fabric: FabricSetup, env: str
) -> None:
    artifact, receipt, _ = fabric.apply()
    spec = template_spec(RELEASE)
    with pytest.raises(ValueError, match="mismatch"):
        validate_receipt(receipt, spec, fabric.target, env=env)
    real = receipt.model_copy(
        update={"operation_handles": {**receipt.operation_handles, "platform": "fabric"}}
    )
    validate_receipt(real, spec, fabric.target, env=env)
    adapter = FabricAdapter(client_factory=fabric.fake.client_factory, env=env)
    count = len(fabric.fake.requests)
    with pytest.raises(ValueError, match="mismatch"):
        adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    with pytest.raises(ValueError, match="mismatch"):
        adapter.observe(ObserveRequest(receipt=receipt, spec=artifact.spec), fabric.read)
    assert len(fabric.fake.requests) == count


def test_fabric_fake_apply_is_refused_outside_development_and_test(
    fabric_fake: FakeFabricWorkspace,
) -> None:
    setup = make_fabric_setup(fabric_fake)
    setup.adapter = FabricAdapter(client_factory=fabric_fake.client_factory, env="production")
    artifact, plan, ctx = setup.prepare()
    with pytest.raises(ValueError, match="mismatch"):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert ctx.journal.completed("fabric-finalize") is None


def test_fabric_adapter_environment_comes_from_shifde_env(
    fabric_fake: FakeFabricWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SHIFDE_ENV", raising=False)
    assert FabricAdapter(client_factory=fabric_fake.client_factory).env == "production"
    monkeypatch.setenv("SHIFDE_ENV", "development")
    assert FabricAdapter(client_factory=fabric_fake.client_factory).env == "development"
