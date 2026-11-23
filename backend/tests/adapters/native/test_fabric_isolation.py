"""Tenant, target and workspace isolation of the Fabric native adapter.

Two tenants, A and B, each with its own workspace on one `FakeFabricWorkspace`. At this layer the
cross-client "404" rule is a `PermissionError` before any native request: A's context with B's
target, plan, artifact, receipt or journal is refused without the fake seeing a request, an
owned resource naming B's workspace is refused before any request to it, A's inventory never
lists B's items, and an item of A's bound name in B's workspace neither blocks A nor is touched.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from shifde.adapters.native.base import DeployContext, InMemoryJournal, ReadContext
from shifde.adapters.native.contracts import (
    ApplyRequest,
    InspectRequest,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    VerifyRequest,
)
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.fabric.source import TOOLCHAIN_DIGEST
from shifde.adapters.native.planning import seal_plan
from tests.adapters.native.fabric_support import LAKE, NOTEBOOK, FabricSetup, make_fabric_setup


@pytest.fixture
def fabric_fake() -> FakeFabricWorkspace:
    return FakeFabricWorkspace(hang_timeout=0.05)


@pytest.fixture
def fabric_a(fabric_fake: FakeFabricWorkspace) -> FabricSetup:
    return make_fabric_setup(fabric_fake)


@pytest.fixture
def fabric_b(fabric_fake: FakeFabricWorkspace) -> FabricSetup:
    return make_fabric_setup(fabric_fake)


def _requests_to(fake: FakeFabricWorkspace, workspace: str) -> list[str]:
    return [path for _, _, path, _ in fake.requests if workspace in path]


# --------------------------------------------------------------------------- cross-tenant
def test_fabric_context_refuses_another_tenants_target(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    with pytest.raises(PermissionError):
        ReadContext(tenant_id=fabric_a.read.tenant_id, target=fabric_b.target)
    with pytest.raises(PermissionError):
        replace(fabric_a.deploy_context(), target=fabric_b.target)
    assert fabric_fake.requests == []


def test_fabric_context_refuses_another_tenants_journal(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    operation = uuid4()
    with pytest.raises(PermissionError):
        DeployContext(
            tenant_id=fabric_a.read.tenant_id,
            target=fabric_a.target,
            operation_id=operation,
            artifacts=fabric_a.build.artifacts,
            journal=InMemoryJournal(fabric_b.read.tenant_id, operation),
            toolchain_digest=TOOLCHAIN_DIGEST,
        )
    assert fabric_fake.requests == []


def test_fabric_refuses_another_tenants_artifact_before_any_request(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact_a, plan_a, deploy_a = fabric_a.prepare()
    artifact_b = fabric_b.artifact()
    count = len(fabric_fake.requests)
    with pytest.raises(PermissionError):
        fabric_a.adapter.plan(PlanRequest(artifact=artifact_b), fabric_a.read)
    with pytest.raises(PermissionError):
        fabric_a.adapter.apply(ApplyRequest(artifact=artifact_b, plan=plan_a), deploy_a)
    assert len(fabric_fake.requests) == count
    assert not any(deploy_a.journal.has_intent(step.key) for step in plan_a.steps)


def test_fabric_refuses_another_tenants_plan_before_any_request(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact_a, _, deploy_a = fabric_a.prepare()
    _, plan_b, _ = fabric_b.prepare()
    count = len(fabric_fake.requests)
    with pytest.raises(PermissionError):
        fabric_a.adapter.apply(ApplyRequest(artifact=artifact_a, plan=plan_b), deploy_a)
    with pytest.raises(PermissionError):
        fabric_a.adapter.reconcile(ReconcileRequest(plan=plan_b), fabric_a.read)
    assert len(fabric_fake.requests) == count


def test_fabric_refuses_another_tenants_receipt_before_any_request(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact_b, receipt_b, _ = fabric_b.apply()
    count = len(fabric_fake.requests)
    with pytest.raises(PermissionError):
        fabric_a.adapter.observe(
            ObserveRequest(receipt=receipt_b, spec=artifact_b.spec), fabric_a.read
        )
    with pytest.raises(PermissionError):
        fabric_a.adapter.verify(
            VerifyRequest(receipt=receipt_b, spec=artifact_b.spec), fabric_a.read
        )
    assert len(fabric_fake.requests) == count


# --------------------------------------------------------------------------- cross-target
def test_fabric_refuses_a_plan_of_another_target_of_the_same_tenant(
    fabric_a: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact, plan, _ = fabric_a.prepare()
    other_target = fabric_a.target.model_copy(update={"id": uuid4()})
    deploy = replace(fabric_a.deploy_context(), target=other_target)
    count = len(fabric_fake.requests)
    with pytest.raises(ValueError, match="target binding changed"):
        fabric_a.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(fabric_fake.requests) == count


def test_fabric_refuses_a_receipt_of_another_target_of_the_same_tenant(
    fabric_a: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact, receipt, _ = fabric_a.apply()
    other_target = fabric_a.target.model_copy(update={"id": uuid4()})
    read = ReadContext(tenant_id=fabric_a.read.tenant_id, target=other_target)
    count = len(fabric_fake.requests)
    with pytest.raises(PermissionError):
        fabric_a.adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), read)
    assert len(fabric_fake.requests) == count


def test_fabric_refuses_a_resealed_plan_with_another_workspace(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact, plan, deploy = fabric_a.prepare()
    forged = seal_plan(
        plan.model_copy(update={"details": {**plan.details, "workspace": fabric_b.workspace}})
    )
    with pytest.raises(ValueError, match="bindings changed"):
        fabric_a.adapter.apply(ApplyRequest(artifact=artifact, plan=forged), deploy)
    assert fabric_fake.writes == [] and _requests_to(fabric_fake, fabric_b.workspace) == []


def test_fabric_refuses_an_owned_resource_of_another_target_or_application(
    fabric_a: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact, receipt, _ = fabric_a.apply()
    count = len(fabric_fake.requests)
    for update in ({"target_id": uuid4()}, {"application_id": uuid4()}):
        owned = tuple(r.model_copy(update=update) for r in receipt.resources)
        read = replace(fabric_a.read, owned_resources=owned)
        with pytest.raises(PermissionError):
            fabric_a.adapter.plan(PlanRequest(artifact=artifact), read)
    assert len(fabric_fake.requests) == count


# --------------------------------------------------------------------------- cross-workspace
def test_fabric_owned_resource_naming_another_workspace_is_refused_before_any_request(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    artifact_b, receipt_b, _ = fabric_b.apply()
    artifact_a = fabric_a.artifact(artifact_b.application_id)
    owned = tuple(
        r.model_copy(update={"target_id": fabric_a.target.id}) for r in receipt_b.resources
    )
    read = replace(fabric_a.read, owned_resources=owned)
    before_b = len(_requests_to(fabric_fake, fabric_b.workspace))
    count = len(fabric_fake.requests)
    with pytest.raises(PermissionError, match="another workspace"):
        fabric_a.adapter.plan(PlanRequest(artifact=artifact_a), read)
    with pytest.raises(PermissionError, match="another workspace"):
        fabric_a.adapter.inspect(
            InspectRequest(
                application_id=artifact_a.application_id, app_name="", spec=artifact_a.spec
            ),
            read,
        )
    assert len(fabric_fake.requests) == count
    assert len(_requests_to(fabric_fake, fabric_b.workspace)) == before_b


def test_fabric_inventory_never_lists_another_workspaces_items(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    fabric_b.apply()
    _, receipt_a, _ = fabric_a.apply()
    b_ids = set(fabric_fake.items[fabric_b.workspace])
    listed = fabric_fake.client_factory(fabric_a.target).items(fabric_a.workspace)
    assert {item["id"] for item in listed} == set(fabric_fake.items[fabric_a.workspace])
    assert not {item["id"] for item in listed} & b_ids
    assert not any(r.native_id.split(":")[-1] in b_ids for r in receipt_a.resources)


def test_fabric_same_name_in_another_workspace_neither_blocks_nor_is_touched(
    fabric_a: FabricSetup, fabric_b: FabricSetup, fabric_fake: FakeFabricWorkspace
) -> None:
    lake_b = fabric_fake.seed_foreign_item(fabric_b.workspace, "Lakehouse", LAKE)
    notebook_b = fabric_fake.seed_foreign_item(fabric_b.workspace, "Notebook", NOTEBOOK)
    before = {k: dict(v) for k, v in fabric_fake.items[fabric_b.workspace].items()}
    artifact, receipt, _ = fabric_a.apply()
    assert fabric_a.adapter.verify(
        VerifyRequest(receipt=receipt, spec=artifact.spec), fabric_a.read
    ).passed
    assert fabric_fake.items[fabric_b.workspace] == before
    assert _requests_to(fabric_fake, fabric_b.workspace) == []
    assert not {lake_b, notebook_b} & {r.native_id.split(":")[-1] for r in receipt.resources}
