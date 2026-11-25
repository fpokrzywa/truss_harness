"""The runner on a Fabric target (SP8-5): plan, apply, observe, verify and reconcile on the Fabric
fake, the checks that come before any artifact read, adapter, platform request or checkpoint (the
credential entry's Entra tenant and workspace, the sealed artifact, the stored receipt), the fake's
switch, the toolchain digests and the per-job Entra token provider of the real platform.

`Unit` runs one target's jobs through `Runner` with an in-memory gateway; the rest goes through the
routes and the in-process gateway. No network is used anywhere."""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import logging
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any  # Any: JSON bodies, recorded calls
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from shifde.adapters.native.base import create_archive, read_archive
from shifde.adapters.native.contracts import (
    ApplyReceipt,
    ContextDeployment,
    ContextPlan,
    CredentialGrant,
    JobContext,
    JobLease,
    JournalEntry,
    LeasedJob,
    ObserveRequest,
    OwnedResource,
    Target,
    VerifyRequest,
    utcnow,
)
from shifde.adapters.native.fabric.adapter import TERMINAL, VERIFICATION, FabricAdapter
from shifde.adapters.native.fabric.clients import FabricClient
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.fabric.source import ORIGIN, TOOLCHAIN_DIGEST, exit_cell, template_spec
from shifde.core.db import Database
from shifde.core.httplogs import HTTP_CLIENT_LOGGERS, quiet_http_client_logs
from shifde.delivery.plans import fabric_artifact
from shifde.executor import main
from shifde.executor import runner as runner_module
from shifde.executor.credentials import CredentialBroker, CredentialUnavailable
from shifde.executor.runner import (
    FakePlatform,
    LeaseState,
    RealPlatform,
    Runner,
    toolchain_digest,
)
from tests.connections.routes import as_user
from tests.delivery.fabric import (
    CHECKS,
    LAKEHOUSE,
    NOTEBOOK,
    approved_deployment,
    fabric_platform,
    fabric_release,
    fabric_target,
    flow_runner,
    run_due,
    workspace_of,
)
from tests.delivery.routes import (
    FABRIC_BINDINGS,
    Flow,
    deployment_jobs,
    deployment_row,
    make_due,
    owner_sql,
    plan_row,
    targets,
)
from tests.executor.support import (
    SP8_CLIENT_ID,
    SP8_ENTRA_TENANT,
    SP8_SECRET,
    ask_plan,
    sp8_credentials_file,
    sp8_entry,
)

KINDS = ("plan", "apply", "observe", "verify", "reconcile")
WORKSPACE = "0a1b2c3d-0000-4000-8000-00000000abcd"
OTHER = "0a1b2c3d-0000-4000-8000-00000000ffff"
API = "https://api.fabric.microsoft.com/v1/workspaces/"
RELEASE = "a" * 64
FIRST_STEPS = [
    "lakehouse-metadata-submit",
    "lakehouse-metadata-complete",
    "notebook-metadata-submit",
    "notebook-metadata-complete",
    "fabric-verification-submit",
    "fabric-finalize",
]
UPDATE_STEPS = [
    *FIRST_STEPS[:4],
    "notebook-definition-submit",
    "notebook-definition-complete",
    *FIRST_STEPS[4:],
]


# ------------------------------------------------------------------ one target's jobs, in memory
class MemoryGateway:
    """The gateway calls of `Unit`'s job, in memory, each counted."""

    def __init__(self, unit: Unit, job: LeasedJob, ctx: JobContext, grant: CredentialGrant) -> None:
        self.unit, self.job, self.ctx, self.grant = unit, job, ctx, grant
        self.artifact_reads, self.grants = 0, 0
        self.checkpoints: list[str] = []
        self.value: dict[str, Any] | None = None
        self.error_code: str | None = None

    def context(self, _lease: JobLease) -> JobContext:
        return self.ctx

    def credential_grant(self, _lease: JobLease) -> CredentialGrant:
        self.grants += 1
        return self.grant

    def artifact(self, _lease: JobLease) -> bytes:
        self.artifact_reads += 1
        return self.unit.content

    def checkpoint(self, _lease: JobLease, **step: Any) -> JournalEntry:
        self.checkpoints.append(f"{step['step_key']}:{step['state']}")
        return JournalEntry(
            step_key=step["step_key"],
            input_digest=step["input_digest"],
            state=step["state"],
            receipt=step["receipt"],
        )

    def result(
        self, _lease: JobLease, *, value: dict[str, Any] | None = None, error_code: Any = None
    ) -> None:
        self.value, self.error_code = value, error_code


@dataclass
class Unit:
    """A Fabric target, its plan and its deployment; `run(kind)` runs one job of them."""

    directory: Path
    platform: Any  # Any: a Platform
    tenant: UUID = field(default_factory=uuid4)
    target_id: UUID = field(default_factory=uuid4)
    executor: UUID = field(default_factory=uuid4)
    workspace: str = WORKSPACE
    release_digest: str = RELEASE
    plan: dict[str, Any] = field(default_factory=dict)  # sealed fields, or changes of them
    receipt: ApplyReceipt | None = None
    owned: list[OwnedResource] = field(default_factory=list)
    audience: str | None = None
    entry: dict[str, str] = field(default_factory=dict)  # changes of the credentials entry
    content: bytes = field(init=False)

    def __post_init__(self) -> None:
        manifest = {"target_kind": "fabric.solution/v1"}
        self.content, _ = fabric_artifact(manifest, self.release_digest)

    @property
    def target(self) -> Target:
        return Target(
            tenant_id=self.tenant,
            id=self.target_id,
            host=ORIGIN,
            bindings={**FABRIC_BINDINGS, "workspace": self.workspace},
        )

    def context(self, job: LeasedJob) -> JobContext:
        values: dict[str, Any] = {
            "id": job.plan_id,
            "tenant_id": self.tenant,
            "application_key": "sales-app",
            "display_name": "Sales app",
            "artifact_id": uuid4(),
            "artifact_digest": hashlib.sha256(self.content).hexdigest(),
            "target_revision": 1,
            "status": "approved",
            "steps": None,
            "snapshot": None,
            "adapter_version": None,
            "toolchain_digest": None,
            "digest": None,
            "expires_at": None,
            "release_digest": self.release_digest,
        }
        return JobContext(
            job=job,
            target=self.target,
            platform_kind="fabric",
            plan=ContextPlan(**{**values, **self.plan}),
            deployment=ContextDeployment(
                id=job.deployment_id or uuid4(),
                tenant_id=self.tenant,
                status="observing",
                receipt=self.receipt,
            ),
            owned_resources=self.owned,
        )

    def broker(self) -> CredentialBroker:
        self.directory.mkdir(exist_ok=True)
        entry = {
            **sp8_entry(self.directory, "fabric", API + self.workspace, tenant=self.tenant),
            **self.entry,
        }
        path = self.directory / "fabric.json"
        path.write_text(json.dumps({"fabric-dev": entry}), encoding="utf-8")
        path.chmod(0o600)
        return CredentialBroker.from_file(path)

    def lease(self, kind: str) -> JobLease:
        until = utcnow() + timedelta(seconds=45)
        job = LeasedJob.model_validate(
            {
                "id": uuid4(),
                "kind": kind,
                "plan_id": uuid4(),
                "deployment_id": uuid4(),
                "target_id": self.target_id,
                "deadline": None,
                "attempts": 1,
                "max_attempts": 1,
            }
        )
        return JobLease(job=job, lease_id=uuid4(), fence=1, lease_until=until, request_id=uuid4())

    def grant(self, lease: JobLease, expires_at: datetime | None = None) -> CredentialGrant:
        return CredentialGrant(
            job_id=lease.job.id,
            executor_id=self.executor,
            target_id=self.target_id,
            credential_ref="fabric-dev",
            audience=self.audience or API + self.workspace,
            expires_at=expires_at or lease.lease_until,
        )

    def runner(self, lease: JobLease) -> tuple[Runner, Any]:
        gateway: Any = MemoryGateway(self, lease.job, self.context(lease.job), self.grant(lease))
        made = Runner(gateway, self.broker(), self.executor, self.platform, tenant_id=self.tenant)
        return made, gateway

    def run(self, kind: str) -> MemoryGateway:
        lease = self.lease(kind)
        made, gateway = self.runner(lease)
        made._run(lease)
        done: MemoryGateway = gateway
        return done

    def seal(self) -> list[str]:
        """The plan job's result becomes the sealed plan; its step keys."""
        done = self.run("plan")
        assert done.value is not None, done.error_code
        self.plan = {
            **done.value,
            "digest": "d" * 64,
            "expires_at": utcnow() + timedelta(minutes=15),
        }
        return [s["key"] for s in done.value["steps"]]

    def deploy(self) -> Unit:
        """Plan and apply; the receipt and the owned resources are stored."""
        self.seal()
        done = self.run("apply")
        assert done.value is not None, done.error_code
        self.receipt = ApplyReceipt.model_validate(done.value)
        self.owned = [
            OwnedResource(resource_type=r.resource_type, native_id=r.native_id)
            for r in self.receipt.resources
        ]
        return self


@pytest.fixture
def fake() -> FakeFabricWorkspace:
    return FakeFabricWorkspace()


@pytest.fixture
def unit(tmp_path: Path, fake: FakeFabricWorkspace) -> Unit:
    return Unit(tmp_path, fabric_platform(fake))


class NoFabric(FakePlatform):
    """A platform that must build no Fabric adapter."""

    def fabric_adapter(self, target: Target, credential_provider: Any) -> FabricAdapter:
        raise AssertionError("the platform was asked for an adapter")


def test_a_unit_runs_every_job_kind(unit: Unit, fake: FakeFabricWorkspace) -> None:
    assert unit.seal() == FIRST_STEPS and fake.writes == []
    unit.deploy()
    assert unit.run("observe").value is not None
    verified = unit.run("verify").value
    assert verified is not None
    assert {c["name"]: c["status"] for c in verified["checks"]} == dict.fromkeys(CHECKS, "passed")
    seen = len(fake.requests)
    reconciled = unit.run("reconcile").value
    assert reconciled is not None and reconciled["state"] == "ambiguous"
    assert len(fake.requests) == seen  # reconcile makes no platform request


# ------------------------------------------------------------------ the credential entry (C)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mismatch", ["entra_tenant", "entry_workspace", "audience", "tenant"])
def test_an_entry_of_another_entra_tenant_or_workspace_is_credential_unavailable(
    tmp_path: Path, fake: FakeFabricWorkspace, kind: str, mismatch: str
) -> None:
    """Before any artifact read, adapter, platform request or checkpoint, for every job kind."""
    unit = Unit(tmp_path, NoFabric(fabric=fake, environment="test", fabric_enabled=True))
    if mismatch == "entra_tenant":
        unit.entry = {"entra_tenant_id": OTHER}
    elif mismatch == "entry_workspace":
        unit.entry = {"host": API + OTHER}
    elif mismatch == "audience":  # the entry and the grant agree, the target is another workspace
        unit.entry, unit.audience = {"host": API + OTHER}, API + OTHER
    else:
        unit.entry = {"tenant_id": str(uuid4())}
    done = unit.run(kind)
    assert (done.error_code, done.value) == ("credential_unavailable", None)
    assert done.artifact_reads == 0 and done.checkpoints == [] and fake.requests == []


def test_an_entra_tenant_in_another_case_is_the_targets(
    unit: Unit, fake: FakeFabricWorkspace
) -> None:
    unit.entry = {"entra_tenant_id": SP8_ENTRA_TENANT.upper()}
    assert unit.seal() == FIRST_STEPS


# ------------------------------------------------------------------ the sealed artifact (D)
def _rebuild(content: bytes, edit: Callable[[dict[str, bytes]], None]) -> bytes:
    files = read_archive(content)
    edit(files)
    return create_archive(files)


def _hidden(files: dict[str, bytes]) -> None:
    files[".env"] = b"x"


def _extra(files: dict[str, bytes]) -> None:
    files["setup.py"] = b"x"


def _first_cell(files: dict[str, bytes]) -> None:
    notebook = json.loads(files["main.ipynb"])
    notebook["cells"][0] = exit_cell("b" * 64)
    files["main.ipynb"] = json.dumps(notebook).encode()


def _no_release(files: dict[str, bytes]) -> None:
    del files["release.json"]


def _other_release(files: dict[str, bytes]) -> None:
    files["release.json"] = json.dumps({"manifest": {}, "digest": "b" * 64}).encode()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("edit", [_hidden, _extra, _first_cell, _no_release, _other_release])
def test_a_changed_artifact_ends_every_job_before_any_request(
    unit: Unit, fake: FakeFabricWorkspace, kind: str, edit: Callable[[dict[str, bytes]], None]
) -> None:
    """The plan names the changed bytes' digest, so only the content checks refuse it."""
    unit.deploy()
    unit.content = _rebuild(unit.content, edit)
    seen = len(fake.requests)
    done = unit.run(kind)
    assert (done.error_code, done.value) == ("execution_failed", None)
    assert done.checkpoints == [] and len(fake.requests) == seen


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("wrong", ["artifact_digest", "release_digest", "no_release_digest"])
def test_an_artifact_that_is_not_the_plans_ends_every_job_before_any_request(
    unit: Unit, fake: FakeFabricWorkspace, kind: str, wrong: str
) -> None:
    unit.deploy()
    change = {
        "artifact_digest": {"artifact_digest": "e" * 64},
        "release_digest": {"release_digest": "e" * 64},
        "no_release_digest": {"release_digest": None},
    }[wrong]
    unit.plan.update(change)
    seen = len(fake.requests)
    done = unit.run(kind)
    assert (done.error_code, done.value) == ("execution_failed", None)
    assert done.checkpoints == [] and len(fake.requests) == seen


@pytest.mark.parametrize("kind", ["observe", "verify"])
@pytest.mark.parametrize("wrong", ["artifact", "definition", "no_definition", "resource", "owned"])
def test_a_receipt_that_is_not_the_artifacts_is_refused_before_any_request(
    unit: Unit, fake: FakeFabricWorkspace, kind: str, wrong: str
) -> None:
    unit.deploy()
    receipt = unit.receipt
    assert receipt is not None
    handles = dict(receipt.operation_handles)
    if wrong == "artifact":
        receipt = receipt.model_copy(update={"artifact_digest": "e" * 64})
    elif wrong == "definition":
        handles["notebook:definition"] = "e" * 64
        receipt = receipt.model_copy(update={"operation_handles": handles})
    elif wrong == "no_definition":
        del handles["notebook:definition"]
        receipt = receipt.model_copy(update={"operation_handles": handles})
    elif wrong == "resource":  # a receipt item the target does not own
        stranger = receipt.resources[0].model_copy(
            update={"native_id": f"fabric:{WORKSPACE}:{uuid4()}"}
        )
        receipt = receipt.model_copy(update={"resources": [stranger, receipt.resources[1]]})
    else:
        unit.owned = unit.owned[:1]
    unit.receipt = receipt
    seen = len(fake.requests)
    done = unit.run(kind)
    assert (done.error_code, done.value) == ("execution_failed", None)
    assert len(fake.requests) == seen


@pytest.mark.parametrize("kind", ["observe", "verify"])
def test_a_job_without_a_stored_receipt_makes_no_request(
    unit: Unit, fake: FakeFabricWorkspace, kind: str
) -> None:
    unit.deploy().receipt = None
    seen = len(fake.requests)
    assert unit.run(kind).error_code == "execution_failed"
    assert len(fake.requests) == seen


@pytest.mark.parametrize("kind", KINDS)
def test_an_owned_resource_of_another_type_is_refused(
    unit: Unit, fake: FakeFabricWorkspace, kind: str
) -> None:
    unit.deploy()
    unit.owned.append(
        OwnedResource(resource_type="fabric.Report", native_id=f"fabric:{WORKSPACE}:{uuid4()}")
    )
    seen = len(fake.requests)
    assert unit.run(kind).error_code == "execution_failed"
    assert len(fake.requests) == seen


def test_the_spec_passed_to_the_adapter_is_of_the_artifacts_release(
    tmp_path: Path, fake: FakeFabricWorkspace
) -> None:
    """Observe and verify get the template spec of the digest in the artifact's `release.json`;
    nothing else of the job names a spec."""
    unit = Unit(tmp_path, fabric_platform(fake), release_digest="c" * 64).deploy()
    seen: list[Any] = []

    class Recording(FakePlatform):
        def fabric_adapter(self, target: Target, credential_provider: Any) -> FabricAdapter:
            made = super().fabric_adapter(target, credential_provider)
            observe, verify = made.observe, made.verify

            def observed(request: ObserveRequest, ctx: Any) -> Any:
                seen.append(request.spec)
                return observe(request, ctx)

            def verified(request: VerifyRequest, ctx: Any) -> Any:
                seen.append(request.spec)
                return verify(request, ctx)

            made.observe, made.verify = observed, verified  # type: ignore[method-assign]
            return made

    unit.platform = Recording(fabric=fake, environment="test", fabric_enabled=True)
    for kind in ("observe", "verify"):
        assert unit.run(kind).value is not None
    assert seen == [template_spec("c" * 64)] * 2


def test_a_stale_target_revision_is_not_applied(unit: Unit, fake: FakeFabricWorkspace) -> None:
    unit.seal()
    unit.plan["target_revision"] = 2
    done = unit.run("apply")
    assert done.error_code == "execution_failed"
    assert done.checkpoints == [] and fake.writes == []


# ------------------------------------------------------------------ foreign items, faults
def test_a_foreign_item_is_a_code_of_its_own_in_a_plan_only(
    unit: Unit, fake: FakeFabricWorkspace
) -> None:
    unit.seal()
    fake.seed_foreign_item(WORKSPACE, "Notebook", FABRIC_BINDINGS["notebook_name"])
    assert unit.run("plan").error_code == "foreign_item"
    done = unit.run("apply")
    assert done.error_code == "execution_failed"
    assert done.checkpoints == [] and fake.writes == []


@pytest.mark.parametrize("drift", ["description", "displayName", "definition", "deleted"])
def test_a_drifted_item_is_observed_unknown_and_fails_native_items(
    unit: Unit, fake: FakeFabricWorkspace, drift: str
) -> None:
    unit.deploy()
    assert unit.receipt is not None
    notebook = unit.receipt.resources[1].native_id.rsplit(":", 1)[1]
    if drift == "deleted":
        del fake.items[WORKSPACE][notebook]
    elif drift == "definition":
        part = fake.definitions[notebook]["parts"][0]
        doc = json.loads(base64.b64decode(part["payload"]))
        doc["cells"][1]["source"] = ["print('changed')\n"]
        part["payload"] = base64.b64encode(json.dumps(doc).encode()).decode()
    else:
        fake.items[WORKSPACE][notebook][drift] = "changed"
    observed = unit.run("observe")
    assert observed.error_code == "execution_failed" or (
        observed.value is not None and observed.value["state"] == "unknown"
    )
    verified = unit.run("verify").value
    assert verified is not None
    checks = {c["name"]: c["status"] for c in verified["checks"]}
    # the fake's job is under its item, so it is gone with a deleted notebook
    health = "failed" if drift == "deleted" else "passed"
    assert checks == {"native-items": "failed", "runtime-health": health}


# ------------------------------------------------------------------ the fake's switch (E)
@pytest.mark.parametrize(
    ("environment", "enabled"),
    [
        ("test", False),
        ("development", False),
        ("", True),
        ("production", True),
        ("staging", True),
    ],
)
def test_the_fabric_fake_is_refused_without_its_switch_or_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str, enabled: bool
) -> None:
    monkeypatch.setenv("SHIFDE_ENV", "test")  # the process environment does not stand in for it
    platform = FakePlatform(environment=environment, fabric_enabled=enabled)
    target = Target(tenant_id=uuid4(), host=ORIGIN, bindings={"workspace": WORKSPACE})
    with pytest.raises(RuntimeError):
        platform.fabric_adapter(target, lambda: pytest.fail("the credentials were read"))
    assert platform.fabric is None
    assert FakePlatform(environment="test").fabric_enabled is False  # off by default


@pytest.mark.parametrize("environment", ["test", "development"])
def test_the_switched_on_fake_knows_the_targets_workspace(environment: str) -> None:
    platform = FakePlatform(environment=environment, fabric_enabled=True)
    target = Target(tenant_id=uuid4(), host=ORIGIN, bindings={"workspace": WORKSPACE})
    made = platform.fabric_adapter(target, lambda: pytest.fail("the credentials were read"))
    assert platform.fabric is not None and platform.fabric.workspaces == {WORKSPACE}
    assert made.env == environment
    platform.fabric.capacity[WORKSPACE] = False
    platform.fabric_adapter(target, lambda: pytest.fail("the credentials were read"))
    assert platform.fabric.capacity == {WORKSPACE: False}  # an existing workspace is left alone


def test_a_switched_off_fake_fails_the_job_without_a_request(
    unit: Unit, fake: FakeFabricWorkspace
) -> None:
    unit.platform = FakePlatform(fabric=fake, environment="test")
    assert unit.run("plan").error_code == "execution_failed"
    assert fake.requests == []


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, False), ("", False), ("false", False), ("0", False), ("true", True), ("TRUE", True)],
)
def test_the_entry_point_turns_the_switch_on_only_with_its_variable(
    tmp_path: Path, value: str | None, enabled: bool
) -> None:
    creds = sp8_credentials_file(tmp_path, "fabric", API + WORKSPACE, tenant=uuid4())
    environ = {
        main.API_URL: "http://127.0.0.1:8000",
        main.TOKEN: "shx_test",
        main.CREDENTIALS_FILE: str(creds),
        "SHIFDE_ENV": "test",
    }
    if value is not None:
        environ["SHIFDE_DEV_FAKE_DATA_PLATFORMS"] = value
    platform = main.configure(["--fake-platform"], environ).platform
    assert isinstance(platform, FakePlatform) and platform.fabric_enabled is enabled


# ------------------------------------------------------------------ toolchain digests
DATABRICKS_REAL = "a604fd78db32cce26aea6b2662071da5ac6f75519a13097672275a5d10428565"
DATABRICKS_FAKE = "b7cb139e7fa56f659b0b4dd09260948d2e26dd017d8da825584f0d3036afcb5e"
SNOWFLAKE_REAL = "f9236a56e807fbf8c9e6c82c7cf607aeedeb41055082f12b3c929f5935bc336c"
SNOWFLAKE_FAKE = "e8945c7a6e3b65dc94d134f1e83a9807fdc9372bc7cfcdbdf20d593f5f76201f"


def test_the_toolchain_digests_are_distinct_and_the_others_unchanged() -> None:
    assert toolchain_digest("databricks") == DATABRICKS_REAL
    assert toolchain_digest("fake") == DATABRICKS_FAKE
    assert toolchain_digest("databricks", "snowflake") == SNOWFLAKE_REAL
    assert toolchain_digest("fake", "snowflake") == SNOWFLAKE_FAKE
    fabric = {toolchain_digest(p, "fabric") for p in ("fake", "databricks")}
    others = {DATABRICKS_REAL, DATABRICKS_FAKE, SNOWFLAKE_REAL, SNOWFLAKE_FAKE, TOOLCHAIN_DIGEST}
    assert len(fabric | others) == 7


class Clients:
    """Stands in for the runner's `FabricClient` and `EntraTokenProvider`: real client calls
    over the fake's transport, and a recording token provider; nothing leaves the process."""

    def __init__(self, fake: FakeFabricWorkspace) -> None:
        self.fake = fake
        self.ids: list[tuple[str, str]] = []
        self.secrets: list[str] = []
        self.alive: list[weakref.ref[Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outer = self

        class Provider:
            def __init__(self, entra: str, client: str, secret: Callable[[], str]) -> None:
                outer.ids.append((entra, client))
                outer.alive.append(weakref.ref(self))
                self.secret = secret

            def __call__(self) -> str:
                outer.secrets.append(self.secret())
                return f"token-of-{self.secret()}"

        def client(target: Target, tokens: Callable[[], str]) -> FabricClient:
            made = FabricClient(target, tokens, transport=outer.fake.transport)
            outer.alive.append(weakref.ref(made))
            return made

        monkeypatch.setattr(runner_module, "EntraTokenProvider", Provider)
        monkeypatch.setattr(runner_module, "FabricClient", client)


@pytest.fixture
def clients(fake: FakeFabricWorkspace, monkeypatch: pytest.MonkeyPatch) -> Clients:
    made = Clients(fake)
    made.install(monkeypatch)
    return made


def test_a_plan_sealed_on_the_fake_is_not_applied_by_a_real_executor(
    unit: Unit, fake: FakeFabricWorkspace, clients: Clients
) -> None:
    unit.seal()
    assert unit.plan["toolchain_digest"] == toolchain_digest("fake", "fabric")
    unit.platform = RealPlatform()
    done = unit.run("apply")
    assert done.error_code == "execution_failed"
    assert done.checkpoints == [] and fake.writes == []
    unit.plan["toolchain_digest"] = toolchain_digest("databricks", "fabric")
    assert unit.run("apply").checkpoints  # the same plan of the real toolchain is


# ------------------------------------------------------------------ one provider per job (A, B)
def _entry(number: int) -> dict[str, str]:
    return {
        "entra_tenant_id": f"0a1b2c3d-0000-4000-8000-{number:012d}",
        "client_id": f"0a1b2c3d-0000-4000-9000-{number:012d}",
    }


def test_each_job_has_its_own_token_provider_and_nothing_keeps_it(
    unit: Unit, fake: FakeFabricWorkspace, clients: Clients
) -> None:
    fake.add_workspace(WORKSPACE)
    unit.platform = platform = RealPlatform()
    before = dict(vars(runner_module))
    lease = unit.lease("plan")
    made, gateway = unit.runner(lease)
    made._run(lease)
    assert gateway.error_code is None
    second = unit.run("plan")
    assert second.error_code is None
    assert clients.ids == [(SP8_ENTRA_TENANT, SP8_CLIENT_ID)] * 2  # one provider per job
    assert set(clients.secrets) == {SP8_SECRET}
    gc.collect()
    assert clients.alive and all(ref() is None for ref in clients.alive)
    assert vars(platform) == {} and made._state is None
    assert dict(vars(runner_module)) == before


def test_two_organizations_jobs_use_only_their_own_entra_ids(
    tmp_path: Path, fake: FakeFabricWorkspace, clients: Clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = []
    for number, workspace in ((1, WORKSPACE), (2, OTHER)):
        fake.add_workspace(workspace)
        one = Unit(tmp_path / str(number), RealPlatform(), workspace=workspace)
        (tmp_path / str(number)).mkdir()
        secret = tmp_path / str(number) / "own.secret"
        secret.write_text(f"secret-{number}", encoding="utf-8")
        secret.chmod(0o600)
        one.entry = {**_entry(number), "client_secret_file": str(secret)}
        units.append(one)
    bindings = {n: {"entra_tenant_id": _entry(n)["entra_tenant_id"]} for n in (1, 2)}
    monkeypatch.setattr(
        Unit,
        "target",
        property(
            lambda u: Target(
                tenant_id=u.tenant,
                id=u.target_id,
                host=ORIGIN,
                bindings={
                    **FABRIC_BINDINGS,
                    **bindings[units.index(u) + 1],
                    "workspace": u.workspace,
                },
            )
        ),
    )
    for number, one in enumerate(units, 1):
        clients.ids.clear()
        clients.secrets.clear()
        assert one.run("plan").error_code is None
        own = _entry(number)
        assert clients.ids == [(own["entra_tenant_id"], own["client_id"])]
        assert set(clients.secrets) == {f"secret-{number}"}
    # A's job with the other organization's entry: refused, no provider
    clients.ids.clear()
    units[0].entry = {**units[0].entry, "tenant_id": str(units[1].tenant)}
    assert units[0].run("plan").error_code == "credential_unavailable"
    assert clients.ids == []


def test_the_provider_rechecks_the_broker_and_reads_the_secret_on_every_call(unit: Unit) -> None:
    lease = unit.lease("plan")
    made, gateway = unit.runner(lease)
    state = LeaseState(lease=lease, lease_until=lease.lease_until)
    state.grant = unit.grant(lease, utcnow() - timedelta(seconds=1))  # expired: refreshed
    provider = made._fabric_provider(state, unit.target)
    assert provider().client_secret == SP8_SECRET
    assert gateway.grants == 1 and state.grant is gateway.grant
    secret_file = unit.directory / "fabric.json.secret"
    secret_file.write_text("rotated", encoding="utf-8")
    assert provider().client_secret == "rotated" and gateway.grants == 1
    for other in (
        {"target_id": uuid4()},
        {"job_id": uuid4()},
        {"executor_id": uuid4()},
        {"audience": API + OTHER},
        {"credential_ref": "other"},
    ):
        state.grant = gateway.grant.model_copy(update=other)
        with pytest.raises(CredentialUnavailable):
            provider()
    state.grant = gateway.grant
    stranger = unit.target.model_copy(
        update={"bindings": {**unit.target.bindings, "entra_tenant_id": OTHER}}
    )
    with pytest.raises(CredentialUnavailable):
        made._fabric_provider(state, stranger)()
    secret_file.unlink()
    with pytest.raises(CredentialUnavailable):
        provider()


# ------------------------------------------------------------------ through the routes
@pytest.fixture
def quiet() -> Iterator[None]:
    """The executor's own HTTP client log levels (`main.run`)."""
    levels = {name: logging.getLogger(name).level for name in HTTP_CLIENT_LOGGERS}
    quiet_http_client_logs()
    yield
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)


def _results(owner_db: Database, flow: Flow) -> str:
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT result FROM shifde.delivery_jobs WHERE tenant_id = :t"), {"t": flow.tenant}
        ).scalars()
        reasons = o.execute(
            text(
                "SELECT coalesce(status_reason, '') FROM shifde.delivery_plans WHERE tenant_id = :t "
                "UNION ALL SELECT coalesce(status_reason, '') FROM shifde.delivery_deployments "
                "WHERE tenant_id = :t"
            ),
            {"t": flow.tenant},
        ).scalars()
        return str([list(rows), list(reasons)])


def test_two_releases_are_verified_on_the_fake_and_leak_no_secret(
    flow: Flow,
    tmp_path: Path,
    owner_db: Database,
    fake: FakeFabricWorkspace,
    caplog: pytest.LogCaptureFixture,
    quiet: None,
) -> None:
    caplog.set_level(logging.DEBUG)
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)

    plan = ask_plan(flow, target["id"], release_id=fabric_release(owner_db, target["id"]).id)
    assert runner.run_once()
    sealed = plan_row(owner_db, plan["id"])
    assert sealed["status"] == "sealed", sealed["status_reason"]
    assert [s["key"] for s in sealed["steps"]] == FIRST_STEPS
    assert sealed["toolchain_digest"] == toolchain_digest("fake", "fabric")
    assert fake.requests and fake.writes == []

    deployment = flow.approve(flow.get_plan(plan["id"]))["deployment_id"]
    assert runner.run_once()
    body = flow.get(deployment)
    assert body["status"] == "observing", body
    journal = {s["step_key"]: s for s in body["journal"]}
    assert list(journal) == FIRST_STEPS and TERMINAL in journal and VERIFICATION in journal
    assert all(s["state"] == "complete" for s in body["journal"])
    assert all(s["receipt"]["platform"] == "fake" for s in body["journal"])
    assert body["receipt"]["operation_handles"]["platform"] == "fake"
    items = {r["key"]: r["native_id"] for r in body["receipt"]["resources"]}
    run_due(flow, runner, deployment, "observe", "verify")
    body = flow.get(deployment)
    assert body["status"] == "verified", body
    assert {c["name"]: c["status"] for c in body["verification"]["checks"]} == dict.fromkeys(
        CHECKS, "passed"
    )

    # a second release updates the same items
    second = fabric_release(owner_db, target["id"])
    plan = ask_plan(flow, target["id"], release_id=second.id)
    assert runner.run_once()
    sealed = plan_row(owner_db, plan["id"])
    assert [s["key"] for s in sealed["steps"]] == UPDATE_STEPS
    deployment = flow.approve(flow.get_plan(plan["id"]))["deployment_id"]
    run_due(flow, runner, deployment, "apply", "observe", "verify")
    body = flow.get(deployment)
    assert body["status"] == "verified", body
    assert {r["key"]: r["native_id"] for r in body["receipt"]["resources"]} == items
    assert body["receipt"]["operation_handles"]["release_digest"] == second.digest

    blobs = (caplog.text, _results(owner_db, flow), json.dumps(body))
    for leak in (SP8_SECRET, str(tmp_path), "fabric.json.secret", "fake-token"):
        assert all(leak not in blob for blob in blobs), leak


def test_a_foreign_item_fails_the_plan_without_a_write(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace
) -> None:
    target = fabric_target(flow)
    workspace = fake.add_workspace(workspace_of(target))
    fake.seed_foreign_item(workspace, "Lakehouse", target["bindings"]["notebook_name"])
    runner = flow_runner(flow, target, tmp_path, fake)
    plan = ask_plan(flow, target["id"], release_id=fabric_release(owner_db, target["id"]).id)
    assert runner.run_once()
    failed = plan_row(owner_db, plan["id"])
    assert (failed["status"], failed["status_reason"]) == (
        "plan_failed",
        "plan job failed: foreign_item",
    )
    assert failed["digest"] is None and fake.writes == []


def test_every_platform_refusal_fails_the_plan_with_the_same_reason(
    flow: Flow,
    tmp_path: Path,
    owner_db: Database,
    fake: FakeFabricWorkspace,
    caplog: pytest.LogCaptureFixture,
    quiet: None,
) -> None:
    """403, 404, another error and an unreachable platform are told apart nowhere."""
    caplog.set_level(logging.DEBUG)
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    release = fabric_release(owner_db, target["id"])
    item = str(uuid4())
    answers: list[Any] = [403, 404, 401, 500, httpx.ConnectError("unreachable " + item)]
    reasons = []
    for answer in answers:

        def handler(request: httpx.Request, answer: Any = answer) -> httpx.Response:
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer, json={"errorCode": "ItemNotFound", "item": item})

        fake.transport = httpx.MockTransport(handler)
        plan = ask_plan(flow, target["id"], release_id=release.id)
        caplog.clear()
        assert runner.run_once()
        failed = plan_row(owner_db, plan["id"])
        reasons.append((failed["status"], failed["status_reason"]))
        lines = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("shifde"))
        for leak in (
            workspace_of(target),
            item,
            "ItemNotFound",
            f"({answer})",
            *(target["bindings"][k] for k in ("lakehouse_name", "notebook_name")),
        ):
            assert leak not in caplog.text if len(leak) > 8 else leak not in lines
    assert reasons == [("plan_failed", "plan job failed: execution_failed")] * len(answers)
    assert workspace_of(target) not in _results(owner_db, flow)


# ------------------------------------------------------------------ the outcome of a fault
@pytest.mark.parametrize(
    "step",
    [
        "lakehouse-metadata-submit",
        "notebook-metadata-submit",
        "notebook-definition-submit",
        "fabric-verification-submit",
    ],
)
def test_a_write_lost_after_acceptance_is_an_unknown_outcome(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace, step: str
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    if step == "notebook-definition-submit":  # an update of owned items
        first = approved_deployment(flow, runner, target, fabric_release(owner_db, target["id"]).id)
        run_due(flow, runner, first, "apply", "observe", "verify")
        assert deployment_row(owner_db, first)["status"] == "verified"
    release = fabric_release(owner_db, target["id"])
    deployment = approved_deployment(flow, runner, target, release.id)
    fake.fail_after_accept = step
    assert runner.run_once()
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "unknown_outcome", row["status_reason"]
    body = flow.get(deployment)
    assert {s["step_key"]: s["state"] for s in body["journal"]}[step] == "intent"
    written, requested = list(fake.writes), len(fake.requests)
    fake.fail_after_accept = None
    assert flow.act(deployment, "reconcile").status_code in (200, 202)
    run_due(flow, runner, deployment, "reconcile")
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "unknown_outcome"
    assert row["reconcile_evidence"]["state"] == "ambiguous"
    assert not runner.run_once()
    assert fake.writes == written and len(fake.requests) == requested


def test_a_failed_operation_after_acceptance_is_an_unknown_outcome(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    deployment = approved_deployment(
        flow, runner, target, fabric_release(owner_db, target["id"]).id
    )
    fake.lro_failed = True
    assert runner.run_once()
    assert deployment_row(owner_db, deployment)["status"] == "unknown_outcome"


def test_a_failed_verification_job_fails_the_deployment(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    deployment = approved_deployment(
        flow, runner, target, fabric_release(owner_db, target["id"]).id
    )
    fake.job_failed = True
    run_due(flow, runner, deployment, "apply", "observe")
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "the platform reported a failure")


@pytest.mark.parametrize("when", ["observe", "verify"])
def test_a_drift_after_apply_is_never_verified(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace, when: str
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    deployment = approved_deployment(
        flow, runner, target, fabric_release(owner_db, target["id"]).id
    )
    run_due(flow, runner, deployment, "apply", *(["observe"] if when == "verify" else []))
    for found in fake.items[workspace_of(target)].values():
        found["description"] = "changed after apply"
    run_due(flow, runner, deployment, when)
    status = deployment_row(owner_db, deployment)["status"]
    assert status == ("observing" if when == "observe" else "failed")
    if when == "observe":  # polled until the deadline, then failed
        owner_sql(
            owner_db,
            "UPDATE shifde.delivery_deployments SET observe_deadline = now() - interval '1 second' "
            "WHERE id = :d",
            d=deployment,
        )
        make_due(owner_db, deployment)
        runner.run_once()
        assert flow.get(deployment)["status"] == "failed"


# ------------------------------------------------------------------ the target changed (C)
def test_an_apply_after_the_targets_revision_changed_writes_nothing(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    deployment = approved_deployment(
        flow, runner, target, fabric_release(owner_db, target["id"]).id
    )
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_targets SET revision = revision + 1 WHERE id = :t",
        t=target["id"],
    )
    assert runner.run_once()
    body = flow.get(deployment)
    assert body["status"] == "failed" and body["journal"] == [], body
    assert body["status_reason"] == "no platform write was attempted"
    assert fake.writes == []


def test_an_apply_on_an_archived_target_writes_nothing(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    deployment = approved_deployment(
        flow, runner, target, fabric_release(owner_db, target["id"]).id
    )
    h = as_user(flow.client, flow.requester)
    archived = flow.client.post(f"{targets(flow.slug)}/{target['id']}/archive", headers=h)
    flow.client.cookies.clear()
    assert archived.status_code == 200, archived.text
    assert archived.json()["revision"] == target["revision"] + 1
    seen = len(fake.requests)
    assert runner.run_once()
    assert deployment_row(owner_db, deployment)["status"] == "failed"
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"]["status"] == "failed"
    assert fake.writes == [] and len(fake.requests) == seen


def test_native_resources_are_recorded_under_their_types(
    flow: Flow, tmp_path: Path, owner_db: Database, fake: FakeFabricWorkspace
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, fake)
    deployment = approved_deployment(
        flow, runner, target, fabric_release(owner_db, target["id"]).id
    )
    assert runner.run_once()
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT resource_type, native_id FROM shifde.delivery_native_resources "
                "WHERE deployment_id = :d ORDER BY resource_type"
            ),
            {"d": deployment},
        ).all()
    assert [r[0] for r in rows] == [LAKEHOUSE, NOTEBOOK]
    assert {r[1].rsplit(":", 1)[1] for r in rows} == set(fake.items[workspace_of(target)])
