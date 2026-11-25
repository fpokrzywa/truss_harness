"""Fixtures for the native adapter tests: the fake Databricks and Snowflake platforms, no
database."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any  # Any: Databricks app JSON
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    ReadContext,
    create_archive,
)
from shifde.adapters.native.contracts import (
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    DatabricksApplicationSpec,
    DeploymentPlan,
    PlanRequest,
    ScaffoldRequest,
    SnowflakeApplicationSpec,
    Target,
)
from shifde.adapters.native.databricks.adapter import DatabricksAdapter
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.adapters.native.snowflake.adapter import SnowflakeNativeAdapter
from shifde.adapters.native.snowflake.clients import SnowflakeKeyPair
from shifde.adapters.native.snowflake.fake import Account, FakeSnowflakeNativePlatform
from shifde.appgen.render import render
from tests.appgen.rendered import (
    CORTEX_MODEL,
    SNOWFLAKE_DIGEST,
    SNOWFLAKE_MANIFEST,
    SNOWFLAKE_RESOLVED,
)

WAREHOUSE = "warehouse-123"
PRINCIPAL = "4a3c1f7e-0b5d-4c8e-9f21-6d7a8b9c0e1f"  # a service principal's application id


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


@dataclass
class Setup:
    adapter: DatabricksAdapter
    platform: FakeDatabricksPlatform
    build: BuildContext
    read: ReadContext

    @property
    def apps(self) -> dict[str, dict[str, Any]]:
        """The fake's apps in this setup's workspace."""
        return self.platform.apps.setdefault(self.read.target.host, {})

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


def make_setup(platform: FakeDatabricksPlatform, host: str) -> Setup:
    tenant = uuid4()
    target = Target(
        tenant_id=tenant,
        host=host,
        bindings={"warehouse_id": WAREHOUSE, "service_principal": PRINCIPAL},
    )
    build = BuildContext(
        tenant_id=tenant, artifacts=InMemoryArtifactStore(), toolchain_digest="a" * 64
    )
    adapter = DatabricksAdapter(cli=platform, workspace_factory=platform.workspace)
    return Setup(adapter, platform, build, ReadContext(tenant_id=tenant, target=target))


@pytest.fixture
def platform() -> FakeDatabricksPlatform:
    return FakeDatabricksPlatform(
        service_principal=PRINCIPAL, warehouses={WAREHOUSE}, hang_timeout=0.05
    )


@pytest.fixture
def setup(platform: FakeDatabricksPlatform) -> Setup:
    return make_setup(platform, "https://workspace.example")


@pytest.fixture
def other(platform: FakeDatabricksPlatform) -> Setup:
    """A second tenant, in another workspace on the same fake platform."""
    return make_setup(platform, "https://other-workspace.example")


# ------------------------------------------------------------------ Snowflake (SP6-2)
SF_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SF_PEM = SF_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
SF_HOST = "https://acct1.snowflakecomputing.com"
SF_BINDINGS = {"package": "SHIFDE_PKG", "application": "SHIFDE_APP", "warehouse": "APP_WH"}
SF_ROLE = "DEPLOY_ROLE"
SF_RELEASE = SNOWFLAKE_DIGEST  # the release digest the package's deep health answers
# The rendered sample package as the adapter reads it (the executor strips `release.json`).
SF_FILES = {
    path: data
    for path, data in render(SNOWFLAKE_MANIFEST, SF_RELEASE, SNOWFLAKE_RESOLVED).items()
    if path != "release.json"
}


def sf_credential(
    tenant_id: UUID,
    host: str = SF_HOST,
    pem: str = SF_PEM,
    account: str = "acct1",
    user: str = "deployer",
) -> SnowflakeKeyPair:
    return SnowflakeKeyPair(
        tenant_id=tenant_id,
        host=host,
        account=account,
        user=user,
        role=SF_ROLE,
        private_key_pem=pem,
    )


@dataclass
class SfSetup:
    adapter: SnowflakeNativeAdapter
    platform: FakeSnowflakeNativePlatform
    build: BuildContext
    read: ReadContext
    credential: SnowflakeKeyPair

    @property
    def account(self) -> Account:
        return self.platform.state(self.read.target.host)

    def artifact(
        self, application_id: UUID | None = None, files: dict[str, bytes] | None = None
    ) -> BuildArtifact:
        source = self.build.artifacts.put(self.build.tenant_id, create_archive(files or SF_FILES))
        return self.adapter.build(
            BuildRequest(
                spec=SnowflakeApplicationSpec(),
                source=source,
                application_id=application_id or uuid4(),
            ),
            self.build,
        )

    def with_owned(self, receipt: ApplyReceipt) -> SfSetup:
        """This setup with a fresh operation that owns `receipt`'s resources."""
        read = replace(self.read, operation_id=uuid4(), owned_resources=tuple(receipt.resources))
        return replace(self, read=read)

    def deploy_context(self, journal: InMemoryJournal | None = None) -> DeployContext:
        operation_id = journal.operation_id if journal else self.read.operation_id
        return DeployContext(
            tenant_id=self.read.tenant_id,
            target=self.read.target,
            owned_resources=self.read.owned_resources,
            operation_id=operation_id,
            artifacts=self.build.artifacts,
            journal=journal or InMemoryJournal(self.read.tenant_id, operation_id),
            toolchain_digest=self.build.toolchain_digest,
        )

    def prepare(
        self, application_id: UUID | None = None, files: dict[str, bytes] | None = None
    ) -> tuple[BuildArtifact, DeploymentPlan, DeployContext]:
        artifact = self.artifact(application_id, files)
        plan = self.adapter.plan(PlanRequest(artifact=artifact), self.read)
        return artifact, plan, self.deploy_context()

    def deploy(
        self, application_id: UUID | None = None, files: dict[str, bytes] | None = None
    ) -> tuple[BuildArtifact, DeploymentPlan, ApplyReceipt]:
        artifact, plan, deploy = self.prepare(application_id, files)
        return (
            artifact,
            plan,
            self.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy),
        )


def make_sf_setup(
    platform: FakeSnowflakeNativePlatform, host: str = SF_HOST, credential_host: str | None = None
) -> SfSetup:
    tenant = uuid4()
    target = Target(tenant_id=tenant, host=host, bindings=dict(SF_BINDINGS))
    account = host.removeprefix("https://").split(".")[0]  # the target host's account
    platform.data(host).cortex.setdefault(CORTEX_MODEL, [])  # the package's Cortex model
    credential = sf_credential(tenant, credential_host or host, account=account)
    build = BuildContext(
        tenant_id=tenant, artifacts=InMemoryArtifactStore(), toolchain_digest="b" * 64
    )
    adapter = SnowflakeNativeAdapter(
        gateway_factory=lambda t: platform.gateway(t, lambda: credential),
        copier_factory=platform.copier,
    )
    return SfSetup(
        adapter, platform, build, ReadContext(tenant_id=tenant, target=target), credential
    )


@pytest.fixture
def sf_platform() -> FakeSnowflakeNativePlatform:
    return FakeSnowflakeNativePlatform(
        environment="test", public_key=SF_KEY.public_key(), user="deployer", hang_timeout=0.05
    )


@pytest.fixture
def sf(sf_platform: FakeSnowflakeNativePlatform) -> SfSetup:
    return make_sf_setup(sf_platform)


@pytest.fixture
def sf_other(sf_platform: FakeSnowflakeNativePlatform) -> SfSetup:
    """A second tenant, on another account of the same fake platform."""
    return make_sf_setup(sf_platform, "https://acct2.snowflakecomputing.com")
