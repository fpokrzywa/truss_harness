"""The gateway client over an `httpx.MockTransport` (B-2688): the bearer token and the executor
version on every call, no redirect followed, 204 as no lease, 409 `lease_lost` as `LeaseLost`,
the artifact digest checked, and the token never in a message or a `repr`."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from shifde import __version__
from shifde.adapters.native.contracts import JobLease, LeasedJob, utcnow
from shifde.executor.client import (
    ArtifactDigestMismatch,
    GatewayClient,
    GatewayError,
    LeaseLost,
)

TOKEN = "shx_prefix_executor-token-never-logged"  # noqa: S105 - a test value
BASE = "https://seth.example.com"


def _client(handler: httpx.MockTransport) -> GatewayClient:
    return GatewayClient(BASE, TOKEN, http=httpx.Client(base_url=BASE, transport=handler))


def _lease() -> JobLease:
    return JobLease(
        job=LeasedJob(
            id=uuid4(),
            kind="plan",
            plan_id=uuid4(),
            deployment_id=None,
            target_id=uuid4(),
            deadline=None,
            attempts=1,
            max_attempts=4,
        ),
        lease_id=uuid4(),
        fence=1,
        lease_until=utcnow() + timedelta(seconds=45),
        request_id=uuid4(),
    )


def test_every_call_sends_the_token_and_the_executor_version() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    assert _client(httpx.MockTransport(handler)).lease(["plan"]) is None
    (request,) = seen
    assert request.url.path == "/api/executor/v1/lease"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-executor-version"] == __version__


def test_a_redirect_is_never_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(307, headers={"location": "https://elsewhere.example.com/steal"})

    with pytest.raises(GatewayError) as raised:
        _client(httpx.MockTransport(handler)).whoami()
    assert calls == ["/api/executor/v1/whoami"]
    assert raised.value.status == 307


def test_lease_lost_and_other_refusals() -> None:
    def lost(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "lease_lost", "detail": TOKEN})

    with pytest.raises(LeaseLost):
        _client(httpx.MockTransport(lost)).heartbeat(_lease())

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "target_unavailable", "detail": TOKEN})

    with pytest.raises(GatewayError) as raised:
        _client(httpx.MockTransport(refused)).credential_grant(_lease())
    assert not isinstance(raised.value, LeaseLost)
    assert raised.value.code == "target_unavailable"
    assert TOKEN not in str(raised.value)


def test_the_artifact_is_returned_only_when_it_matches_its_digest() -> None:
    data = b"PK\x05\x06" + b"\x00" * 18
    good = hashlib.sha256(data).hexdigest()
    lease = _lease()

    def handler(digest: str) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            assert request.url.params["lease_id"] == str(lease.lease_id)
            assert request.url.params["fence"] == "1"
            return httpx.Response(200, content=data, headers={"X-Artifact-Digest": digest})

        return httpx.MockTransport(answer)

    assert _client(handler(good)).artifact(lease) == data
    with pytest.raises(ArtifactDigestMismatch):
        _client(handler("0" * 64)).artifact(lease)


def test_the_token_is_never_in_the_repr() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(204)))
    assert TOKEN not in repr(client)
    assert TOKEN not in str(vars(client))
