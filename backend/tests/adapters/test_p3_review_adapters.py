from __future__ import annotations

import gzip
import time
from typing import Any

import httpx
import pytest

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.conformance import connection as conf_connection
from shifde.adapters.conformance import exec_ctx, loopback_guard
from shifde.adapters.mcp_http import McpHttpAdapter
from shifde.adapters.testservers import McpTestServer
from shifde.core.egress import (
    EgressGuard,
    EgressPolicy,
    EgressViolation,
    classify_ip,
    normalize_ip,
)

PUBLIC = "93.184.216.34"


class _Rec(httpx.BaseTransport):
    def __init__(self, responder: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)  # type: ignore[no-any-return]


def _guard(inner: httpx.BaseTransport, **policy: Any) -> EgressGuard:
    layer = EgressPolicy(allowed_schemes=frozenset({"http"}), **policy)
    return EgressGuard([layer], resolver=lambda h: [PUBLIC], inner=inner)


def test_gzip_bomb_cannot_bypass_the_response_cap() -> None:
    bomb = gzip.compress(b"\0" * 50_000_000)  # ~50 KB on the wire, 50 MB decoded
    assert len(bomb) < 100_000
    inner = _Rec(lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=bomb))
    with _guard(inner, max_response_bytes=1_000_000).client() as client:
        with pytest.raises(EgressViolation) as exc:
            client.get("http://bomb.test/").read()
        assert exc.value.code == "encoding"
    # the guard asks for identity encoding in the first place
    assert inner.requests[0].headers["accept-encoding"] == "identity"
    # an honest identity body is untouched
    ok = _Rec(lambda r: httpx.Response(200, content=b"hello"))
    with _guard(ok, max_response_bytes=1000).client() as client:
        assert client.get("http://ok.test/").text == "hello"


def test_relative_redirects_work_under_a_host_allowlist_and_stay_pinned() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/next"})
        return httpx.Response(200, content=b"done")

    inner = _Rec(responder)
    guard = EgressGuard(
        [
            EgressPolicy(
                allowed_schemes=frozenset({"https"}),
                allowed_hosts=("api.example.com",),
                allow_redirects=True,
            )
        ],
        resolver=lambda h: [PUBLIC],
        inner=inner,
    )
    with guard.client() as client:
        response = client.get("https://api.example.com/start")
    assert response.status_code == 200 and response.text == "done"
    assert [r.url.path for r in inner.requests] == ["/start", "/next"]
    for r in inner.requests:  # every hop was connected by IP with the real name for Host/SNI
        assert r.url.host == PUBLIC and r.headers["host"] == "api.example.com"
        assert r.extensions["sni_hostname"] == "api.example.com"
    assert str(response.request.url) == "https://api.example.com/next"  # caller sees real URL


def test_empty_tool_allowlist_means_no_tools_and_omitted_means_all(
    mcp_server: McpTestServer,
) -> None:
    adapter = McpHttpAdapter()
    ctx = exec_ctx(loopback_guard(timeout_seconds=15.0))
    base = {"url": mcp_server.url}
    token = "mcp-secret-token-0002"
    none_allowed = conf_connection(
        "mcp_http", {**base, "tool_allowlist": []}, auth_method="bearer", secret=token
    )
    with pytest.raises(AdapterFailure) as exc:
        adapter.open(ctx, none_allowed).invoke({"tool": "add", "arguments": {"a": 1, "b": 2}})
    assert exc.value.error_class is ErrorClass.CAPABILITY
    everything = conf_connection("mcp_http", base, auth_method="bearer", secret=token)
    assert (
        not adapter.open(ctx, everything)
        .invoke({"tool": "add", "arguments": {"a": 1, "b": 2}})
        .result["is_error"]
    )
    schema = adapter.descriptor.config_schema["properties"]["tool_allowlist"]
    assert "[] = none" in schema["title"]


@pytest.mark.parametrize("address", ["fec0::1", "2002:c000:204::1", "2002:7f00:1::1"])
def test_site_local_and_6to4_are_hard_denied(address: str) -> None:
    assert classify_ip(normalize_ip(address)) == "hard"


def test_call_deadline_stops_the_transfer_inside_the_transport() -> None:
    def trickle(request: httpx.Request) -> httpx.Response:
        def body() -> Any:
            for _ in range(20):
                time.sleep(0.15)
                yield b"x" * 10

        return httpx.Response(200, content=body())

    guard = _guard(_Rec(trickle), max_response_bytes=10_000)
    guard.set_deadline(0.5)
    started = time.monotonic()
    with guard.client() as client, pytest.raises(EgressViolation) as exc:
        client.get("http://slow.test/").read()
    assert exc.value.code == "deadline" and time.monotonic() - started < 1.5
    with guard.client() as client, pytest.raises(EgressViolation) as late:  # budget is spent
        client.get("http://slow.test/")
    assert late.value.code == "deadline"
    expired = _guard(_Rec(trickle))
    expired.set_deadline(0.2)
    with pytest.raises(EgressViolation):
        expired.sleep(5)  # the fake provider's latency honours the same budget


def test_hosts_sharing_an_address_never_share_a_connection_pool() -> None:
    made: list[_Rec] = []

    def factory() -> httpx.BaseTransport:
        t = _Rec(lambda r: httpx.Response(200, content=b"ok"))
        made.append(t)
        return t

    guard = EgressGuard(
        [EgressPolicy(allowed_schemes=frozenset({"https"}))],
        resolver=lambda h: [PUBLIC],  # both names resolve to the SAME address
        inner_factory=factory,
    )
    with guard.client() as client:
        client.get("https://a.example.com/")
        client.get("https://a.example.com/again")
        assert len(made) == 1  # same name: the pool is reused
        client.get("https://b.example.com/")
        assert len(made) == 2  # other name on the same IP: a separate pool (own TLS sessions)
    assert [len(t.requests) for t in made] == [2, 1]
