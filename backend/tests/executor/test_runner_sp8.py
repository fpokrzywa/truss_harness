"""The runner on a Fabric, Palantir or Docker target (SP8-1): the dispatch table has the five
platform kinds; the fake platform builds each new kind's adapter over one lazily built fake; a
leased job of each new kind runs its broker check (a mismatch is `credential_unavailable`), and a
Fabric or Docker one then fails closed (`execution_failed`) before any adapter or platform call and
any journal intent. Palantir jobs run (SP8-7): `test_runner_palantir.py`.

The gateway cannot yet give the Fabric and Docker targets' contexts (SP8-5 and SP8-9 map them), so
`Gateway` answers `context` with the mapped target and everything else through the in-process
gateway; the plan job rows are `tests.delivery.sp8`'s."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, get_args  # Any: JSON bodies and recorded calls
from uuid import UUID, uuid4

import pytest

from shifde.adapters.native.base import InMemoryArtifactStore
from shifde.adapters.native.contracts import (
    ContextDeployment,
    ContextPlan,
    CredentialGrant,
    JobContext,
    JobLease,
    JournalEntry,
    LeasedJob,
    Target,
    utcnow,
)
from shifde.adapters.native.docker.adapter import DockerComposeAdapter
from shifde.adapters.native.docker.fake import FakeContainerRuntime
from shifde.adapters.native.docker.runtime import DockerCliRuntime
from shifde.adapters.native.fabric.adapter import FabricAdapter
from shifde.adapters.native.fabric.clients import FabricClient
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.palantir.adapter import PalantirMarketplaceAdapter
from shifde.adapters.native.palantir.clients import FoundryCredentials, PalantirClient
from shifde.adapters.native.palantir.fake import FakeFoundryStack
from shifde.core.db import Database
from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker, FabricCredentials
from shifde.executor.runner import (
    DISPATCH,
    FakePlatform,
    JobFailure,
    LeaseState,
    RealPlatform,
    Runner,
    TargetKind,
)
from tests.connections.routes import as_user
from tests.delivery.routes import (
    PALANTIR_BINDINGS,
    SP8_KINDS,
    Flow,
    job_row,
    plan_row,
    register,
    sp8_address,
    sp8_body,
)
from tests.delivery.sp8 import Sp8Plan, sp8_plan
from tests.executor.support import (
    SP8_CLIENT_ID,
    SP8_ENTRA_TENANT,
    SP8_SECRET,
    credentials_file,
    sp8_credentials_file,
)

FAIL_CLOSED = ("fabric", "docker")  # until SP8-5 and SP8-9
GUID = "0a1b2c3d-0000-4000-8000-00000000abcd"
ADDRESS = {
    "fabric": f"https://api.fabric.microsoft.com/v1/workspaces/{GUID}",
    "palantir": "https://stack-1.palantirfoundry.com",
    "docker": f"docker://{GUID}",
}


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIFDE_ENV", "test")  # the Docker fake refuses anything else


def _mapped(kind: str, address: str, **ids: Any) -> Target:
    """The target of `address` as the kind's adapter takes it (string bindings; a Fabric host is
    the API origin)."""
    if kind == "fabric":
        host, workspace = address.split("/v1/workspaces/")
        bindings = {"workspace": workspace, "lakehouse": "SalesLake", "notebook": "SalesNotebook"}
        return Target(host=host, bindings=bindings, **ids)
    if kind == "palantir":
        strings = {**PALANTIR_BINDINGS, "allow_preview_apis": "false"}
        return Target(host=address, bindings=strings, **ids)
    return Target(host=address, bindings={"project": "sales-app", "port": "8501"}, **ids)


def _unit_target(kind: str) -> Target:
    return _mapped(kind, ADDRESS[kind], tenant_id=uuid4(), id=uuid4())


def _fake_platform(palantir_host: str = ADDRESS["palantir"]) -> FakePlatform:
    """A fake platform whose three fakes exist already, so any call to one is recorded."""
    return FakePlatform(
        fabric=FakeFabricWorkspace(),
        palantir=FakeFoundryStack(
            environment="test",
            host=palantir_host,
            frontend_origin=PALANTIR_BINDINGS["frontend_origin"],
            ontology_rid=PALANTIR_BINDINGS["ontology_rid"],
        ),
        docker=FakeContainerRuntime(GUID),
        environment="test",
    )


def _calls(platform: FakePlatform) -> list[Any]:
    """Every request or action the three fakes recorded."""
    assert platform.fabric and platform.palantir and platform.docker
    return [*platform.fabric.requests, *platform.palantir.requests, *platform.docker.actions]


def _fabric_ids() -> FabricCredentials:
    return FabricCredentials(SP8_ENTRA_TENANT, SP8_CLIENT_ID, SP8_SECRET)


def _foundry_ids(_host: str) -> FoundryCredentials:
    return FoundryCredentials(SP8_CLIENT_ID, SP8_SECRET)


# ------------------------------------------------------------------ dispatch and platforms
def test_the_dispatch_table_has_the_five_kinds() -> None:
    kinds = {"databricks", "snowflake", "fabric", "palantir", "docker"}
    assert set(DISPATCH) == set(get_args(TargetKind)) == kinds
    assert set(get_args(JobContext.model_fields["platform_kind"].annotation)) == kinds


def test_the_fake_platform_builds_the_fabric_adapter_over_one_fake() -> None:
    platform, target = FakePlatform(environment="test"), _unit_target("fabric")
    assert platform.fabric is None
    adapter = platform.fabric_adapter(target, _fabric_ids)
    fake = platform.fabric
    assert isinstance(adapter, FabricAdapter) and isinstance(fake, FakeFabricWorkspace)
    assert adapter.client_factory == fake.client_factory
    platform.fabric_adapter(target, _fabric_ids)
    assert platform.fabric is fake


def test_the_fake_platform_builds_the_palantir_adapter_over_one_fake() -> None:
    platform, target = FakePlatform(environment="test"), _unit_target("palantir")
    assert platform.palantir is None
    adapter = platform.palantir_adapter(target, _foundry_ids)
    fake = platform.palantir
    assert isinstance(adapter, PalantirMarketplaceAdapter)
    assert isinstance(fake, FakeFoundryStack) and fake.host == target.host
    client = adapter.client_factory(target)
    assert client.platform == "fake" and client.transport is fake.transport
    platform.palantir_adapter(target, _foundry_ids)
    assert platform.palantir is fake


def test_the_fake_platform_builds_the_docker_adapter_over_one_fake() -> None:
    platform, target = FakePlatform(environment="test"), _unit_target("docker")
    assert platform.docker is None
    adapter = platform.docker_adapter(target, InMemoryArtifactStore())
    fake = platform.docker
    assert isinstance(adapter, DockerComposeAdapter) and isinstance(fake, FakeContainerRuntime)
    assert adapter.runtime is fake and fake.daemon_id() == GUID
    platform.docker_adapter(target, InMemoryArtifactStore())
    assert platform.docker is fake


def test_the_docker_fake_refuses_outside_development_and_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIFDE_ENV", "production")
    with pytest.raises(RuntimeError):
        FakePlatform().docker_adapter(_unit_target("docker"), InMemoryArtifactStore())


def test_the_real_platform_builds_each_new_adapter_over_its_client() -> None:
    """Built only: nothing here sends a request or runs a command."""
    real = RealPlatform()
    target = _unit_target("fabric")
    fabric = real.fabric_adapter(target, _fabric_ids)
    assert isinstance(fabric, FabricAdapter)
    assert type(fabric.client_factory(target)) is FabricClient
    target = _unit_target("palantir")
    palantir = real.palantir_adapter(target, _foundry_ids)
    assert isinstance(palantir, PalantirMarketplaceAdapter)
    assert type(palantir.client_factory(target)) is PalantirClient
    docker = real.docker_adapter(_unit_target("docker"), InMemoryArtifactStore())
    assert isinstance(docker, DockerComposeAdapter)
    assert type(docker.runtime) is DockerCliRuntime


# ------------------------------------------------------------------ leased jobs fail closed
def _context_plan(tenant: UUID, plan_id: UUID, artifact: UUID, digest: str) -> ContextPlan:
    return ContextPlan(
        id=plan_id,
        tenant_id=tenant,
        application_key="sales-app",
        display_name="Sales app",
        artifact_id=artifact,
        artifact_digest=digest,
        target_revision=1,
        status="requested",
        steps=None,
        snapshot=None,
        adapter_version=None,
        toolchain_digest=None,
        digest=None,
        expires_at=None,
    )


class Gateway(GatewayClient):
    """The in-process gateway of one plan job, its context mapped; checkpoints are recorded."""

    def __init__(self, flow: Flow, kind: str, target: Target, plan: ContextPlan) -> None:
        super().__init__("https://testserver", flow.token, http=flow.client)
        self.kind, self.target, self.plan = kind, target, plan
        self.checkpoints: list[Any] = []

    def context(self, lease: JobLease) -> JobContext:
        return JobContext.model_validate(
            {
                "job": lease.job,
                "target": self.target,
                "platform_kind": self.kind,
                "plan": self.plan,
                "deployment": None,
                "owned_resources": [],
            }
        )

    def checkpoint(self, lease: JobLease, **step: Any) -> JournalEntry:
        self.checkpoints.append(step)
        return super().checkpoint(lease, **step)


@dataclass
class Leased:
    """A registered target of a new kind, its queued plan job and the gateway that leases it."""

    target: dict[str, Any]
    plan: Sp8Plan
    gateway: Gateway
    platform: FakePlatform

    def run(self, creds: Path) -> None:
        me = self.gateway.whoami()
        broker = CredentialBroker.from_file(creds)
        runner = Runner(self.gateway, broker, me.executor_id, self.platform, tenant_id=me.tenant_id)
        assert runner.run_once()


def _leased(flow: Flow, app_db: Database, owner_db: Database, kind: str) -> Leased:
    h = as_user(flow.client, flow.requester)
    body = sp8_body(kind, flow.workspace, sp8_address(kind))
    made = register(flow.client, h, flow.slug, body)
    flow.client.cookies.clear()
    plan = sp8_plan(app_db, flow.tenant, made["id"])
    artifact = plan_row(owner_db, plan.id)["artifact_id"]
    target = _mapped(
        kind,
        made["address"],
        tenant_id=flow.tenant,
        id=UUID(made["id"]),
        revision=made["revision"],
    )
    context_plan = _context_plan(flow.tenant, plan.id, artifact, plan.artifact_digest)
    platform = _fake_platform(made["address"] if kind == "palantir" else ADDRESS["palantir"])
    return Leased(made, plan, Gateway(flow, kind, target, context_plan), platform)


@pytest.mark.parametrize("kind", FAIL_CLOSED)
def test_a_leased_job_of_a_new_kind_fails_closed(
    flow: Flow,
    tmp_path: Path,
    app_db: Database,
    owner_db: Database,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    leased = _leased(flow, app_db, owner_db, kind)
    leased.run(sp8_credentials_file(tmp_path, kind, leased.target["address"], tenant=flow.tenant))
    failed = plan_row(owner_db, leased.plan.id)
    assert failed["status"] == "plan_failed"
    assert failed["status_reason"] == "plan job failed: execution_failed"
    assert _calls(leased.platform) == [] and leased.gateway.checkpoints == []
    result = job_row(owner_db, leased.plan.job_id)["result"]
    assert result == {"status": "failed", "error_code": "execution_failed"}
    for leak in (SP8_SECRET, str(tmp_path)):
        assert leak not in caplog.text and leak not in str(result)


@pytest.mark.parametrize("mismatch", ["tenant", "platform"])
@pytest.mark.parametrize("kind", SP8_KINDS)
def test_an_entry_of_another_tenant_or_platform_is_credential_unavailable(
    flow: Flow, tmp_path: Path, app_db: Database, owner_db: Database, kind: str, mismatch: str
) -> None:
    leased = _leased(flow, app_db, owner_db, kind)
    address, ref = leased.target["address"], leased.target["credential_ref"]
    if mismatch == "tenant":
        creds = sp8_credentials_file(tmp_path, kind, address, tenant=uuid4())
    elif kind == "docker":
        creds = credentials_file(tmp_path, address, tenant=flow.tenant, ref=ref)
    else:
        creds = sp8_credentials_file(tmp_path, "docker", address, tenant=flow.tenant, ref=ref)
    leased.run(creds)
    failed = plan_row(owner_db, leased.plan.id)
    assert failed["status"] == "plan_failed"
    assert failed["status_reason"] == "plan job failed: credential_unavailable"
    assert _calls(leased.platform) == [] and leased.gateway.checkpoints == []


class Stub:
    """A gateway of one job, in memory: its context and grant; any other call fails the test."""

    def __init__(self, ctx: JobContext, grant: CredentialGrant) -> None:
        self.ctx, self.grant = ctx, grant

    def context(self, _lease: JobLease) -> JobContext:
        return self.ctx

    def credential_grant(self, _lease: JobLease) -> CredentialGrant:
        return self.grant

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the gateway was called: {name}")


class NoPlatform(FakePlatform):
    """A platform that must build no adapter."""

    def __getattribute__(self, name: str) -> Any:
        if name.endswith("adapter"):
            raise AssertionError(f"the platform was called: {name}")
        return super().__getattribute__(name)


@pytest.mark.parametrize("job_kind", ["plan", "apply", "observe", "verify", "reconcile"])
@pytest.mark.parametrize("kind", FAIL_CLOSED)
def test_every_job_kind_fails_closed_before_any_adapter_or_checkpoint(
    tmp_path: Path, kind: str, job_kind: str
) -> None:
    executor, plan_id, deployment_id = uuid4(), uuid4(), uuid4()
    target = _unit_target(kind)
    job = LeasedJob.model_validate(
        {
            "id": uuid4(),
            "kind": job_kind,
            "plan_id": plan_id,
            "deployment_id": deployment_id,
            "target_id": target.id,
            "deadline": None,
            "attempts": 1,
            "max_attempts": 1,
        }
    )
    ctx = JobContext.model_validate(
        {
            "job": job,
            "target": target,
            "platform_kind": kind,
            "plan": _context_plan(target.tenant_id, plan_id, uuid4(), "e" * 64),
            "deployment": ContextDeployment(
                id=deployment_id, tenant_id=target.tenant_id, status="applying", receipt=None
            ),
            "owned_resources": [],
        }
    )
    until = utcnow() + timedelta(seconds=45)
    grant = CredentialGrant(
        job_id=job.id,
        executor_id=executor,
        target_id=target.id,
        credential_ref=f"{kind}-dev",
        audience=ADDRESS[kind],
        expires_at=until,
    )
    creds = sp8_credentials_file(tmp_path, kind, ADDRESS[kind], tenant=target.tenant_id)
    stub: Any = Stub(ctx, grant)  # Any: stands in for a GatewayClient
    broker = CredentialBroker.from_file(creds)
    runner = Runner(stub, broker, executor, NoPlatform(), tenant_id=target.tenant_id)
    lease = JobLease(job=job, lease_id=uuid4(), fence=1, lease_until=until, request_id=uuid4())
    with pytest.raises(JobFailure) as raised:
        runner._execute(LeaseState(lease=lease, lease_until=until))
    assert raised.value.code == "execution_failed"
