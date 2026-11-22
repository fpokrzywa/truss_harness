"""The executor gateway's job queue, leases, fences and step journal (design sections 4 and 5,
section 9 items 4, 5, 7, 8, 9 and 16).

A job is leased only through the `shifde.delivery_lease_job` definer (the app holds no grant on
the lease columns), after `shifde.delivery_sweep_lost_jobs` has settled the organization's
expired leases: an `apply` job is never re-queued (it becomes `lost`, with an unknown outcome when
its deployment journal holds any step); other kinds are re-queued up to `max_attempts`. A
`BEFORE UPDATE` trigger refuses reviving a lease without a new fence. `service.settle_plans` then
fails the plans of lost plan jobs and expires sealed plans past `expires_at`, and
`service.settle_deployments` applies the deployment settle rules (B-2683). Leasing an `apply` job
moves its deployment to `leased`; its first accepted `intent` moves it to `applying`.

Every per-job call proves the current lease (`lease_id` and `fence`). A job of another
organization, or one that does not exist, answers the same 404; a lease that is not the caller's
current one answers 409 `lease_lost`, and nothing revives an expired lease. The journal mirrors
the SDK journal: intent before write, no changed input digest, no conflicting receipt. A refusal
changes nothing (the transaction is rolled back). Times are the database clock's.

`JOB_KINDS` maps a job kind to its result model and an `on_result` hook run in the result's
transaction (settled call 5): `plan` (`service.on_plan_result` seals or fails the plan), `apply`
(`ApplyReceipt`), `observe` (`NativeObservation`), `verify` (`VerificationReport`) and `reconcile`
(`ReconciliationResult`), each with its `service.on_*_result` deployment hook; a result for a
kind without an entry is refused. A leased `plan` job serves its plan's artifact (`artifact`).
`job_context` answers what a leased job runs against (target, plan, deployment, owned resources;
B-2688), so the executor can drive the adapter from ids alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any  # Any: job and journal rows, JSON documents
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from shifde.adapters.native.contracts import (
    ApplyReceipt,
    CheckpointSubmission,
    ContextDeployment,
    ContextPlan,
    CredentialGrant,
    HeartbeatAck,
    JobContext,
    JobLease,
    JobResultAck,
    JobResultSubmission,
    JournalEntry,
    LeasedJob,
    LeaseProof,
    LeaseRequest,
    NativeObservation,
    OwnedResource,
    PlanJobResult,
    ReconciliationResult,
    Target,
    VerificationReport,
)
from shifde.core.errors import AppError, NotFound, ValidationFailed
from shifde.delivery import repository, service
from shifde.delivery.service import AuthenticatedExecutor

Row = Mapping[str, Any]  # Any: SQLAlchemy RowMapping column values

LEASE_SECONDS = 45
GRANT_SECONDS = 30
READ_ONLY_MAX_ATTEMPTS = service.READ_ONLY_MAX_ATTEMPTS  # one lease plus up to 3 re-queues
APPLY_MAX_ATTEMPTS = service.APPLY_MAX_ATTEMPTS  # a platform write is never retried


@dataclass(frozen=True)
class JobKindDefinition:
    result_model: type[BaseModel]
    on_result: Callable[[Session, Row, JobResultSubmission], None] | None = None


JOB_KINDS: dict[str, JobKindDefinition] = {
    "plan": JobKindDefinition(PlanJobResult, service.on_plan_result),
    "apply": JobKindDefinition(ApplyReceipt, service.on_apply_result),
    "observe": JobKindDefinition(NativeObservation, service.on_observe_result),
    "verify": JobKindDefinition(VerificationReport, service.on_verify_result),
    "reconcile": JobKindDefinition(ReconciliationResult, service.on_reconcile_result),
}


@dataclass(frozen=True)
class Artifact:
    content: bytes
    digest: str


class LeaseLost(AppError):
    status, code, title = 409, "lease_lost", "Conflict"


class ArtifactNotFound(AppError):
    status, code, title = 404, "artifact_not_found", "Not Found"


class TargetUnavailable(AppError):
    status, code, title = 409, "target_unavailable", "Conflict"


class StaleSequence(AppError):
    status, code, title = 409, "stale_sequence", "Conflict"


class NoJournal(AppError):
    status, code, title = 409, "no_journal", "Conflict"


class InputDigestChanged(AppError):
    status, code, title = 409, "input_digest_changed", "Conflict"


class StepOutcomeUnknown(AppError):
    status, code, title = 409, "step_outcome_unknown", "Conflict"


class NoIntent(AppError):
    status, code, title = 409, "no_intent", "Conflict"


class ConflictingReceipt(AppError):
    status, code, title = 409, "conflicting_receipt", "Conflict"


class JobKindUnsupported(AppError):
    status, code, title = 409, "job_kind_unsupported", "Conflict"


def enqueue_job(
    session: Session,
    *,
    tenant_id: UUID,
    kind: str,
    plan_id: UUID | None = None,
    deployment_id: UUID | None = None,
    target_id: UUID | None = None,
    run_after: datetime | None = None,
    deadline: datetime | None = None,
) -> UUID:
    """Queue a job (the caller commits); `apply` gets one attempt, other kinds four. A `plan` job
    naming a plan must name a plan of the same organization and that plan's target (B-2732)."""
    return service.enqueue_job(
        session,
        tenant_id=tenant_id,
        kind=kind,
        plan_id=plan_id,
        deployment_id=deployment_id,
        target_id=target_id,
        run_after=run_after,
        deadline=deadline,
    )


def _leased(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, proof: LeaseProof
) -> Row:
    """The job, locked, if the proof is the caller's current unexpired lease."""
    row = repository.get_job(session, job_id, tenant_id=executor.tenant_id, for_update=True)
    if row is None or row["tenant_id"] != executor.tenant_id:
        raise NotFound("job not found")
    if (
        row["status"] != "leased"
        or row["executor_id"] != executor.executor_id
        or row["lease_id"] != proof.lease_id
        or row["fence"] != proof.fence
        or row["lease_until"] <= row["db_now"]
    ):
        raise LeaseLost("the lease is no longer held")
    return row


def _leased_job(row: Row) -> LeasedJob:
    return LeasedJob(
        id=row["id"],
        kind=row["kind"],
        plan_id=row["plan_id"],
        deployment_id=row["deployment_id"],
        target_id=row["target_id"],
        deadline=row["deadline"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


def lease(session: Session, executor: AuthenticatedExecutor, body: LeaseRequest) -> JobLease | None:
    """Sweep the organization's expired leases and settle its plans and deployments, then lease
    (or replay) one due job; a leased `plan` job moves its plan to `planning`, a leased `apply`
    job its deployment to `leased`."""
    repository.sweep_lost_jobs(session)
    service.settle_plans(session, executor.tenant_id)
    service.settle_deployments(session, executor.tenant_id)
    granted = repository.lease_job(session, executor.executor_id, body.request_id, body.kinds)
    if granted is None:
        session.commit()
        return None
    row = repository.get_job(session, granted["job_id"], tenant_id=executor.tenant_id)
    if row is None:  # pragma: no cover - the definer just leased it in this organization
        raise NotFound("job not found")
    service.start_planning(session, row)
    service.start_deployment(session, row)
    session.commit()
    return JobLease(
        job=_leased_job(row),
        lease_id=granted["lease_id"],
        fence=granted["fence"],
        lease_until=granted["lease_until"],
        request_id=body.request_id,
    )


def heartbeat(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, proof: LeaseProof
) -> HeartbeatAck:
    _leased(session, executor, job_id, proof)
    until = repository.extend_lease(session, job_id, LEASE_SECONDS, tenant_id=executor.tenant_id)
    session.commit()
    return HeartbeatAck(lease_until=until)


def artifact(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, proof: LeaseProof
) -> Artifact:
    """The artifact of the job's plan (bytes and sha256 digest); a job without a plan has none."""
    row = _leased(session, executor, job_id, proof)
    found = (
        repository.get_plan_artifact(session, row["plan_id"], tenant_id=executor.tenant_id)
        if row["plan_id"] is not None
        else None
    )
    if found is None:
        raise ArtifactNotFound("the job has no artifact")
    session.commit()
    return Artifact(content=bytes(found["content"]), digest=found["digest"])


def job_context(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, proof: LeaseProof
) -> JobContext:
    """What the job runs against: its target, its plan (sealed fields once sealed), its
    deployment (receipt once applied) and the native resources the plan's application owns on the
    target. Never a credential, the claim or address key, or the artifact bytes."""
    tenant_id = executor.tenant_id
    row = _leased(session, executor, job_id, proof)
    target = (
        repository.get_target(session, row["target_id"], tenant_id=tenant_id)
        if row["target_id"] is not None
        else None
    )
    if target is None or target["status"] != "active":
        raise TargetUnavailable("the job's target is not available")
    plan = (
        repository.get_plan(session, row["plan_id"], tenant_id=tenant_id)
        if row["plan_id"] is not None
        else None
    )
    deployment = (
        repository.get_deployment(session, row["deployment_id"], tenant_id=tenant_id)
        if row["deployment_id"] is not None
        else None
    )
    owned = (
        repository.list_native_resources(
            session, target["id"], plan["application_key"], tenant_id=tenant_id
        )
        if plan is not None
        else []
    )
    session.commit()
    return JobContext(
        job=_leased_job(row),
        target=Target(
            tenant_id=tenant_id,
            id=target["id"],
            revision=target["revision"],
            environment=target["environment"],
            host=target["address"],
            bindings=target["bindings"],
        ),
        plan=None
        if plan is None
        else ContextPlan(
            id=plan["id"],
            tenant_id=plan["tenant_id"],
            application_key=plan["application_key"],
            display_name=plan["display_name"],
            artifact_id=plan["artifact_id"],
            artifact_digest=plan["artifact_digest"],
            target_revision=plan["target_revision"],
            status=plan["status"],
            steps=plan["steps"],
            snapshot=plan["snapshot"],
            adapter_version=plan["adapter_version"],
            toolchain_digest=plan["toolchain_digest"],
            digest=plan["digest"],
            expires_at=plan["expires_at"],
        ),
        deployment=None
        if deployment is None
        else ContextDeployment(
            id=deployment["id"],
            tenant_id=deployment["tenant_id"],
            status=deployment["status"],
            receipt=deployment["receipt"],
        ),
        owned_resources=[
            OwnedResource(resource_type=r["resource_type"], native_id=r["native_id"]) for r in owned
        ],
    )


def credential_grant(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, proof: LeaseProof
) -> CredentialGrant:
    """Which credential the executor may use for the job's target, and until when (no secret)."""
    row = _leased(session, executor, job_id, proof)
    target = (
        repository.get_target(session, row["target_id"], tenant_id=executor.tenant_id)
        if row["target_id"] is not None
        else None
    )
    if target is None or target["status"] != "active":
        raise TargetUnavailable("the job's target is not available")
    expires = repository.grant_expiry(session, row["lease_until"], GRANT_SECONDS)
    session.commit()
    return CredentialGrant(
        job_id=job_id,
        executor_id=executor.executor_id,
        target_id=target["id"],
        credential_ref=target["credential_ref"],
        audience=target["address"],
        expires_at=expires,
    )


def _entry(row: Row) -> JournalEntry:
    return JournalEntry(
        step_key=row["step_key"],
        input_digest=row["input_digest"],
        state=row["state"],
        receipt=row["receipt"],
    )


def checkpoint(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, body: CheckpointSubmission
) -> JournalEntry:
    """Record a journal step: the intent before the platform write, the receipt after it."""
    tenant_id = executor.tenant_id
    row = _leased(session, executor, job_id, body)
    if body.sequence <= row["checkpoint_seq"]:
        raise StaleSequence("the checkpoint sequence is not newer than the last accepted one")
    deployment_id = row["deployment_id"]
    if deployment_id is None:
        raise NoJournal("the job has no deployment journal")
    current = repository.get_journal_entry(
        session, deployment_id, body.step_key, tenant_id=tenant_id
    )
    if current is not None and current["input_digest"] != body.input_digest:
        raise InputDigestChanged("the step's input digest changed")
    if body.state == "intent":
        if current is None:
            current = repository.insert_intent(
                session,
                {
                    "tenant_id": tenant_id,
                    "deployment_id": deployment_id,
                    "step_key": body.step_key,
                    "input_digest": body.input_digest,
                    "job_id": job_id,
                    "fence": body.fence,
                },
            )
        elif current["state"] == "intent" and (
            current["job_id"] != job_id or current["fence"] != body.fence
        ):
            raise StepOutcomeUnknown("an earlier attempt recorded this step's intent")
        service.start_applying(session, row)
    else:
        if current is None:
            raise NoIntent("no intent was recorded for the step")
        if current["state"] == "complete":
            if current["receipt"] != body.receipt:
                raise ConflictingReceipt("the step already has a different receipt")
        else:
            current = repository.complete_step(
                session, deployment_id, body.step_key, body.receipt or {}, tenant_id=tenant_id
            )
    repository.set_checkpoint_seq(session, job_id, body.sequence, tenant_id=tenant_id)
    session.commit()
    return _entry(current)


def submit_result(
    session: Session, executor: AuthenticatedExecutor, job_id: UUID, body: JobResultSubmission
) -> JobResultAck:
    """Finish the job and run its kind's `on_result` hook in the same transaction."""
    row = _leased(session, executor, job_id, body)
    definition = JOB_KINDS.get(row["kind"])
    if definition is None:
        raise JobKindUnsupported("this job kind takes no result yet")
    result: dict[str, Any]
    if body.status == "succeeded":
        try:
            definition.result_model.model_validate(body.value)
        except PydanticValidationError as exc:
            raise ValidationFailed(
                "result value is invalid",
                errors=[
                    {
                        "loc": ["body", "value", *map(str, e["loc"])],
                        "msg": e["msg"],
                        "type": e["type"],
                    }
                    for e in exc.errors()
                ],
            ) from exc
        result = {"status": "succeeded", "value": body.value}
    else:
        result = {"status": "failed", "error_code": body.error_code}
    repository.finish_job(session, job_id, result, tenant_id=executor.tenant_id)
    if definition.on_result is not None:
        definition.on_result(session, row, body)
    session.commit()
    return JobResultAck(job_id=job_id, status="done")
