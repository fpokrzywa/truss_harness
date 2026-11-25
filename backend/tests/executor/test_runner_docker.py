"""The runner on a Docker target (SP8-9), over the real gateway and the fake runtime serving the
rendered app on the sample-data workspace fake (no daemon, no network): one deployment to
`verified`, a second release replacing its container, a failing deep health, a fault after an
accepted write, the plan refusal codes, and everything a Docker job refuses: a plan of another
generator, a bundle of another release, a resource of another type. Also the toolchain digests and
the real platform's CLI plugin directories."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast  # Any: JSON bodies and recorded calls
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from shifde.adapters.native.base import ArtifactStore, InMemoryArtifactStore
from shifde.adapters.native.contracts import JobContext, JobLease, OwnedResource, Target
from shifde.adapters.native.docker import LIVE_VALIDATED, MATURITY
from shifde.adapters.native.docker.adapter import (
    RESOURCE_KEY,
    RESOURCE_TYPE,
    DockerComposeAdapter,
)
from shifde.adapters.native.docker.fake import (
    FakeContainer,
    FakeContainerRuntime,
    prepare_rendered_app,
)
from shifde.adapters.native.docker.runtime import DockerCliRuntime
from shifde.adapters.native.docker.source import TARGET_LABEL, compose_project, image_tag
from shifde.core.db import Database
from shifde.executor import runner as runner_module
from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker
from shifde.executor.runner import FakePlatform, RealPlatform, Runner, toolchain_digest
from tests.delivery.conftest import make_executor
from tests.delivery.docker import docker_release, docker_target, rows, workspace
from tests.delivery.routes import (
    Flow,
    address,
    deployment_jobs,
    deployment_row,
    job_row,
    make_due,
    owner_sql,
    plan_row,
)
from tests.executor.support import ask_plan, credentials_file, sp8_credentials_file
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

CHECKS = ["native-version", "runtime-health", "app-deep-health"]
STEPS = ["image-build", "image-verify", "compose-up", "container-ready"]


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIFDE_ENV", "test")  # the Docker fake refuses anything else


class Docker:
    """A's Docker target on a claimed workspace, a release of it and a runner over `platform`."""

    def __init__(self, flow: Flow, tmp_path: Path, platform: FakePlatform | None = None) -> None:
        self.flow, self.owner_db = flow, flow.owner_db
        self.target = docker_target(flow, workspace_host=workspace(flow))
        self.release = docker_release(flow, self.target)
        self.platform = platform or FakePlatform(environment="test")
        self.gateway = GatewayClient("https://testserver", flow.token, http=flow.client)
        me = self.gateway.whoami()
        creds = sp8_credentials_file(tmp_path, "docker", self.daemon, tenant=flow.tenant)
        self.runner = Runner(
            self.gateway,
            CredentialBroker.from_file(creds),
            me.executor_id,
            self.platform,
            tenant_id=me.tenant_id,
        )

    @property
    def daemon(self) -> str:
        return str(self.target["address"])

    @property
    def project(self) -> str:
        return compose_project(UUID(self.target["id"]))

    @property
    def fake(self) -> FakeContainerRuntime:
        """The platform's runtime, built as the platform builds it when no job has run yet."""
        if self.platform.docker is None:
            daemon_id = self.daemon.removeprefix("docker://")
            self.platform.docker = FakeContainerRuntime(daemon_id, prepare_app=prepare_rendered_app)
        return self.platform.docker

    def plan(self, release_id: object | None = None) -> Any:  # Any: a plan row
        """The plan of `release_id` (the release unless given) after its plan job ran."""
        made = ask_plan(self.flow, self.target["id"], release_id=release_id or self.release.id)
        assert self.runner.run_once()
        return plan_row(self.owner_db, made["id"])

    def approved(self, release_id: object | None = None) -> str:
        row = self.plan(release_id)
        assert row["status"] == "sealed", row["status_reason"]
        return str(self.flow.approve(self.flow.get_plan(row["id"]))["deployment_id"])

    def run(self, deployment: str, *jobs: str) -> Any:  # Any: the deployment row
        for _ in jobs:
            make_due(self.owner_db, deployment)
            assert self.runner.run_once()
        return deployment_row(self.owner_db, deployment)

    def reason(self, plan: Any) -> str:
        return str(plan_row(self.owner_db, plan["id"])["status_reason"])


@pytest.fixture
def docker(flow: Flow, tmp_path: Path) -> Docker:
    return Docker(flow, tmp_path)


def _edit_context(
    monkeypatch: pytest.MonkeyPatch, edit: Callable[[JobContext], JobContext], kind: str
) -> None:
    """The real gateway's context of a `kind` job, changed by `edit`."""
    context = GatewayClient.context

    def edited(self: GatewayClient, lease: JobLease) -> JobContext:
        ctx = context(self, lease)
        return edit(ctx) if ctx.job.kind == kind else ctx

    monkeypatch.setattr(GatewayClient, "context", edited)


def _plan_fields(**fields: Any) -> Callable[[JobContext], JobContext]:
    def edit(ctx: JobContext) -> JobContext:
        assert ctx.plan is not None
        return ctx.model_copy(update={"plan": ctx.plan.model_copy(update=fields)})

    return edit


# ------------------------------------------------------------------ end to end
def test_a_docker_deployment_reaches_verified_and_a_second_release_replaces_its_container(
    docker: Docker, owner_db: Database
) -> None:
    modules = set(sys.modules)
    plan = docker.plan()
    assert plan["status"] == "sealed", plan["status_reason"]
    assert [(s["key"], s["action"]) for s in plan["steps"]] == [
        ("image-build", "create"),
        ("compose-up", "create"),
        ("container-ready", "start"),
    ]
    assert plan["toolchain_digest"] == toolchain_digest("fake", "docker")
    deployment = docker.flow.approve(docker.flow.get_plan(plan["id"]))["deployment_id"]
    row = docker.run(deployment, "apply")
    assert row["status"] == "observing", row["status_reason"]
    journal = {s["step_key"]: s["state"] for s in docker.flow.get(deployment)["journal"]}
    assert journal == dict.fromkeys(STEPS, "complete")
    container = docker.fake.projects[docker.project]
    digest = docker.release.release.digest
    assert container.image == image_tag(docker.project, digest)
    assert docker.run(deployment, "observe", "verify")["status"] == "verified"
    body = docker.flow.get(deployment)
    assert [(c["name"], c["status"]) for c in body["verification"]["checks"]] == [
        (name, "passed") for name in CHECKS
    ]
    # the staged rendered app answered, over every data-read table, and left no module behind
    status, deep = docker.fake.http_get_local(docker.project, container.port, "/health?deep=1")
    assert status == 200 and digest.encode() in deep
    assert b"table:main.sales.invoices" in deep
    added = set(sys.modules) - modules
    assert not {m for m in added if m.startswith(("agent_loop", "backend_", "shifde_fake_app"))}

    second = docker_release(docker.flow, docker.target)
    assert second.release.digest != digest
    plan = docker.plan(second.id)
    assert [(s["key"], s["action"]) for s in plan["steps"]] == [
        ("image-build", "create"),
        ("compose-up", "update"),
        ("container-ready", "start"),
    ]
    (owned,) = plan["snapshot"]["resources"]
    assert (owned["key"], owned["resource_type"]) == (RESOURCE_KEY, RESOURCE_TYPE)
    deployment = docker.flow.approve(docker.flow.get_plan(plan["id"]))["deployment_id"]
    assert docker.run(deployment, "apply", "observe", "verify")["status"] == "verified"
    assert list(docker.fake.projects) == [docker.project]
    assert docker.fake.projects[docker.project].image == image_tag(
        docker.project, second.release.digest
    )


@pytest.mark.parametrize("how", ["unreadable-table", "fault"])
def test_a_failing_deep_health_ends_failed(docker: Docker, how: str) -> None:
    release = docker.release
    if how == "fault":
        docker.fake.deep_health_bad = True
    else:

        def unreadable(manifest: dict[str, Any]) -> None:
            manifest["data_reads"][0]["tables"].append("main.default.payroll")

        release = docker_release(docker.flow, docker.target, edit=unreadable)
    deployment = docker.approved(release.id)
    row = docker.run(deployment, "apply", "observe", "verify")
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")
    checks = {c["name"]: c for c in docker.flow.get(deployment)["verification"]["checks"]}
    assert checks["native-version"]["status"] == checks["runtime-health"]["status"] == "passed"
    assert checks["app-deep-health"]["status"] == "failed"
    reason = "warehouse unreachable" if how == "fault" else "cannot read main.default.payroll"
    assert reason in checks["app-deep-health"]["evidence"]


@pytest.mark.parametrize("step", ["image-build", "compose-up"])
def test_a_fault_after_an_accepted_write_is_an_unknown_outcome_and_never_retried(
    docker: Docker, owner_db: Database, step: str
) -> None:
    docker.fake.fail_after_accept = step
    deployment = docker.approved()
    row = docker.run(deployment, "apply")
    assert row["status"] == "unknown_outcome", row["status_reason"]
    actions = list(docker.fake.actions)
    action = {"image-build": "build", "compose-up": "compose-up"}[step]
    assert [a for a, _ in actions].count(action) == 1
    docker.fake.fail_after_accept = None
    assert not docker.runner.run_once()
    assert docker.fake.actions == actions
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert (apply["status"], apply["attempts"]) == ("done", 1)
    assert apply["result"] == {"status": "failed", "error_code": "execution_failed"}
    # a reconcile reads no plan details and never replays
    r = docker.flow.act(deployment, "reconcile")
    assert r.status_code in (200, 202), r.text
    row = docker.run(deployment, "reconcile")
    assert row["status"] == "unknown_outcome" and docker.fake.actions == actions
    assert row["reconcile_evidence"]["state"] == "ambiguous"


# ------------------------------------------------------------------ plan refusal codes
def test_a_plan_job_on_another_daemon_is_docker_host_mismatch(docker: Docker) -> None:
    docker.fake.daemon_mismatch = True
    plan = docker.plan()
    assert plan["status"] == "plan_failed"
    assert plan["status_reason"] == "plan job failed: docker_host_mismatch"
    assert docker.fake.actions == []


@pytest.mark.parametrize("label", [None, "0a1b2c3d-0000-4000-8000-00000000abcd"])
def test_a_plan_job_on_another_targets_project_is_foreign_project(
    docker: Docker, label: str | None
) -> None:
    docker.fake.projects[docker.project] = FakeContainer(
        name="app",
        image="other:1",
        state="running",
        health="healthy",
        port=8501,
        labels={TARGET_LABEL: label} if label else {},
    )
    plan = docker.plan()
    assert plan["status"] == "plan_failed"
    assert plan["status_reason"] == "plan job failed: foreign_project"
    assert docker.fake.actions == []


def test_a_docker_refusal_of_another_job_kind_is_execution_failed(
    docker: Docker, owner_db: Database
) -> None:
    deployment = docker.approved()
    docker.fake.daemon_mismatch = True
    row = docker.run(deployment, "apply")
    assert (row["status"], row["status_reason"]) == (
        "failed",
        "no platform write was attempted",
    )
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    result = job_row(owner_db, apply["id"])["result"]
    assert result == {"status": "failed", "error_code": "execution_failed"}
    assert docker.fake.actions == []


def test_another_failure_of_a_plan_job_is_execution_failed(
    docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: FakeContainerRuntime) -> str:
        raise RuntimeError("Docker command failed with exit code 1")

    monkeypatch.setattr(FakeContainerRuntime, "daemon_id", broken)
    assert docker.fake.actions == []
    plan = docker.plan()
    assert plan["status_reason"] == "plan job failed: execution_failed"


# ------------------------------------------------------------------ only the generator's bundles
class Watched(FakePlatform):
    """A fake platform that records every Docker adapter it builds."""

    def __init__(self) -> None:
        super().__init__(environment="test")
        self.built: list[UUID] = []

    def docker_adapter(self, target: Target, artifacts: ArtifactStore) -> DockerComposeAdapter:
        self.built.append(target.id)
        return super().docker_adapter(target, artifacts)


@pytest.mark.parametrize(
    "version",
    [None, "shifde.appgen/1.0", "shifde.appgen.docker/1.1", "shifde.appgen.snowflake/1.0"],
)
def test_a_plan_of_another_generator_is_refused_before_any_adapter(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    platform = Watched()
    docker = Docker(flow, tmp_path, platform)
    _edit_context(monkeypatch, _plan_fields(generator_version=version), "plan")
    plan = docker.plan()
    assert plan["status_reason"] == "plan job failed: execution_failed"
    assert platform.built == [] and platform.docker is None


def test_a_plan_of_the_docker_generator_builds_one_adapter_per_job(
    flow: Flow, tmp_path: Path
) -> None:
    platform = Watched()
    docker = Docker(flow, tmp_path, platform)
    assert docker.plan()["status"] == "sealed"
    assert platform.built == [UUID(docker.target["id"])]


def test_a_plan_job_refuses_a_bundle_of_another_release(
    docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert docker.fake.actions == []
    _edit_context(monkeypatch, _plan_fields(release_digest="f" * 64), "plan")
    plan = docker.plan()
    assert plan["status_reason"] == "plan job failed: execution_failed"


@pytest.mark.parametrize("release_digest", ["f" * 64, None])
def test_an_apply_job_refuses_a_bundle_of_another_release_before_any_write(
    docker: Docker, monkeypatch: pytest.MonkeyPatch, release_digest: str | None
) -> None:
    deployment = docker.approved()
    _edit_context(monkeypatch, _plan_fields(release_digest=release_digest), "apply")
    row = docker.run(deployment, "apply")
    assert (row["status"], row["status_reason"]) == (
        "failed",
        "no platform write was attempted",
    )
    assert docker.fake.actions == [] and docker.fake.images == {}


def test_a_verify_job_of_another_release_fails_native_version(
    docker: Docker, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = docker.approved()
    _edit_context(monkeypatch, _plan_fields(release_digest="f" * 64), "verify")
    row = docker.run(deployment, "apply", "observe", "verify")
    assert row["status"] == "failed"
    checks = docker.flow.get(deployment)["verification"]["checks"]
    assert [(c["name"], c["status"]) for c in checks] == [
        ("native-version", "failed"),
        ("runtime-health", "skipped"),
        ("app-deep-health", "skipped"),
    ]


def test_an_owned_resource_of_another_type_is_refused(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = Watched()
    docker = Docker(flow, tmp_path, platform)

    def edit(ctx: JobContext) -> JobContext:
        owned = OwnedResource(resource_type="databricks_app", native_id="sales-app")
        return ctx.model_copy(update={"owned_resources": [owned]})

    _edit_context(monkeypatch, edit, "plan")
    assert docker.plan()["status_reason"] == "plan job failed: execution_failed"
    assert platform.built == []


def test_the_docker_store_keeps_the_release_record_and_is_the_jobs_own(
    flow: Flow, tmp_path: Path
) -> None:
    stores: list[ArtifactStore] = []

    class Stores(Watched):
        def docker_adapter(self, target: Target, artifacts: ArtifactStore) -> DockerComposeAdapter:
            stores.append(artifacts)
            return super().docker_adapter(target, artifacts)

    docker = Docker(flow, tmp_path, Stores())
    docker.approved()
    assert docker.runner.run_once()  # apply
    assert len(stores) == 2 and stores[0] is not stores[1]
    for store in stores:
        assert type(store) is InMemoryArtifactStore
        assert [tenant for tenant, _ in store.archives] == [flow.tenant]


def test_an_artifact_that_is_not_the_bundle_is_refused(docker: Docker, owner_db: Database) -> None:
    """A tampered row: the artifact's bytes no longer match the plan's digest."""
    made = ask_plan(docker.flow, docker.target["id"], release_id=docker.release.id)
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_artifacts SET content = content || '\\x00'::bytea, "
        "size = size + 1 WHERE id = :a",
        a=made["artifact_id"],
    )
    assert docker.runner.run_once()
    assert docker.reason(made) == "plan job failed: execution_failed"


# ------------------------------------------------------------------ client A against client B
def test_bs_runner_leases_and_changes_nothing_of_as_docker_jobs(
    client: TestClient,
    docker: Docker,
    world: World,
    people: People,
    owner_db: Database,
    tmp_path: Path,
) -> None:
    """B's executor on the very same fake daemon: it leases none of A's plan, apply, observe or
    verify jobs, and none of A's rows change."""
    side = make_executor(client, world.b, people.owner_b)
    gateway = GatewayClient("https://testserver", side.token, http=client)
    me = gateway.whoami()
    creds = credentials_file(tmp_path, address(), tenant=side.tenant, name="b.json")
    b = Runner(
        gateway,
        CredentialBroker.from_file(creds),
        me.executor_id,
        docker.platform,
        tenant_id=me.tenant_id,
    )

    def b_reaches_nothing() -> None:
        before = rows(owner_db, docker.flow.tenant)
        assert not b.run_once()
        assert rows(owner_db, docker.flow.tenant) == before

    made = ask_plan(docker.flow, docker.target["id"], release_id=docker.release.id)
    b_reaches_nothing()
    assert docker.runner.run_once()
    deployment = docker.flow.approve(docker.flow.get_plan(made["id"]))["deployment_id"]
    for _ in ("apply", "observe", "verify"):
        make_due(owner_db, deployment)
        b_reaches_nothing()
        assert docker.runner.run_once()
    assert deployment_row(owner_db, deployment)["status"] == "verified"


# ------------------------------------------------------------------ toolchain and platforms
def test_the_docker_toolchain_digest_is_its_own_and_no_other_changes() -> None:
    """The Databricks and Snowflake digests are those of origin/main before SP8-9."""
    pinned = {
        ("fake", "databricks"): (
            "b7cb139e7fa56f659b0b4dd09260948d2e26dd017d8da825584f0d3036afcb5e"
        ),
        ("databricks", "databricks"): (
            "a604fd78db32cce26aea6b2662071da5ac6f75519a13097672275a5d10428565"
        ),
        ("fake", "snowflake"): ("e8945c7a6e3b65dc94d134f1e83a9807fdc9372bc7cfcdbdf20d593f5f76201f"),
        ("databricks", "snowflake"): (
            "f9236a56e807fbf8c9e6c82c7cf607aeedeb41055082f12b3c929f5935bc336c"
        ),
    }
    others = {key: toolchain_digest(*cast(Any, key)) for key in pinned}
    assert others == pinned
    fake, real = toolchain_digest("fake", "docker"), toolchain_digest("databricks", "docker")
    assert len({fake, real, *others.values()}) == 6


@pytest.mark.parametrize("constant", ["ADAPTER_VERSION", "BASE_IMAGE"])
def test_the_docker_toolchain_digest_names_its_parts(
    monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    before = toolchain_digest("fake", "docker")
    monkeypatch.setattr(runner_module.docker_source, constant, "changed")
    assert toolchain_digest("fake", "docker") != before


def test_the_docker_toolchain_digest_names_the_minimum_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = toolchain_digest("fake", "docker")
    assert runner_module.MIN_ENGINE == (28, 0)
    monkeypatch.setattr(runner_module, "MIN_ENGINE", (29, 0))
    assert toolchain_digest("fake", "docker") != before


@pytest.mark.parametrize("system", ["nt", "posix"])
def test_the_real_docker_adapter_gets_the_users_cli_plugins_on_windows_only(
    monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    """Built only: no command runs (B-3946)."""
    monkeypatch.setattr(runner_module, "os", SimpleNamespace(name=system))
    target = Target(tenant_id=uuid4(), host=f"docker://{uuid4()}", bindings={"port": "8501"})
    adapter = RealPlatform().docker_adapter(target, InMemoryArtifactStore())
    assert isinstance(adapter.runtime, DockerCliRuntime)
    plugins = Path.home() / ".docker" / "cli-plugins"
    assert adapter.runtime.plugin_dirs == ((plugins,) if system == "nt" else ())


def test_the_docker_adapter_stays_experimental() -> None:
    assert MATURITY == "experimental" and not LIVE_VALIDATED
