"""Fixtures for the native adapter tests: the fake Databricks platform, no database."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    ReadContext,
)
from shifde.adapters.native.contracts import (
    BuildArtifact,
    BuildRequest,
    DatabricksApplicationSpec,
    DeploymentPlan,
    PlanRequest,
    ScaffoldRequest,
    Target,
)
from shifde.adapters.native.databricks.adapter import DatabricksAdapter
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform

WAREHOUSE = "warehouse-123"


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


@dataclass
class Setup:
    adapter: DatabricksAdapter
    platform: FakeDatabricksPlatform
    build: BuildContext
    read: ReadContext

    def new_adapter(self) -> DatabricksAdapter:
        return DatabricksAdapter(cli=self.platform, workspace_factory=self.platform.workspace)

    def deploy_context(
        self, journal: InMemoryJournal | None = None, toolchain_digest: str | None = None
    ) -> DeployContext:
        operation_id = journal.operation_id if journal else uuid4()
        return DeployContext(
            tenant_id=self.read.tenant_id,
            target=self.read.target,
            operation_id=operation_id,
            artifacts=self.build.artifacts,
            journal=journal or InMemoryJournal(self.read.tenant_id, operation_id),
            toolchain_digest=toolchain_digest or self.build.toolchain_digest,
        )

    def artifact(self, application_id: UUID | None = None) -> BuildArtifact:
        spec = DatabricksApplicationSpec()
        source = self.adapter.scaffold(ScaffoldRequest(spec=spec), self.build)
        return self.adapter.build(
            BuildRequest(spec=spec, source=source, application_id=application_id or uuid4()),
            self.build,
        )

    def prepare(
        self, application_id: UUID | None = None
    ) -> tuple[BuildArtifact, DeploymentPlan, DeployContext]:
        artifact = self.artifact(application_id)
        plan = self.adapter.plan(PlanRequest(artifact=artifact), self.read)
        return artifact, plan, self.deploy_context()


@pytest.fixture
def setup() -> Setup:
    tenant = uuid4()
    target = Target(
        tenant_id=tenant, host="https://workspace.example", bindings={"warehouse_id": WAREHOUSE}
    )
    platform = FakeDatabricksPlatform(warehouses={WAREHOUSE}, hang_timeout=0.05)
    build = BuildContext(
        tenant_id=tenant, artifacts=InMemoryArtifactStore(), toolchain_digest="a" * 64
    )
    adapter = DatabricksAdapter(cli=platform, workspace_factory=platform.workspace)
    return Setup(adapter, platform, build, ReadContext(tenant_id=tenant, target=target))
