"""The runner: lease one job, run it through the adapter of its target's platform (Databricks or
Snowflake), post its result (design 4 and 5, settled calls 5, 8 and 14; FDE SDK
`shifde_executor.runner`, synchronous, one job at a time; B-2688..B-2695; SP6 design 6.4).

Every job reads its context (target, plan, deployment, owned resources) and a credential grant
under its lease; a grant the broker refuses ends the job `credential_unavailable` before any
adapter call. The deploying service principal comes from the credential entry, not the target.
The job context's `platform_kind` picks the adapter through `DISPATCH`: `databricks`
(`DatabricksAdapter`, tokens from the broker) or `snowflake` (`SnowflakeNativeAdapter`, a key pair
the broker re-checks and reads on every statement). An entry of another platform is refused like
any other mismatch. A `fabric`, `palantir` or `docker` job runs its platform's broker check and
then fails closed (`execution_failed`) before any adapter or platform call and before any journal
intent, until SP8-5, SP8-7 and SP8-9 wire its call path; the platforms already build those
adapters (`fabric_adapter`, `palantir_adapter`, `docker_adapter`).
While a job runs it is heartbeated in the background; a 409 marks the lease lost.

`apply` writes at most once: before each platform write the adapter's journal posts an `intent`
checkpoint and writes only once the gateway acknowledged it; afterwards it posts `complete`.
Any exception ends the job with one failed result `execution_failed` (none once the lease is
lost: the server's sweep decides `unknown_outcome` or `failed`). Nothing is retried here; the
server re-queues read-only jobs after a lost lease. A failed result never carries error text.

The platform is real (`RealPlatform`: the pinned Databricks CLI and workspace REST reads with
tokens from the broker, or the Snowflake SQL API and the pinned Snowflake CLI's stage copy with the
broker's key pair) or the in-memory fakes (`FakePlatform`, one fake per platform per process;
the Snowflake fake is built with the executor's `SHIFDE_ENV`, so it refuses outside development
and test, as the Docker fake does). The toolchain digest names the adapter, its CLI version and
the platform, so a plan made on a fake is never applied by a real executor, and the fake marks
every `complete` receipt and the apply receipt's handles with `platform: fake`.

Process control (SP4 design 6.2): `stop()` requests no new lease, while the job in flight runs to
its normal end, result included. `last_tick` is the monotonic time of the last run-loop pass (or
lease heartbeat while a job is in flight) and `live()` / `ready()` hold while it, and the client's
last successful gateway call, are within `window()`. `abandon()` (the shutdown grace ran out)
posts nothing more for the job in flight; `write_in_flight()` tells whether a platform write was.

A plan's artifact is the application source (the SDK source, or a Snowflake Native App package)
plus a root `release.json`, the release it deploys (SP3-4).
Its digest covers the whole archive; the adapter reads the source without `release.json`
(`ReleaseArtifactStore`), so the record is checked but never deployed.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol  # Any: JSON step inputs and receipts
from uuid import UUID, uuid5

from pydantic import BaseModel

from shifde.adapters import snowflake_jwt
from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.adapters.native.base import (
    ArtifactStore,
    DeployContext,
    InMemoryArtifactStore,
    NeedsAttention,
    ReadContext,
    create_archive,
    read_archive,
)
from shifde.adapters.native.contracts import (
    DOCKER_HOST,
    ApplyRequest,
    BuildArtifact,
    ContextPlan,
    CredentialGrant,
    DatabricksApplicationSpec,
    DeploymentPlan,
    JobContext,
    JobKind,
    JobLease,
    JournalEntry,
    ObserveRequest,
    PlanJobResult,
    PlanRequest,
    ReconcileRequest,
    ResourceBinding,
    SnowflakeApplicationSpec,
    SourceArchive,
    Target,
    VerifyRequest,
    digest,
    utcnow,
)
from shifde.adapters.native.databricks.adapter import (
    PRINCIPAL_BINDING,
    WAREHOUSE_BINDING,
    DatabricksAdapter,
    app_name,
    root_path,
)
from shifde.adapters.native.databricks.clients import DatabricksCLI, DatabricksWorkspaceGateway
from shifde.adapters.native.databricks.fake import FakeDatabricksPlatform
from shifde.adapters.native.databricks.source import ADAPTER_VERSION, CLI_VERSION
from shifde.adapters.native.docker.adapter import DockerComposeAdapter
from shifde.adapters.native.docker.fake import FakeContainerRuntime
from shifde.adapters.native.docker.runtime import DockerCliRuntime
from shifde.adapters.native.fabric.adapter import FabricAdapter
from shifde.adapters.native.fabric.clients import EntraTokenProvider, FabricClient
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.palantir.adapter import PalantirMarketplaceAdapter
from shifde.adapters.native.palantir.clients import FoundryCredentials, PalantirClient
from shifde.adapters.native.palantir.fake import FakeFoundryStack
from shifde.adapters.native.planning import bindings_digest, seal_plan
from shifde.adapters.native.snowflake import source as snowflake_source
from shifde.adapters.native.snowflake.adapter import RESOURCE_KEY_OF_TYPE, SnowflakeNativeAdapter
from shifde.adapters.native.snowflake.clients import (
    CredentialProvider,
    SnowflakeKeyPair,
    SnowflakeSqlApi,
    SnowflakeStageCopier,
)
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from shifde.executor.client import GatewayClient, LeaseLost
from shifde.executor.credentials import (
    CredentialBroker,
    CredentialUnavailable,
    FabricCredentials,
)

log = logging.getLogger(__name__)

KINDS: list[JobKind] = ["plan", "apply", "observe", "verify", "reconcile"]
HEARTBEAT_SECONDS = 15.0  # against the gateway's 45 s lease
POLL_SECONDS = 2.0
# application_id = uuid5(namespace, "<target_id>/<application_key>") (B-2690)
APPLICATION_NAMESPACE = UUID("6f1c2a4e-8d3b-5e7f-9a10-b2c3d4e5f607")
RELEASE_ENTRY = "release.json"  # the release a plan deploys, at the artifact's root (SP3-4)

TokenProvider = Callable[[str], str]
FabricCredentialProvider = Callable[[], FabricCredentials]
FoundryCredentialProvider = Callable[[str], FoundryCredentials]
ErrorCode = Literal["execution_failed", "credential_unavailable"]
TargetKind = Literal["databricks", "snowflake", "fabric", "palantir", "docker"]
Adapter = DatabricksAdapter | SnowflakeNativeAdapter


def toolchain_digest(platform: str, target_kind: TargetKind = "databricks") -> str:
    """The executing toolchain: the adapter of `target_kind`, its version, its pinned CLI version
    and the platform (`fake`, or the real platform)."""
    if target_kind == "snowflake":
        return digest(
            {
                "adapter": "snowflake",
                "adapter_version": snowflake_source.ADAPTER_VERSION,
                "cli_version": snowflake_source.CLI_VERSION,
                "platform": "fake" if platform == "fake" else "snowflake",
            }
        )
    return digest(
        {"adapter_version": ADAPTER_VERSION, "cli_version": CLI_VERSION, "platform": platform}
    )


def application_id(target_id: UUID, application_key: str) -> UUID:
    """Stable per target and application key, so redeployments keep the native app names."""
    return uuid5(APPLICATION_NAMESPACE, f"{target_id}/{application_key}")


class ReleaseArtifactStore(InMemoryArtifactStore):
    """The plan's artifact, digest-checked as a whole; read back without `release.json`, so the
    adapter validates and deploys the application source only."""

    def read(self, reference: SourceArchive, tenant_id: UUID) -> bytes:
        data = super().read(reference, tenant_id)
        files = read_archive(data)
        if files.pop(RELEASE_ENTRY, None) is None:
            return data
        return create_archive(files)


class Platform(Protocol):
    kind: str

    def adapter(self, target: Target, token_provider: TokenProvider) -> DatabricksAdapter: ...

    def snowflake_adapter(
        self, target: Target, credential_provider: CredentialProvider
    ) -> SnowflakeNativeAdapter: ...

    def fabric_adapter(
        self, target: Target, credential_provider: FabricCredentialProvider
    ) -> FabricAdapter: ...

    def palantir_adapter(
        self, target: Target, credential_provider: FoundryCredentialProvider
    ) -> PalantirMarketplaceAdapter: ...

    def docker_adapter(self, target: Target, artifacts: ArtifactStore) -> DockerComposeAdapter: ...


class RealPlatform:
    """A Databricks workspace (the pinned CLI writes, httpx reads, tokens from the broker) or a
    Snowflake account (SQL API statements and the pinned CLI's stage copy, key pairs from the
    broker)."""

    kind = "databricks"

    def adapter(self, target: Target, token_provider: TokenProvider) -> DatabricksAdapter:
        return DatabricksAdapter(
            cli=DatabricksCLI(token_provider=token_provider),
            workspace_factory=lambda t: DatabricksWorkspaceGateway(
                t, token_provider=token_provider
            ),
        )

    def snowflake_adapter(
        self, target: Target, credential_provider: CredentialProvider
    ) -> SnowflakeNativeAdapter:
        return SnowflakeNativeAdapter(
            gateway_factory=lambda t: SnowflakeSqlApi(t, credential_provider=credential_provider),
            copier_factory=lambda t: SnowflakeStageCopier(
                t, credential_provider=credential_provider
            ),
        )

    def fabric_adapter(
        self, target: Target, credential_provider: FabricCredentialProvider
    ) -> FabricAdapter:
        """Fabric REST calls with Entra client-credentials tokens; the secret is read from the
        broker again whenever a token is fetched."""
        ids = credential_provider()
        tokens = EntraTokenProvider(
            ids.entra_tenant_id, ids.client_id, lambda: credential_provider().client_secret
        )
        return FabricAdapter(client_factory=lambda t: FabricClient(t, tokens))

    def palantir_adapter(
        self, target: Target, credential_provider: FoundryCredentialProvider
    ) -> PalantirMarketplaceAdapter:
        return PalantirMarketplaceAdapter(
            client_factory=lambda t: PalantirClient(t, credential_provider=credential_provider)
        )

    def docker_adapter(self, target: Target, artifacts: ArtifactStore) -> DockerComposeAdapter:
        return DockerComposeAdapter(DockerCliRuntime(), artifacts=artifacts)


class FakePlatform:
    """One in-memory fake per platform for the process: its state outlives a job, so observe sees
    what apply deployed. The Databricks fake accepts writes by the job's service principal and
    treats the target's bound warehouse as existing. The Snowflake fake is built on first use with
    `environment` (the executor's `SHIFDE_ENV`; it refuses anything but development and test) and
    the public key of the job's key pair, so it accepts the JWTs signed with the entry's key.
    The Fabric workspace, Foundry stack (for the first target's host, frontend origin and
    ontology) and Docker daemon (for the first target's daemon id; it refuses outside development
    and test) fakes are each built on first use and kept for the process."""

    kind = "fake"

    def __init__(
        self,
        fake: FakeDatabricksPlatform | None = None,
        *,
        snowflake: FakeSnowflakeNativePlatform | None = None,
        fabric: FakeFabricWorkspace | None = None,
        palantir: FakeFoundryStack | None = None,
        docker: FakeContainerRuntime | None = None,
        environment: str = "",
    ) -> None:
        self.fake = fake or FakeDatabricksPlatform(service_principal="unset")
        self.snowflake = snowflake
        self.fabric = fabric
        self.palantir = palantir
        self.docker = docker
        self.environment = environment

    def adapter(self, target: Target, token_provider: TokenProvider) -> DatabricksAdapter:
        self.fake.service_principal = target.bindings[PRINCIPAL_BINDING]
        warehouse = target.bindings.get(WAREHOUSE_BINDING)
        if warehouse:
            self.fake.warehouses.add(warehouse)
        return DatabricksAdapter(cli=self.fake, workspace_factory=self.fake.workspace)

    def snowflake_fake(self, credential: SnowflakeKeyPair) -> FakeSnowflakeNativePlatform:
        """The process's Snowflake fake, built for `credential`'s key, account and user on first
        use."""
        if self.snowflake is None:
            key = snowflake_jwt.private_key(credential.private_key_pem)
            if isinstance(key, str):
                raise ValueError(key)
            self.snowflake = FakeSnowflakeNativePlatform(
                environment=self.environment,
                public_key=key.public_key(),
                account=credential.account,
                user=credential.user,
            )
        return self.snowflake

    def snowflake_adapter(
        self, target: Target, credential_provider: CredentialProvider
    ) -> SnowflakeNativeAdapter:
        return SnowflakeNativeAdapter(
            gateway_factory=lambda t: self.snowflake_fake(credential_provider()).gateway(
                t, credential_provider
            ),
            copier_factory=lambda t: self.snowflake_fake(credential_provider()).copier(t),
        )

    def fabric_adapter(
        self, target: Target, credential_provider: FabricCredentialProvider
    ) -> FabricAdapter:
        if self.fabric is None:
            self.fabric = FakeFabricWorkspace()
        return FabricAdapter(
            client_factory=self.fabric.client_factory, env=self.environment or None
        )

    def palantir_adapter(
        self, target: Target, credential_provider: FoundryCredentialProvider
    ) -> PalantirMarketplaceAdapter:
        if self.palantir is None:
            self.palantir = FakeFoundryStack(
                host=target.host,
                frontend_origin=target.bindings["frontend_origin"],
                ontology_rid=target.bindings["ontology_rid"],
                object_type=target.bindings.get("object_type") or "Example",
            )
        return PalantirMarketplaceAdapter(client_factory=self.palantir.client_factory)

    def docker_adapter(self, target: Target, artifacts: ArtifactStore) -> DockerComposeAdapter:
        if self.docker is None:
            daemon = DOCKER_HOST.fullmatch(target.host)
            if daemon is None:
                raise ValueError("the target is not a Docker daemon")
            self.docker = FakeContainerRuntime(daemon.group(1))
        return DockerComposeAdapter(self.docker, artifacts=artifacts)


class JobFailure(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code)
        self.code: ErrorCode = code


class ArtifactMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("the artifact is not the plan's artifact")


@dataclass(frozen=True)
class Prepared:
    """A job's platform half: the target as its adapter sees it, the application spec, the
    adapter, its version and the owned resources as adapter bindings."""

    target: Target
    spec: DatabricksApplicationSpec | SnowflakeApplicationSpec
    adapter_version: str
    adapter: Adapter
    owned: tuple[ResourceBinding, ...]


@dataclass
class LeaseState:
    """One running job's lease: renewed by the heartbeat, lost on a 409 or once it ran out."""

    lease: JobLease
    lease_until: datetime
    grant: CredentialGrant | None = None
    lost: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def guard(self) -> None:
        """The deploy context's `write_guard`: no platform write without a live lease."""
        if self.lost or utcnow() >= self.lease_until:
            self.lost = True
            raise LeaseLost


class GatewayJournal:
    """The adapter's `Journal` over gateway checkpoints. Each step runs once per job: `intent`
    returns only after the gateway accepted it (a refusal or transport error raises, so no write
    follows), and `complete` posts the receipt."""

    def __init__(
        self,
        client: GatewayClient,
        state: LeaseState,
        *,
        tenant_id: UUID,
        operation_id: UUID,
        platform: str,
    ) -> None:
        self.client, self.state, self.platform = client, state, platform
        self.tenant_id, self.operation_id = tenant_id, operation_id
        self.sequence = 0
        self.steps: dict[str, JournalEntry] = {}
        self.in_flight: str | None = None  # a step whose `intent` was accepted, not yet complete

    def _post(
        self, key: str, state: Literal["intent", "complete"], receipt: dict[str, Any] | None
    ) -> JournalEntry:
        self.sequence += 1
        try:
            return self.client.checkpoint(
                self.state.lease,
                sequence=self.sequence,
                step_key=key,
                state=state,
                input_digest=self.steps[key].input_digest,
                receipt=receipt,
            )
        except LeaseLost:
            self.state.lost = True
            raise

    def intent(self, key: str, inputs: dict[str, Any]) -> bool:
        if key in self.steps:
            raise NeedsAttention(f"step {key} was already started by this job")
        self.steps[key] = JournalEntry(step_key=key, input_digest=digest(inputs), state="intent")
        if self._post(key, "intent", None).state != "intent":
            raise NeedsAttention(f"step {key} is already complete; it is never replayed")
        self.in_flight = key
        return True

    def complete(self, key: str, receipt: dict[str, Any]) -> None:
        sent = {**receipt, "platform": "fake"} if self.platform == "fake" else receipt
        self._post(key, "complete", sent)
        self.in_flight = None
        self.steps[key] = self.steps[key].model_copy(
            update={"state": "complete", "receipt": receipt}
        )

    def completed(self, key: str) -> dict[str, Any] | None:
        entry = self.steps.get(key)
        return entry.receipt if entry and entry.state == "complete" else None

    def has_intent(self, key: str) -> bool:
        return key in self.steps


class Runner:
    def __init__(
        self,
        client: GatewayClient,
        broker: CredentialBroker,
        executor_id: UUID,
        platform: Platform,
        *,
        tenant_id: UUID,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.client, self.broker, self.executor_id = client, broker, executor_id
        self.tenant_id = tenant_id  # the executor's organization, pinned at start
        self.platform = platform
        self.heartbeat_seconds, self.poll_seconds = heartbeat_seconds, poll_seconds
        self.toolchain_digest = toolchain_digest(platform.kind)
        self._stop = threading.Event()
        self.last_tick = time.monotonic()
        self._state: LeaseState | None = None  # the job in flight
        self._journal: GatewayJournal | None = None  # its journal, for an apply

    def stop(self) -> None:
        self._stop.set()

    def window(self) -> float:
        """How old a tick may be: three polls, or three heartbeats while a job is in flight (the
        run loop does not pass during a long step)."""
        return 3 * (self.heartbeat_seconds if self._state is not None else self.poll_seconds)

    def live(self) -> bool:
        return time.monotonic() - self.last_tick <= self.window()

    def ready(self) -> bool:
        last = self.client.last_success
        return (
            not self._stop.is_set()
            and last is not None
            and time.monotonic() - last <= self.window()
        )

    def in_flight(self) -> bool:
        return self._state is not None

    def write_in_flight(self) -> bool:
        """A platform write of the job in flight was accepted (`intent`) and not yet complete."""
        journal = self._journal
        return journal is not None and journal.in_flight is not None

    def abandon(self) -> None:
        """The shutdown grace ran out: post nothing more for the job in flight (no result, no
        cancel); the server decides it once its lease expires."""
        state = self._state
        if state is not None:
            state.lost = True

    def run_forever(self) -> None:
        """Lease and run jobs until `stop()`; sleeps `poll_seconds` when none is due."""
        while not self._stop.is_set():
            self.last_tick = time.monotonic()
            try:
                ran = self.run_once()
            except Exception as exc:
                log.warning("executor poll failed (%s)", type(exc).__name__)
                ran = False
            if not ran:
                self._stop.wait(self.poll_seconds)

    def run_once(self) -> bool:
        """Lease at most one job and run it; whether one ran."""
        lease = self.client.lease(KINDS)
        if lease is None:
            return False
        self._run(lease)
        return True

    # ------------------------------------------------------------------ one job
    def _heartbeat(self, state: LeaseState) -> None:
        while not state.done.wait(self.heartbeat_seconds):
            self.last_tick = time.monotonic()  # a run-loop tick while the job is in flight
            try:
                state.lease_until = self.client.heartbeat(state.lease).lease_until
            except LeaseLost:
                state.lost = True
                return
            except Exception as exc:
                log.warning("heartbeat failed (%s)", type(exc).__name__)

    def _run(self, lease: JobLease) -> None:
        state = self._state = LeaseState(lease=lease, lease_until=lease.lease_until)
        try:
            self._run_leased(state)
        finally:
            self._state = self._journal = None

    def _run_leased(self, state: LeaseState) -> None:
        lease = state.lease
        job = lease.job
        beat = threading.Thread(target=self._heartbeat, args=(state,), daemon=True)
        beat.start()
        log.info("running %s job %s", job.kind, job.id)
        value: BaseModel | None = None
        code: ErrorCode | None = None
        try:
            value = self._execute(state)
        except LeaseLost:
            state.lost = True
        except (JobFailure, CredentialUnavailable) as exc:
            code = exc.code if isinstance(exc, JobFailure) else "credential_unavailable"
        except Exception as exc:
            log.warning("%s job %s failed (%s)", job.kind, job.id, type(exc).__name__)
            code = "execution_failed"
        finally:
            state.done.set()
            beat.join()
        if state.lost:
            log.warning("%s job %s lost its lease; no result is posted", job.kind, job.id)
            return
        try:
            if value is not None:
                self.client.result(lease, value=value.model_dump(mode="json"))
            else:
                self.client.result(lease, error_code=code or "execution_failed")
        except Exception as exc:
            log.warning("result of %s job %s refused (%s)", job.kind, job.id, type(exc).__name__)
            return
        log.info(
            "%s job %s %s",
            job.kind,
            job.id,
            "succeeded" if value is not None else f"failed ({code})",
        )

    def _token_provider(self, state: LeaseState, target: Target) -> TokenProvider:
        def token(host: str) -> str:
            grant = state.grant
            if grant is None or grant.expires_at <= utcnow():
                grant = state.grant = self.client.credential_grant(state.lease)
            try:
                audience = normalize_databricks_address(host)
            except AddressRefused:
                raise CredentialUnavailable from None
            if audience != grant.audience:
                raise CredentialUnavailable
            return self.broker.token_for(
                grant,
                job_id=state.lease.job.id,
                executor_id=self.executor_id,
                target_id=target.id,
                tenant_id=target.tenant_id,
            )

        return token

    def _key_pair_provider(self, state: LeaseState, target: Target) -> CredentialProvider:
        """The Snowflake key pair, re-checked by the broker (and its key file read) on every call;
        an expired grant is refreshed first."""

        def key_pair() -> SnowflakeKeyPair:
            grant = state.grant
            if grant is None or grant.expires_at <= utcnow():
                grant = state.grant = self.client.credential_grant(state.lease)
            return self.broker.key_pair_for(
                grant,
                job_id=state.lease.job.id,
                executor_id=self.executor_id,
                target_id=target.id,
                tenant_id=target.tenant_id,
            )

        return key_pair

    def _execute(self, state: LeaseState) -> BaseModel:
        lease = state.lease
        job = lease.job
        ctx = self.client.context(lease)
        if ctx.plan is None or job.target_id != ctx.target.id:
            raise ValueError("the job has no plan or names another target")
        if ctx.job.id != job.id:
            raise ValueError("the context names another job")
        tenant = ctx.target.tenant_id
        if (
            tenant != self.tenant_id
            or ctx.plan.tenant_id != tenant
            or (ctx.deployment is not None and ctx.deployment.tenant_id != tenant)
        ):
            raise ValueError("the context names another organization")
        grant = state.grant = self.client.credential_grant(lease)
        kind = ctx.platform_kind
        app_id = application_id(ctx.target.id, ctx.plan.application_key)
        prepared = DISPATCH[kind](self, state, ctx, grant, app_id)
        target, spec, adapter = prepared.target, prepared.spec, prepared.adapter
        adapter_version, owned = prepared.adapter_version, prepared.owned
        toolchain = toolchain_digest(self.platform.kind, kind)
        deadline = job.deadline or utcnow() + timedelta(hours=1)
        read = ReadContext(
            tenant_id=target.tenant_id, target=target, owned_resources=owned, deadline=deadline
        )
        build = (spec, adapter_version, toolchain)
        if job.kind == "plan":
            store = ReleaseArtifactStore()
            artifact = self._artifact(state, ctx.plan, target, store, app_id, build)
            planned = adapter.plan(PlanRequest(artifact=artifact), read)
            return PlanJobResult(
                steps=planned.steps,
                snapshot=planned.snapshot,
                adapter_version=planned.adapter_version,
                toolchain_digest=planned.toolchain_digest,
            )
        if job.kind == "apply":
            if ctx.deployment is None:
                raise ValueError("the apply job has no deployment")
            store = ReleaseArtifactStore()
            artifact = self._artifact(state, ctx.plan, target, store, app_id, build)
            plan = self._plan(ctx, target, app_id, spec, self._details(adapter, ctx, read, app_id))
            journal = GatewayJournal(
                self.client,
                state,
                tenant_id=target.tenant_id,
                operation_id=ctx.deployment.id,
                platform=self.platform.kind,
            )
            self._journal = journal
            deploy = DeployContext(
                tenant_id=target.tenant_id,
                target=target,
                owned_resources=owned,
                deadline=deadline,
                operation_id=ctx.deployment.id,
                artifacts=store,
                journal=journal,
                toolchain_digest=toolchain,
                write_guard=state.guard,
            )
            receipt = adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
            if self.platform.kind == "fake":
                receipt = receipt.model_copy(
                    update={"operation_handles": {**receipt.operation_handles, "platform": "fake"}}
                )
            return receipt
        if job.kind == "reconcile":
            # The Snowflake adapter's reconcile reads no plan details, and after an unknown
            # outcome the native state no longer gives the planned ones.
            details = {} if kind == "snowflake" else self._details(adapter, ctx, read, app_id)
            plan = self._plan(ctx, target, app_id, spec, details)
            return adapter.reconcile(ReconcileRequest(plan=plan), read)
        if ctx.deployment is None or ctx.deployment.receipt is None:
            raise ValueError("the job's deployment has no receipt")
        receipt = ctx.deployment.receipt
        if job.kind == "observe":
            return adapter.observe(ObserveRequest(receipt=receipt), read)
        # a generated app's deep health must answer the plan's release digest (SP5 6.6); a
        # generated app's plan without one fails closed rather than skip deep health
        release_digest = None
        if ctx.plan.generator_version is not None:
            if ctx.plan.release_digest is None:
                raise ValueError("the generated app's plan has no release digest")
            release_digest = ctx.plan.release_digest
        return adapter.verify(VerifyRequest(receipt=receipt, release_digest=release_digest), read)

    # ------------------------------------------------------------------ dispatch (DISPATCH)
    def _grant_ids(self, state: LeaseState, ctx: JobContext) -> dict[str, UUID]:
        """The broker's per-grant identities: this job, this executor, the context's target and
        its organization."""
        return {
            "job_id": state.lease.job.id,
            "executor_id": self.executor_id,
            "target_id": ctx.target.id,
            "tenant_id": ctx.target.tenant_id,
        }

    def _databricks(
        self, state: LeaseState, ctx: JobContext, grant: CredentialGrant, app_id: UUID
    ) -> Prepared:
        entry = self.broker.entry_for(grant, **self._grant_ids(state, ctx))
        target = ctx.target.model_copy(
            update={"bindings": {**ctx.target.bindings, PRINCIPAL_BINDING: entry.service_principal}}
        )
        spec = DatabricksApplicationSpec()
        return Prepared(
            target=target,
            spec=spec,
            adapter_version=ADAPTER_VERSION,
            adapter=self.platform.adapter(target, self._token_provider(state, target)),
            owned=tuple(
                ResourceBinding(
                    key=spec.app_resource_key,
                    native_id=r.native_id,
                    resource_type=r.resource_type,
                    application_id=app_id,
                    target_id=target.id,
                )
                for r in ctx.owned_resources
            ),
        )

    def _snowflake(
        self, state: LeaseState, ctx: JobContext, grant: CredentialGrant, app_id: UUID
    ) -> Prepared:
        self.broker.snowflake_entry_for(grant, **self._grant_ids(state, ctx))
        target = ctx.target
        return Prepared(
            target=target,
            spec=SnowflakeApplicationSpec(),
            adapter_version=snowflake_source.ADAPTER_VERSION,
            adapter=self.platform.snowflake_adapter(target, self._key_pair_provider(state, target)),
            owned=tuple(
                ResourceBinding(
                    key=_snowflake_key(r.resource_type),
                    native_id=r.native_id,
                    resource_type=r.resource_type,
                    application_id=app_id,
                    target_id=target.id,
                )
                for r in ctx.owned_resources
            ),
        )

    # SP8-1: the platform's broker check (a mismatch is `credential_unavailable`), then fail closed
    # before any adapter or platform call and any journal intent; SP8-5 (Fabric), SP8-7
    # (Palantir) and SP8-9 (Docker) replace each with its call path.
    def _fabric(
        self, state: LeaseState, ctx: JobContext, grant: CredentialGrant, app_id: UUID
    ) -> Prepared:
        self.broker.fabric_entry_for(grant, **self._grant_ids(state, ctx))
        raise JobFailure("execution_failed")

    def _palantir(
        self, state: LeaseState, ctx: JobContext, grant: CredentialGrant, app_id: UUID
    ) -> Prepared:
        self.broker.palantir_entry_for(grant, **self._grant_ids(state, ctx))
        raise JobFailure("execution_failed")

    def _docker(
        self, state: LeaseState, ctx: JobContext, grant: CredentialGrant, app_id: UUID
    ) -> Prepared:
        self.broker.docker_entry_for(grant, **self._grant_ids(state, ctx))
        raise JobFailure("execution_failed")

    def _artifact(
        self,
        state: LeaseState,
        plan: ContextPlan,
        target: Target,
        store: ReleaseArtifactStore,
        app_id: UUID,
        build: tuple[DatabricksApplicationSpec | SnowflakeApplicationSpec, str, str],
    ) -> BuildArtifact:
        """The plan's artifact (digest-checked against the header and the plan) in `store`, for
        `build` (spec, adapter version, toolchain digest)."""
        spec, adapter_version, toolchain = build
        data = self.client.artifact(state.lease)
        if hashlib.sha256(data).hexdigest() != plan.artifact_digest:
            raise ArtifactMismatch
        archive = store.put(target.tenant_id, data)
        return BuildArtifact(
            id=plan.artifact_id,
            tenant_id=target.tenant_id,
            application_id=app_id,
            archive=archive,
            source_digest=archive.digest,
            spec_digest=digest(spec),
            spec=spec,
            adapter_version=adapter_version,
            toolchain_digest=toolchain,
        )

    @staticmethod
    def _details(
        adapter: Adapter, ctx: JobContext, read: ReadContext, app_id: UUID
    ) -> dict[str, Any]:  # Any: plan details JSON
        """The details the adapter sealed into the plan: for Snowflake from the adapter's own
        helper on the current native state, for Databricks derived from the target."""
        if ctx.plan is None:
            raise ValueError("the job has no plan")
        if isinstance(adapter, SnowflakeNativeAdapter):
            return adapter.plan_details(ctx.plan.artifact_digest, app_id, read)
        target = read.target
        warehouse = target.bindings.get(DatabricksApplicationSpec().warehouse_binding)
        if not warehouse:
            raise ValueError("required warehouse binding is missing")
        return {
            "cli_version": CLI_VERSION,
            "app_name": app_name(app_id, target.id),
            "root_path": root_path(target.bindings[PRINCIPAL_BINDING], app_id, target.id),
            "warehouse_id": warehouse,
        }

    @staticmethod
    def _plan(
        ctx: JobContext,
        target: Target,
        app_id: UUID,
        spec: DatabricksApplicationSpec | SnowflakeApplicationSpec,
        details: dict[str, Any],  # Any: plan details JSON
    ) -> DeploymentPlan:
        """The sealed plan as the adapter's `DeploymentPlan`: the server's steps, snapshot,
        versions and expiry, the adapter's details, sealed with the local digest."""
        p = ctx.plan
        if (
            p is None
            or p.digest is None
            or p.steps is None
            or p.snapshot is None
            or p.adapter_version is None
            or p.toolchain_digest is None
            or p.expires_at is None
        ):
            raise ValueError("the job's plan is not sealed")
        return seal_plan(
            DeploymentPlan(
                id=p.id,
                tenant_id=target.tenant_id,
                application_id=app_id,
                target_id=target.id,
                target_revision=p.target_revision,
                artifact_id=p.artifact_id,
                artifact_digest=p.artifact_digest,
                spec_digest=digest(spec),
                bindings_digest=bindings_digest(target),
                adapter_version=p.adapter_version,
                toolchain_digest=p.toolchain_digest,
                snapshot=p.snapshot,
                steps=p.steps,
                details=details,
                expires_at=p.expires_at,
                digest="0" * 64,
            )
        )


# the job context's platform kind -> the runner step that checks its credential and builds its
# adapter (or fails closed)
DISPATCH: dict[
    TargetKind, Callable[[Runner, LeaseState, JobContext, CredentialGrant, UUID], Prepared]
] = {
    "databricks": Runner._databricks,
    "snowflake": Runner._snowflake,
    "fabric": Runner._fabric,
    "palantir": Runner._palantir,
    "docker": Runner._docker,
}


def _snowflake_key(resource_type: str) -> str:
    """The Snowflake adapter's resource key of an owned resource type."""
    key = RESOURCE_KEY_OF_TYPE.get(resource_type)
    if key is None:
        raise ValueError("the target owns a resource the Snowflake adapter does not bind")
    return key
