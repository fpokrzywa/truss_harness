"""The FDE SDK Fabric client tests (1fd902b `tests/adapters/test_fabric_clients.py`),
synchronous, plus the egress, long-running-operation, job `Location` and `EntraTokenProvider`
tests of SETH. Every token and secret here is a placeholder.

Dropped from the SDK: `test_iq_feature_gate_and_execution_policy_prevent_network_calls`, since
the credential profile (`allow_iq`, `allow_notebook_execution`) and `fabric.iq/v1` are gone.
Adapted: `test_read_throttling_honors_retry_after_and_refreshes_auth` injects `sleep` instead of
patching `asyncio.sleep`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from shifde.adapters.native.base import NeedsAttention
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.fabric.clients import (
    EntraTokenProvider,
    FabricAPIError,
    FabricAuthError,
    FabricClient,
)
from shifde.adapters.native.fabric.source import ORIGIN

TOKEN = "test-placeholder-token"
SECRET = "test-placeholder-secret"
ENTRA_TENANT = "11111111-2222-4333-8444-555555555555"
CLIENT_ID = "66666666-7777-4888-9999-000000000000"
OPERATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def fabric_client(
    transport: httpx.BaseTransport,
    *,
    sleep: Callable[[float], None] | None = None,
    tokens: list[int] | None = None,
) -> FabricClient:
    def token() -> str:
        if tokens is not None:
            tokens.append(1)
        return TOKEN

    target = Target(tenant_id=uuid4(), host=ORIGIN)
    return FabricClient(target, token, transport=transport, sleep=sleep or (lambda _: None))


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_fabric_never_retries_native_mutations_or_follows_redirects(status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://evil.example", "Retry-After": "0"}
        )

    with pytest.raises(FabricAPIError) as failed:
        fabric_client(httpx.MockTransport(handle)).request("PATCH", "/v1/test", {})
    assert len(requests) == 1 and requests[0].url.host == "api.fabric.microsoft.com"
    assert failed.value.status == status
    for text in (str(failed.value), repr(failed.value)):
        assert TOKEN not in text and "https://" not in text and "/v1/" not in text


def test_fabric_read_throttling_honors_retry_after_and_refreshes_auth() -> None:
    pauses: list[float] = []
    tokens: list[int] = []
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "2"})
            if len(requests) == 1
            else httpx.Response(200, json={"id": "ok"})
        )

    client = fabric_client(httpx.MockTransport(handle), sleep=pauses.append, tokens=tokens)
    result = client.request("GET", "/v1/test", read=True)
    assert result["body"]["id"] == "ok" and pauses == [2.0] and len(tokens) == 2


def test_fabric_read_throttling_gives_up_after_three_retries() -> None:
    pauses: list[float] = []
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    with pytest.raises(FabricAPIError):
        fabric_client(httpx.MockTransport(handle), sleep=pauses.append).request(
            "GET", "/v1/test", read=True
        )
    assert len(requests) == 4 and pauses == [1.0, 1.0, 1.0]


@pytest.mark.parametrize("value", ["61", "nan-ish"])
def test_fabric_retry_after_is_bounded(value: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": value})

    with pytest.raises((TimeoutError, ValueError)):
        fabric_client(httpx.MockTransport(handle)).request("GET", "/v1/test", read=True)


@pytest.mark.parametrize("path", ["/v2/x", "/v1/../x", "/v1/x?y=1", "/v1/x#y", "https://e.x/v1/"])
def test_fabric_client_refuses_a_non_canonical_path_before_any_request(path: str) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must fail before network access")

    with pytest.raises(ValueError):
        fabric_client(httpx.MockTransport(forbidden)).request("GET", path, read=True)


def test_fabric_client_refuses_another_origin() -> None:
    with pytest.raises(ValueError):
        FabricClient(Target(tenant_id=uuid4(), host="https://evil.example"), lambda: TOKEN)


def test_fabric_response_is_capped() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (24 * 1024 * 1024 + 1))

    with pytest.raises(ValueError, match="bound"):
        fabric_client(httpx.MockTransport(handle)).request("GET", "/v1/test", read=True)


def test_fabric_lro_location_is_never_followed() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/workspaces/w/notebooks":
            return httpx.Response(
                202,
                headers={
                    "x-ms-operation-id": OPERATION,
                    "Location": "https://untrusted.example/redirect",
                    "Retry-After": "0",
                },
            )
        if request.url.path == f"/v1/operations/{OPERATION}":
            return httpx.Response(
                200,
                json={"status": "Succeeded"},
                headers={"Location": "https://untrusted.example/next", "Retry-After": "0"},
            )
        if request.url.path == f"/v1/operations/{OPERATION}/result":
            return httpx.Response(200, json={"id": "created"})
        raise AssertionError("unexpected request")

    client = fabric_client(httpx.MockTransport(handle))
    accepted = client.request("POST", "/v1/workspaces/w/notebooks", {"displayName": "N"})
    assert accepted["operation_id"] == OPERATION
    assert client.wait(accepted) == {"id": "created"}
    assert [r.url.path for r in requests] == [
        "/v1/workspaces/w/notebooks",
        f"/v1/operations/{OPERATION}",
        f"/v1/operations/{OPERATION}/result",
    ]
    assert {r.url.host for r in requests} == {"api.fabric.microsoft.com"}


@pytest.mark.parametrize(
    ("status", "error"),
    [("Failed", RuntimeError), ("Cancelled", RuntimeError), ("Mystery", NeedsAttention)],
)
def test_fabric_lro_failed_cancelled_or_unknown_state(status: str, error: type[Exception]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": status}, headers={"Retry-After": "0"})

    accepted = {"status": 202, "operation_id": OPERATION, "retry_after": "0", "body": {}}
    with pytest.raises(error):
        fabric_client(httpx.MockTransport(handle)).wait(accepted)


def test_fabric_lro_polls_are_bounded() -> None:
    polls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        polls.append(request)
        return httpx.Response(200, json={"status": "Running"}, headers={"Retry-After": "0"})

    accepted = {"status": 202, "operation_id": OPERATION, "retry_after": "0", "body": {}}
    with pytest.raises(TimeoutError):
        fabric_client(httpx.MockTransport(handle)).wait(accepted)
    assert len(polls) == 128


def test_fabric_accepted_mutation_without_an_operation_id_needs_attention() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    with pytest.raises(NeedsAttention):
        fabric_client(httpx.MockTransport(handle)).request("POST", "/v1/test", {})
    assert len(requests) == 1


WS = "0f0e0d0c-0b0a-4909-8807-060504030201"
ITEM = "11112222-3333-4444-8555-666677778888"
JOB = "99990000-aaaa-4bbb-8ccc-ddddeeeeffff"


@pytest.mark.parametrize(
    "location",
    [
        f"http://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/{JOB}",
        f"https://evil.example/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/{JOB}",
        f"https://api.fabric.microsoft.com:8443/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/{JOB}",
        f"https://api.fabric.microsoft.com/v1/workspaces/{uuid4()}/items/{ITEM}/jobs/instances/{JOB}",
        f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{uuid4()}/jobs/instances/{JOB}",
        f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/{JOB}?x=1",
        f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/{JOB}/x",
        f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/not-a-job",
        "https://untrusted.example/redirect",
        "",
    ],
)
def test_fabric_job_location_allow_list_refuses(location: str) -> None:
    with pytest.raises(NeedsAttention):
        FabricClient.job_id(location, WS, ITEM)


def test_fabric_job_location_allow_list_accepts_the_job_instance() -> None:
    location = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/items/{ITEM}/jobs/instances/{JOB}"
    )
    assert FabricClient.job_id(location, WS, ITEM) == JOB


# --------------------------------------------------------------------------- Entra
def entra(
    handle: Callable[[httpx.Request], httpx.Response], calls: list[int]
) -> EntraTokenProvider:
    def secret() -> str:
        calls.append(1)
        return SECRET

    return EntraTokenProvider(
        ENTRA_TENANT, CLIENT_ID, secret, transport=httpx.MockTransport(handle)
    )


def test_fabric_entra_token_request_shape_and_cache() -> None:
    requests: list[httpx.Request] = []
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})

    provider = entra(handle, calls)
    assert calls == []
    assert provider() == TOKEN and provider() == TOKEN
    assert len(requests) == 1 and len(calls) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://login.microsoftonline.com/{ENTRA_TENANT}/oauth2/v2.0/token"
    )
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": [CLIENT_ID],
        "client_secret": [SECRET],
        "scope": ["https://api.fabric.microsoft.com/.default"],
    }


def test_fabric_entra_token_is_refetched_inside_the_expiry_margin() -> None:
    requests: list[httpx.Request] = []
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 60})

    provider = entra(handle, calls)
    provider()
    provider()
    assert len(requests) == 2 and len(calls) == 2


def test_fabric_entra_oversized_response_is_refused_at_the_streaming_cap() -> None:
    sent: list[int] = []

    def chunks() -> Iterator[bytes]:
        while True:
            sent.append(1)
            yield b"x" * 16384

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    with pytest.raises(FabricAuthError) as failed:
        entra(handle, [])()
    assert 4 <= len(sent) <= 5  # 64 KiB cap in 16 KiB blocks: stops at the first block past it
    assert "x" * 16 not in repr(failed.value)


def test_fabric_entra_token_request_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/token"})

    with pytest.raises(FabricAuthError):
        entra(handle, [])()
    assert [r.url.host for r in requests] == ["login.microsoftonline.com"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_client", "secret": SECRET, "token": TOKEN}),
        httpx.Response(500, text=f"{SECRET} {TOKEN}"),
        httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": TOKEN}),
        httpx.Response(200, json={"access_token": TOKEN, "expires_in": "soon"}),
        httpx.Response(200, json=[TOKEN, SECRET]),
        httpx.Response(200, text=f"not json {TOKEN} {SECRET}"),
    ],
)
def test_fabric_entra_failure_never_carries_the_secret_or_token(response: httpx.Response) -> None:
    with pytest.raises(FabricAuthError) as failed:
        entra(lambda request: response, [])()
    for text in (str(failed.value), repr(failed.value)):
        assert SECRET not in text and TOKEN not in text and "{" not in text
    assert failed.value.__cause__ is None


@pytest.mark.parametrize(
    ("tenant", "client"),
    [
        ("common", CLIENT_ID),
        (ENTRA_TENANT, "not-a-guid"),
        (f"{ENTRA_TENANT}/../x", CLIENT_ID),
        ("evil.example", CLIENT_ID),
    ],
)
def test_fabric_entra_ids_must_be_guids(tenant: str, client: str) -> None:
    with pytest.raises(ValueError):
        EntraTokenProvider(tenant, client, lambda: SECRET)


def test_fabric_entra_token_feeds_the_fabric_client() -> None:
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, content=json.dumps({"id": "w"}).encode())

    transport = httpx.MockTransport(handle)
    provider = EntraTokenProvider(ENTRA_TENANT, CLIENT_ID, lambda: SECRET, transport=transport)
    client = FabricClient(Target(tenant_id=uuid4(), host=ORIGIN), provider, transport=transport)
    assert client.workspace("w") == {"id": "w"} and client.workspace("w") == {"id": "w"}
    assert hosts == [
        "login.microsoftonline.com",
        "api.fabric.microsoft.com",
        "api.fabric.microsoft.com",
    ]
