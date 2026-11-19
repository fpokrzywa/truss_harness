"""Executor gateway routes (design section 5, section 9 items 12 and 13; DECISIONS B-2664).

Mounted without `require_auth`: the only credential read here is `Authorization: Bearer shx_...`
(an executor token; cookies are never read), and a user API key or session gets the same 401
`invalid_credentials` as any other refused token. User routes refuse executor tokens through
`apikeys.authenticate_bearer`, which accepts only `shf_` keys.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from shifde.core.deps import SessionDep, SettingsDep
from shifde.delivery import schemas as s
from shifde.delivery.service import AuthenticatedExecutor, authenticate_executor

router = APIRouter(prefix="/api/executor/v1")


def require_executor(
    request: Request, session: SessionDep, settings: SettingsDep
) -> AuthenticatedExecutor:
    """Authenticate the executor; the session then runs in its organization (no principal) and
    `last_seen_at` / `version` (`X-Executor-Version`) are updated on every call."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return authenticate_executor(
        session,
        settings.pepper_ring.values(),
        token.strip() if scheme.lower() == "bearer" else None,
        request.headers.get("x-executor-version"),
    )


ExecutorDep = Annotated[AuthenticatedExecutor, Depends(require_executor)]


@router.get("/whoami", tags=["executor"])
def whoami(executor: ExecutorDep) -> s.ExecutorWhoami:
    return s.ExecutorWhoami(
        executor_id=executor.executor_id,
        name=executor.name,
        status=executor.status,
        version=executor.version,
        last_seen_at=executor.last_seen_at,
    )
