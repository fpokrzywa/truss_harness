"""The runner: lease one job, run it through the Databricks adapter, post its result (design 4
and 5, settled calls 5, 8 and 14; FDE SDK `shifde_executor.runner`, synchronous, one job at a time;
B-2688..B-2695).

Every job reads its context (target, plan, deployment, owned resources) and a credential grant
under its lease; a grant the broker refuses ends the job `credential_unavailable` before any
adapter call. The deploying service principal comes from the credential entry, not the target.
While a job runs it is heartbeated in the background; a 409 marks the lease lost.

`apply` writes at most once: before each platform write the adapter's journal posts an `intent`
checkpoint and writes only once the gateway acknowledged it; afterwards it posts `complete`.
Any exception ends the job with one failed result `execution_failed` (none once the lease is
lost: the server's sweep decides `unknown_outcome` or `failed`). Nothing is retried here; the
server re-queues read-only jobs after a lost lease. A failed result never carries error text.

The platform is real (`RealPlatform`: pinned CLI and workspace REST reads with tokens from the
broker) or the in-memory fake (`FakePlatform`, one per process). The toolchain digest names the
platform, so a plan made on the fake is never applied by a real executor, and the fake marks
every `complete` receipt and the apply receipt's handles with `platform: fake`.

Process control (SP4 design 6.2): `stop()` requests no new lease, while the job in flight runs to
its normal end, result included. `last_tick` is the monotonic time of the last run-loop pass (or
lease heartbeat while a job is in flight) and `live()` / `ready()` hold while it, and the client's
last successful gateway call, are within `window()`. `abandon()` (the shutdown grace ran out)
posts nothing more for the job in flight; `write_in_flight()` tells whether a platform write was.

A plan's artifact is the SDK source plus a root `release.json`, the release it deploys (SP3-4).
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

from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.adapters.native.base import (
    DeployContext,
    InMemoryArtifactStore,
    NeedsAttention,
    ReadContext,
    create_archive,
    read_archive,
)
from shifde.adapters.native.contracts import (
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
from shifde.adapters.native.planning import bindings_digest, seal_plan
from shifde.executor.client import GatewayClient, LeaseLost
from shifde.executor.credentials import CredentialBroker, CredentialUnavailable

log = logging.getLogger(__name__)

KINDS: list[JobKind] = ["plan", "apply", "observe", "verify", "reconcile"]
HEARTBEAT_SECONDS = 15.0  # against the gateway's 45 s lease
POLL_SECONDS = 2.0
# application_id = uuid5(namespace, "<target_id>/<application_key>") (B-2690)
APPLICATION_NAMESPACE = UUID("6f1c2a4e-8d3b-5e7f-9a10-b2c3d4e5f607")
RELEASE_ENTRY = "release.json"  # the release a plan deploys, at the artifact's root (SP3-4)

TokenProvider = Callable[[str], str]
ErrorCode = Literal["execution_failed", "credential_unavailable"]


def toolchain_digest(platform: str) -> str:
    """The executing toolchain: adapter version, pinned CLI version and platform kind."""
    return digest(
        {"adapter_version": ADAPTER_VERSION, "cli_version": CLI_VERSION, "platform": platform}
    )


def application_id(target_id: UUID, application_key: str) -> UUID:
    """Stable per target and application key, so redeployments keep the Databricks app name."""
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


class RealPlatform:
    """A Databricks workspace: the pinned CLI writes, httpx reads, tokens from the broker."""

    kind = "databricks"

    def adapter(self, target: Target, token_provider: TokenProvider) -> DatabricksAdapter:
        return DatabricksAdapter(
            cli=DatabricksCLI(token_provider=token_provider),
            workspace_factory=lambda t: DatabricksWorkspaceGateway(
                t, token_provider=token_provider
            ),
        )


class FakePlatform:
    """One in-memory fake for the process: its state outlives a job, so observe sees what apply
    deployed. It accepts writes by the job's service principal and treats the target's bound
    warehouse as existing."""

    kind = "fake"

    def __init__(self, fake: FakeDatabricksPlatform | None = None) -> None:
        self.fake = fake or FakeDatabricksPlatform(service_principal="unset")

    def adapter(self, target: Target, token_provider: TokenProvider) -> DatabricksAdapter:
        self.fake.service_principal = target.bindings[PRINCIPAL_BINDING]
        warehouse = target.bindings.get(WAREHOUSE_BINDING)
        if warehouse:
            self.fake.warehouses.add(warehouse)
        return DatabricksAdapter(cli=self.fake, workspace_factory=self.fake.workspace)


class JobFailure(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code)
        self.code: ErrorCode = code


class ArtifactMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("the artifact is not the plan's artifact")


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
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.client, self.broker, self.executor_id = client, broker, executor_id
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

    def _execute(self, state: LeaseState) -> BaseModel:
        lease = state.lease
        job = lease.job
        ctx = self.client.context(lease)
        if ctx.plan is None or job.target_id != ctx.target.id:
            raise ValueError("the job has no plan or names another target")
        if ctx.job.id != job.id:
            raise ValueError("the context names another job")
        state.grant = self.client.credential_grant(lease)
        entry = self.broker.entry_for(
            state.grant,
            job_id=job.id,
            executor_id=self.executor_id,
            target_id=ctx.target.id,
            tenant_id=ctx.target.tenant_id,
        )
        target = ctx.target.model_copy(
            update={"bindings": {**ctx.target.bindings, PRINCIPAL_BINDING: entry.service_principal}}
        )
        spec = DatabricksApplicationSpec()
        app_id = application_id(target.id, ctx.plan.application_key)
        adapter = self.platform.adapter(target, self._token_provider(state, target))
        owned = tuple(
            ResourceBinding(
                key=spec.app_resource_key,
                native_id=r.native_id,
                resource_type=r.resource_type,
                application_id=app_id,
                target_id=target.id,
            )
            for r in ctx.owned_resources
        )
        deadline = job.deadline or utcnow() + timedelta(hours=1)
        read = ReadContext(
            tenant_id=target.tenant_id, target=target, owned_resources=owned, deadline=deadline
        )
        if job.kind == "plan":
            store = ReleaseArtifactStore()
            artifact = self._artifact(state, ctx.plan, target, store, app_id, spec)
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
            artifact = self._artifact(state, ctx.plan, target, store, app_id, spec)
            plan = self._plan(ctx, target, app_id, spec)
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
                toolchain_digest=self.toolchain_digest,
                write_guard=state.guard,
            )
            receipt = adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
            if self.platform.kind == "fake":
                receipt = receipt.model_copy(
                    update={"operation_handles": {**receipt.operation_handles, "platform": "fake"}}
                )
            return receipt
        if job.kind == "reconcile":
            plan = self._plan(ctx, target, app_id, spec)
            return adapter.reconcile(ReconcileRequest(plan=plan), read)
        if ctx.deployment is None or ctx.deployment.receipt is None:
            raise ValueError("the job's deployment has no receipt")
        receipt = ctx.deployment.receipt
        if job.kind == "observe":
            return adapter.observe(ObserveRequest(receipt=receipt), read)
        return adapter.verify(VerifyRequest(receipt=receipt), read)

    def _artifact(
        self,
        state: LeaseState,
        plan: ContextPlan,
        target: Target,
        store: ReleaseArtifactStore,
        app_id: UUID,
        spec: DatabricksApplicationSpec,
    ) -> BuildArtifact:
        """The plan's artifact (digest-checked against the header and the plan) in `store`."""
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
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=self.toolchain_digest,
        )

    @staticmethod
    def _plan(
        ctx: JobContext, target: Target, app_id: UUID, spec: DatabricksApplicationSpec
    ) -> DeploymentPlan:
        """The sealed plan as the adapter's `DeploymentPlan`: the server's steps, snapshot,
        versions and expiry, the details derived from the target, sealed with the local digest."""
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
        warehouse = target.bindings.get(spec.warehouse_binding)
        if not warehouse:
            raise ValueError("required warehouse binding is missing")
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
                details={
                    "cli_version": CLI_VERSION,
                    "app_name": app_name(app_id, target.id),
                    "root_path": root_path(target.bindings[PRINCIPAL_BINDING], app_id, target.id),
                    "warehouse_id": warehouse,
                },
                expires_at=p.expires_at,
                digest="0" * 64,
            )
        )
