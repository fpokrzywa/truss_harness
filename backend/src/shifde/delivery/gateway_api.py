"""Executor gateway routes (design section 5, section 9 items 12, 13 and 16; DECISIONS B-2664,
B-2669): `whoami`, and the job lease, heartbeat, artifact, context, credential grant, checkpoint and
result routes over `delivery.gateway` (wire models in `adapters.native.contracts`). The artifact
route answers a plan job's archive as `application/zip` with its sha256 in `X-Artifact-Digest`
(B-2678); the context route answers what the job runs against (B-2688).

Mounted without `require_auth`: the only credential read here is `Authorization: Bearer shx_...`
(an executor token; cookies are never read), and a user API key or session gets the same 401
`invalid_credentials` as any other refused token. User routes refuse executor tokens through
`apikeys.authenticate_bearer`, which accepts only `shf_` keys.

Every refused executor token writes one installation-level `delivery.executor.token.rejected`
audit row (no token material), and failures are counted per source address in
`app.state.executor_auth_failure_limiter` (`executor_auth_failures_per_minute`, skipped for
loopback and trusted proxies). A source over its limit gets 429 `rate_limited` before any lookup,
so a correct token cannot be told apart by the answer; successes and a bad `X-Executor-Version`
(422) are not counted (B-2730, as B-919).
"""

from __future__ import annotations

from contextlib import suppress
from typing import Annotated, Any  # Any: a lease or an empty 204
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from shifde.adapters.native import contracts as c
from shifde.core.deps import RequestCtxDep, SessionDep, SettingsDep
from shifde.core.errors import InvalidCredentials, RateLimited
from shifde.core.ratelimit import SlidingWindowLimiter, ip_limit_applies, source_key
from shifde.delivery import gateway
from shifde.delivery import schemas as s
from shifde.delivery.service import AuthenticatedExecutor, authenticate_executor

router = APIRouter(prefix="/api/executor/v1")
EXECUTOR_LIMIT_MESSAGE = "too many failed executor authentications; try again later"


def require_executor(
    request: Request, session: SessionDep, settings: SettingsDep, ctx: RequestCtxDep
) -> AuthenticatedExecutor:
    """Authenticate the executor; the session then runs in its organization (no principal) and
    `last_seen_at` / `version` (`X-Executor-Version`) are updated on every call. Failed attempts
    are limited per source; a source over its limit is refused before any lookup (B-2730)."""
    limiter: SlidingWindowLimiter = request.app.state.executor_auth_failure_limiter
    applies = ip_limit_applies(ctx.client_ip, settings.trusted_proxies, forwarded=ctx.forwarded)
    key = source_key(ctx.client_ip) or "unknown"
    if applies and limiter.exceeded(key):
        raise RateLimited(EXECUTOR_LIMIT_MESSAGE)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    try:
        return authenticate_executor(
            session,
            ctx,
            settings.pepper_ring.values(),
            token.strip() if scheme.lower() == "bearer" else None,
            request.headers.get("x-executor-version"),
        )
    except InvalidCredentials:
        if applies:
            with suppress(RateLimited):  # this attempt has already failed with its own 401
                limiter.check(key)
        raise


ExecutorDep = Annotated[AuthenticatedExecutor, Depends(require_executor)]


@router.get("/whoami", tags=["executor"])
def whoami(executor: ExecutorDep) -> s.ExecutorWhoami:
    return s.ExecutorWhoami(
        executor_id=executor.executor_id,
        tenant_id=executor.tenant_id,
        name=executor.name,
        status=executor.status,
        version=executor.version,
        last_seen_at=executor.last_seen_at,
    )


@router.post(
    "/lease",
    tags=["executor"],
    response_model=c.JobLease,
    responses={204: {"description": "No due job"}},
)
def lease_job(body: c.LeaseRequest, executor: ExecutorDep, session: SessionDep) -> Any:
    leased = gateway.lease(session, executor, body)
    return Response(status_code=204) if leased is None else leased


@router.post("/jobs/{job_id}/heartbeat", tags=["executor"])
def heartbeat(
    job_id: UUID, body: c.LeaseProof, executor: ExecutorDep, session: SessionDep
) -> c.HeartbeatAck:
    return gateway.heartbeat(session, executor, job_id, body)


@router.get(
    "/jobs/{job_id}/artifact",
    tags=["executor"],
    response_class=Response,
    responses={
        200: {
            "description": "The plan's artifact; its sha256 digest in `X-Artifact-Digest`",
            "content": {"application/zip": {}},
        }
    },
)
def job_artifact(
    job_id: UUID,
    lease_id: UUID,
    fence: Annotated[int, Query(ge=1)],
    executor: ExecutorDep,
    session: SessionDep,
) -> Response:
    found = gateway.artifact(
        session, executor, job_id, c.LeaseProof(lease_id=lease_id, fence=fence)
    )
    return Response(
        content=found.content,
        media_type="application/zip",
        headers={"X-Artifact-Digest": found.digest},
    )


@router.get("/jobs/{job_id}/context", tags=["executor"])
def job_context(
    job_id: UUID,
    lease_id: UUID,
    fence: Annotated[int, Query(ge=1)],
    executor: ExecutorDep,
    session: SessionDep,
) -> c.JobContext:
    return gateway.job_context(
        session, executor, job_id, c.LeaseProof(lease_id=lease_id, fence=fence)
    )


@router.post("/jobs/{job_id}/credential-grant", tags=["executor"])
def credential_grant(
    job_id: UUID, body: c.LeaseProof, executor: ExecutorDep, session: SessionDep
) -> c.CredentialGrant:
    return gateway.credential_grant(session, executor, job_id, body)


@router.post("/jobs/{job_id}/checkpoint", tags=["executor"])
def checkpoint(
    job_id: UUID, body: c.CheckpointSubmission, executor: ExecutorDep, session: SessionDep
) -> c.JournalEntry:
    return gateway.checkpoint(session, executor, job_id, body)


@router.post("/jobs/{job_id}/result", tags=["executor"])
def job_result(
    job_id: UUID, body: c.JobResultSubmission, executor: ExecutorDep, session: SessionDep
) -> c.JobResultAck:
    return gateway.submit_result(session, executor, job_id, body)
