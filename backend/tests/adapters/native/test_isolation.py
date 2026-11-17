"""Tenant, target and ownership isolation of the native Databricks adapter (lead review of PR #30).

Names, root paths and warehouses are derived from the artifact and the target, never taken from a
caller-supplied request or plan; every cross-tenant or cross-target input is refused with
`PermissionError` before any native write.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any  # Any: plan detail values
from uuid import uuid4

import pytest
import yaml

from shifde.adapters.native.base import (
    DeployContext,
    InMemoryJournal,
    ReadContext,
)
from shifde.adapters.native.contracts import (
    ApplyRequest,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    Target,
    VerifyRequest,
    digest,
)
from shifde.adapters.native.databricks.adapter import app_name, identity
from shifde.adapters.native.planning import seal_plan
from tests.adapters.native.conftest import PRINCIPAL, Setup


# --------------------------------------------------------------------------- derived names
def test_inspect_refuses_an_app_name_not_derived_for_the_application(setup: Setup) -> None:
    application_id = uuid4()
    own = app_name(application_id, setup.read.target.id)
    stranger = app_name(uuid4(), setup.read.target.id)
    setup.apps[stranger] = {"name": stranger, "id": "someone-elses-app"}
    with pytest.raises(PermissionError):
        setup.adapter.inspect(
            InspectRequest(application_id=application_id, app_name=stranger), setup.read
        )
    snapshot = setup.adapter.inspect(
        InspectRequest(application_id=application_id, app_name=own), setup.read
    )
    assert snapshot.resources == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("app_name", "shifde-someone-elses-app"),
        ("root_path", f"/Workspace/Users/{PRINCIPAL}/.shifde/elsewhere"),
        ("root_path", "/Workspace/Shared/.shifde/elsewhere"),
        ("warehouse_id", "warehouse-999"),
    ],
)
def test_a_forged_resealed_plan_is_refused_before_mutation(
    setup: Setup, key: str, value: Any
) -> None:
    artifact, plan, deploy = setup.prepare()
    forged = seal_plan(plan.model_copy(update={"details": {**plan.details, key: value}}))
    with pytest.raises(PermissionError):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=forged), deploy)
    assert setup.platform.actions == []
    assert not deploy.journal.has_intent("bundle-deploy")


def test_a_forged_plan_cannot_adopt_an_unowned_app_at_the_derived_name(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    name = str(plan.details["app_name"])
    setup.apps[name] = {"name": name, "id": "someone-elses-app"}
    forged = seal_plan(
        plan.model_copy(
            update={
                "snapshot": plan.snapshot.model_copy(
                    update={"fingerprint": digest(identity(setup.apps[name]))}
                )
            }
        )
    )
    with pytest.raises(PermissionError, match="unowned"):
        setup.adapter.apply(ApplyRequest(artifact=artifact, plan=forged), deploy)
    assert setup.platform.actions == []


# --------------------------------------------------------------------------- root path
def test_bundles_deploy_under_the_service_principal_home(setup: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    expected = (
        f"/Workspace/Users/{PRINCIPAL}/.shifde/{artifact.application_id}/{setup.read.target.id}"
    )
    assert plan.details["root_path"] == expected
    setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [action[2] for action in setup.platform.actions] == [expected, expected]


def test_plan_requires_a_valid_service_principal_binding(setup: Setup) -> None:
    artifact = setup.artifact()
    for bindings in (
        {"warehouse_id": "warehouse-123"},
        {"warehouse_id": "warehouse-123", "service_principal": "../Shared"},
    ):
        target = setup.read.target.model_copy(update={"bindings": bindings})
        read = ReadContext(tenant_id=setup.read.tenant_id, target=target)
        with pytest.raises(ValueError, match="service principal"):
            setup.adapter.plan(PlanRequest(artifact=artifact), read)


def test_the_fake_cli_refuses_to_write_outside_the_principal_home(
    setup: Setup, tmp_path: Path
) -> None:
    config = {
        "workspace": {"host": setup.read.target.host, "root_path": "/Workspace/Shared/.shifde/x"},
        "resources": {"apps": {"app": {"name": "shifde-x"}}},
    }
    (tmp_path / "databricks.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(RuntimeError):
        setup.platform.execute("deploy", tmp_path, target="dev", host=setup.read.target.host)
    assert setup.apps == {}


def test_the_fake_keeps_apps_per_workspace(setup: Setup, other: Setup) -> None:
    artifact, plan, deploy = setup.prepare()
    setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    name = str(plan.details["app_name"])
    assert name in setup.apps
    assert other.platform.workspace(other.read.target).get_app(name) is None
    assert other.apps == {}


# --------------------------------------------------------------------------- cross tenant
def _read_with_other_target(setup: Setup, other: Setup) -> None:
    ReadContext(tenant_id=setup.read.tenant_id, target=other.read.target)


def _deploy_with_other_journal(setup: Setup, other: Setup) -> None:
    journal = InMemoryJournal(other.read.tenant_id, uuid4())
    DeployContext(
        tenant_id=setup.read.tenant_id,
        target=setup.read.target,
        operation_id=journal.operation_id,
        artifacts=setup.build.artifacts,
        journal=journal,
        toolchain_digest=setup.build.toolchain_digest,
    )


def _store_read_across_tenants(setup: Setup, other: Setup) -> None:
    artifact = other.artifact()
    setup.build.artifacts.read(artifact.archive, setup.build.tenant_id)


def _store_read_with_other_tenant_id(setup: Setup, other: Setup) -> None:
    artifact = setup.artifact()
    setup.build.artifacts.read(artifact.archive, other.build.tenant_id)


def _plan_with_other_artifact(setup: Setup, other: Setup) -> None:
    setup.adapter.plan(PlanRequest(artifact=other.artifact()), setup.read)


def _apply_with_other_artifact(setup: Setup, other: Setup) -> None:
    _, plan, deploy = setup.prepare()
    setup.adapter.apply(ApplyRequest(artifact=other.artifact(), plan=plan), deploy)


def _apply_with_other_plan(setup: Setup, other: Setup) -> None:
    artifact, _, deploy = setup.prepare()
    _, plan, _ = other.prepare()
    setup.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def _receipt(owner: Setup) -> Any:
    artifact, plan, deploy = owner.prepare()
    return owner.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


def _observe_other_tenant_receipt(setup: Setup, other: Setup) -> None:
    setup.adapter.observe(ObserveRequest(receipt=_receipt(other)), setup.read)


def _verify_other_tenant_receipt(setup: Setup, other: Setup) -> None:
    setup.adapter.verify(VerifyRequest(receipt=_receipt(other)), setup.read)


def _other_target_read(setup: Setup) -> ReadContext:
    target = Target(
        tenant_id=setup.read.tenant_id,
        host=setup.read.target.host,
        bindings=setup.read.target.bindings,
    )
    return ReadContext(tenant_id=setup.read.tenant_id, target=target)


def _observe_other_target_receipt(setup: Setup, other: Setup) -> None:
    receipt = _receipt(setup)
    setup.adapter.observe(ObserveRequest(receipt=receipt), _other_target_read(setup))


def _verify_other_target_receipt(setup: Setup, other: Setup) -> None:
    receipt = _receipt(setup)
    setup.adapter.verify(VerifyRequest(receipt=receipt), _other_target_read(setup))


def _reconcile_other_tenant_plan(setup: Setup, other: Setup) -> None:
    _, plan, _ = other.prepare()
    setup.adapter.reconcile(ReconcileRequest(plan=plan), setup.read)


@pytest.mark.parametrize(
    "attempt",
    [
        _read_with_other_target,
        _deploy_with_other_journal,
        _store_read_across_tenants,
        _store_read_with_other_tenant_id,
        _plan_with_other_artifact,
        _apply_with_other_artifact,
        _apply_with_other_plan,
        _observe_other_tenant_receipt,
        _verify_other_tenant_receipt,
        _observe_other_target_receipt,
        _verify_other_target_receipt,
        _reconcile_other_tenant_plan,
    ],
    ids=lambda attempt: attempt.__name__.strip("_"),
)
def test_cross_tenant_and_cross_target_inputs_are_refused(
    setup: Setup, other: Setup, attempt: Callable[[Setup, Setup], None]
) -> None:
    with pytest.raises(PermissionError):
        attempt(setup, other)
