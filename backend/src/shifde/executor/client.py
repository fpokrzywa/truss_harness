"""Synchronous client of the executor gateway (`/api/executor/v1`; FDE SDK `shifde_executor.client`,
without pools, JWT or replays; B-2688).

Every call sends the executor token as `Authorization: Bearer` and the backend version as
`X-Executor-Version`, follows no redirect and parses the body with the `contracts` wire models. A
409 `lease_lost` raises `LeaseLost`; any other refusal raises `GatewayError` naming only the status
and the error code. The monotonic time of the last call answered below 300 is `last_success`
(the executor's readiness). The token is held as a `SecretStr`: it never appears in a log, an
exception or a `repr`.
"""

from __future__ import annotations

import hashlib
import re
import time
from types import TracebackType
from typing import Any, Literal  # Any: httpx request options, JSON values
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr

from shifde import __version__
from shifde.adapters.native.contracts import (
    CheckpointSubmission,
    CredentialGrant,
    HeartbeatAck,
    JobContext,
    JobKind,
    JobLease,
    JobResultAck,
    JobResultSubmission,
    JournalEntry,
    LeaseRequest,
)

PREFIX = "/api/executor/v1"
TIMEOUT = httpx.Timeout(30, connect=5)
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class GatewayError(RuntimeError):
    """The gateway refused a call (status and error code only, never the body text)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"executor gateway refused the call ({status}, {code})")
        self.status, self.code = status, code


class LeaseLost(GatewayError):
    """The job's lease is no longer the executor's (409 `lease_lost`, or it ran out locally)."""

    def __init__(self) -> None:
        super().__init__(409, "lease_lost")


class ArtifactDigestMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("the artifact bytes do not match their digest")


class Whoami(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    executor_id: UUID
    name: str
    status: str


def _code(response: httpx.Response) -> str:
    try:
        candidate = response.json().get("code")
    except (ValueError, AttributeError):
        return "request_failed"
    return candidate if isinstance(candidate, str) and _CODE.fullmatch(candidate) else "unknown"


class GatewayClient:
    """The gateway calls of one executor. `http` may be injected (tests pass a `TestClient`);
    otherwise an `httpx.Client` on `base_url` is created and closed with this client."""

    def __init__(self, base_url: str, token: str, *, http: httpx.Client | None = None) -> None:
        self._token = SecretStr(token)
        self._owned = http is None
        self.last_success: float | None = None
        self._http = http or httpx.Client(
            base_url=base_url, timeout=TIMEOUT, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return f"GatewayClient(base_url={str(self._http.base_url)!r})"

    def __enter__(self) -> GatewayClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def _send(self, method: str, path: str, **options: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "X-Executor-Version": __version__,
        }
        response = self._http.request(
            method, PREFIX + path, headers=headers, follow_redirects=False, **options
        )
        if response.status_code >= 300:
            code = _code(response)
            if response.status_code == 409 and code == "lease_lost":
                raise LeaseLost
            raise GatewayError(response.status_code, code)
        self.last_success = time.monotonic()
        return response

    @staticmethod
    def _proof(lease: JobLease) -> dict[str, Any]:  # Any: JSON values
        return {"lease_id": str(lease.lease_id), "fence": lease.fence}

    def whoami(self) -> Whoami:
        return Whoami.model_validate(self._send("GET", "/whoami").json())

    def lease(self, kinds: list[JobKind]) -> JobLease | None:
        """Lease at most one due job of these kinds (a fresh `request_id`); `None` on 204."""
        body = LeaseRequest(request_id=uuid4(), kinds=kinds)
        response = self._send("POST", "/lease", json=body.model_dump(mode="json"))
        if response.status_code == 204:
            return None
        return JobLease.model_validate(response.json())

    def heartbeat(self, lease: JobLease) -> HeartbeatAck:
        response = self._send("POST", f"/jobs/{lease.job.id}/heartbeat", json=self._proof(lease))
        return HeartbeatAck.model_validate(response.json())

    def artifact(self, lease: JobLease) -> bytes:
        """The job's artifact bytes, only when their sha256 equals `X-Artifact-Digest`."""
        response = self._send("GET", f"/jobs/{lease.job.id}/artifact", params=self._proof(lease))
        data = response.content
        if hashlib.sha256(data).hexdigest() != response.headers.get("x-artifact-digest"):
            raise ArtifactDigestMismatch
        return data

    def context(self, lease: JobLease) -> JobContext:
        response = self._send("GET", f"/jobs/{lease.job.id}/context", params=self._proof(lease))
        return JobContext.model_validate(response.json())

    def credential_grant(self, lease: JobLease) -> CredentialGrant:
        response = self._send(
            "POST", f"/jobs/{lease.job.id}/credential-grant", json=self._proof(lease)
        )
        return CredentialGrant.model_validate(response.json())

    def checkpoint(
        self,
        lease: JobLease,
        *,
        sequence: int,
        step_key: str,
        state: Literal["intent", "complete"],
        input_digest: str,
        receipt: dict[str, JsonValue] | None = None,
    ) -> JournalEntry:
        body = CheckpointSubmission(
            lease_id=lease.lease_id,
            fence=lease.fence,
            sequence=sequence,
            step_key=step_key,
            state=state,
            input_digest=input_digest,
            receipt=receipt,
        )
        response = self._send(
            "POST", f"/jobs/{lease.job.id}/checkpoint", json=body.model_dump(mode="json")
        )
        return JournalEntry.model_validate(response.json())

    def result(
        self,
        lease: JobLease,
        *,
        value: dict[str, JsonValue] | None = None,
        error_code: Literal["execution_failed", "credential_unavailable"] | None = None,
    ) -> JobResultAck:
        """Post `succeeded` with `value`, or `failed` with `error_code` (never error text)."""
        body = JobResultSubmission(
            lease_id=lease.lease_id,
            fence=lease.fence,
            status="failed" if error_code is not None else "succeeded",
            value=value,
            error_code=error_code,
        )
        response = self._send(
            "POST", f"/jobs/{lease.job.id}/result", json=body.model_dump(mode="json")
        )
        return JobResultAck.model_validate(response.json())
