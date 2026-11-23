"""Helpers for the Fabric native adapter tests: one tenant's setup over a `FakeFabricWorkspace`."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any  # Any: notebook JSON
from uuid import UUID, uuid4

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    ReadContext,
    create_archive,
    read_archive,
)
from shifde.adapters.native.contracts import (
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    DeploymentPlan,
    PlanRequest,
    ScaffoldRequest,
    Target,
    digest,
)
from shifde.adapters.native.fabric.adapter import FabricAdapter
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.fabric.source import ORIGIN, TOOLCHAIN_DIGEST, template_spec

RELEASE = digest({"release": "fabric-demo", "version": 1})
LAKE, NOTEBOOK = "DemoLake", "DemoNotebook"


@dataclass
class FabricSetup:
    adapter: FabricAdapter
    fake: FakeFabricWorkspace
    build: BuildContext
    read: ReadContext
    workspace: str

    @property
    def target(self) -> Target:
        return self.read.target

    def deploy_context(
        self, read: ReadContext | None = None, journal: InMemoryJournal | None = None
    ) -> DeployContext:
        read = read or self.read
        operation = journal.operation_id if journal else uuid4()
        return DeployContext(
            tenant_id=read.tenant_id,
            target=read.target,
            owned_resources=read.owned_resources,
            operation_id=operation,
            artifacts=self.build.artifacts,
            journal=journal or InMemoryJournal(read.tenant_id, operation),
            toolchain_digest=TOOLCHAIN_DIGEST,
        )

    def artifact(
        self,
        application_id: UUID | None = None,
        *,
        edit: Callable[[dict[str, bytes]], None] | None = None,
    ) -> BuildArtifact:
        spec = template_spec(RELEASE)
        source = self.adapter.scaffold(ScaffoldRequest(spec=spec), self.build)
        if edit:
            files = read_archive(self.build.artifacts.read(source, self.build.tenant_id))
            edit(files)
            source = self.build.artifacts.put(self.build.tenant_id, create_archive(files))
        return self.adapter.build(
            BuildRequest(application_id=application_id or uuid4(), spec=spec, source=source),
            self.build,
        )

    def prepare(self) -> tuple[BuildArtifact, DeploymentPlan, DeployContext]:
        artifact = self.artifact()
        plan = self.adapter.plan(PlanRequest(artifact=artifact), self.read)
        return artifact, plan, self.deploy_context()

    def apply(self) -> tuple[BuildArtifact, ApplyReceipt, DeployContext]:
        artifact, plan, ctx = self.prepare()
        return artifact, self.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx), ctx

    def upgraded(
        self, receipt: ApplyReceipt, application_id: UUID
    ) -> tuple[BuildArtifact, DeploymentPlan, DeployContext]:
        """A second release of the same application: the second notebook cell is edited (never
        the generated first cell), planned against the first receipt's owned resources."""

        def edit(files: dict[str, bytes]) -> None:
            notebook: dict[str, Any] = json.loads(files["main.ipynb"])
            notebook["cells"][1]["source"] = ["assert spark.range(2).count() == 2\n"]
            files["main.ipynb"] = json.dumps(notebook).encode()

        artifact = self.artifact(application_id, edit=edit)
        read = replace(self.read, owned_resources=tuple(receipt.resources), operation_id=uuid4())
        plan = self.adapter.plan(PlanRequest(artifact=artifact), read)
        return artifact, plan, self.deploy_context(read)


def make_fabric_setup(fake: FakeFabricWorkspace, *, capacity: bool = True) -> FabricSetup:
    tenant = uuid4()
    workspace = fake.add_workspace(capacity=capacity)
    target = Target(
        tenant_id=tenant,
        host=ORIGIN,
        bindings={"workspace": workspace, "lakehouse_name": LAKE, "notebook_name": NOTEBOOK},
    )
    build = BuildContext(
        tenant_id=tenant, artifacts=InMemoryArtifactStore(), toolchain_digest=TOOLCHAIN_DIGEST
    )
    adapter = FabricAdapter(client_factory=fake.client_factory, env="test")
    return FabricSetup(
        adapter, fake, build, ReadContext(tenant_id=tenant, target=target), workspace
    )
