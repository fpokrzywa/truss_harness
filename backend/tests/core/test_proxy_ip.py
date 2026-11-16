"""Audit Item 3 (ARCH M9, SEC Major-2): with the default empty `SHIFDE_TRUSTED_PROXIES` every peer
behind the frontend proxy is 127.0.0.1, so the per-IP limit was inert and the audit `actor_ip`
wrong. The request context now resolves the real client from X-Forwarded-For when the peer is a
trusted proxy; loopback peers (the same-host frontend proxy) are always trusted."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shifde.core.deps import RequestCtxDep
from shifde.core.ratelimit import ip_limit_applies, resolve_client_ip
from shifde.core.settings import Settings


def _app(**overrides: Any) -> FastAPI:  # Any: Settings keyword overrides
    app = FastAPI()
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql+psycopg://x",
        "env": "test",
        **overrides,
    }
    app.state.settings = Settings(**kwargs)

    @app.get("/ip")
    def ip(ctx: RequestCtxDep) -> dict[str, str | None]:
        return {"ip": ctx.client_ip}

    return app


def _ip(app: FastAPI, peer: str, xff: str | None) -> str | None:
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    with TestClient(app, client=(peer, 5000)) as c:
        out: str | None = c.get("/ip", headers=headers).json()["ip"]
        return out


def test_default_config_behind_a_loopback_frontend_proxy_records_the_forwarded_client() -> None:
    app = _app()
    assert app.state.settings.trusted_proxies == []
    ip = _ip(app, "127.0.0.1", "9.9.9.9")
    assert ip == "9.9.9.9"
    assert ip_limit_applies(ip, app.state.settings.trusted_proxies) is True  # the limit bites


def test_loopback_peer_without_forwarded_header_stays_loopback() -> None:
    assert _ip(_app(), "127.0.0.1", None) == "127.0.0.1"


@pytest.mark.parametrize("peer", ["203.0.113.5", "testclient"])
def test_xff_from_an_untrusted_peer_is_ignored(peer: str) -> None:
    assert _ip(_app(), peer, "6.6.6.6") == peer


def test_configured_proxy_chain_yields_the_rightmost_untrusted_address() -> None:
    app = _app(trusted_proxies="10.0.0.0/8")
    # 6.6.6.6 is what the client claimed; 9.9.9.9 is what the first proxy saw
    assert _ip(app, "10.0.0.2", "6.6.6.6, 9.9.9.9, 10.0.0.1") == "9.9.9.9"


def test_garbage_forwarded_values_fall_back_to_the_peer() -> None:
    assert _ip(_app(), "127.0.0.1", "not-an-ip, , ???") == "127.0.0.1"


def test_resolve_client_ip_unit() -> None:
    assert resolve_client_ip(None, "1.2.3.4", []) is None
    assert resolve_client_ip("10.0.0.5", "1.2.3.4", []) == "10.0.0.5"
    assert resolve_client_ip("::1", "2001:db8::7", []) == "2001:db8::7"
    assert resolve_client_ip("10.0.0.5", "1.2.3.4", ["10.0.0.0/24"]) == "1.2.3.4"
    assert resolve_client_ip("10.0.0.5", None, ["10.0.0.0/24"]) == "10.0.0.5"


def test_resolve_client_ip_trust_loopback_off_believes_only_configured_proxies() -> None:
    off = {"trust_loopback": False}
    assert resolve_client_ip("127.0.0.1", "1.2.3.4", [], **off) == "127.0.0.1"
    assert resolve_client_ip("::1", "2001:db8::7", [], **off) == "::1"
    assert resolve_client_ip("10.0.0.5", "1.2.3.4", ["10.0.0.0/24"], **off) == "1.2.3.4"
    # a loopback hop inside a trusted proxy's chain is the client when loopback is not trusted
    assert resolve_client_ip("10.0.0.5", "127.0.0.1", ["10.0.0.0/24"], **off) == "127.0.0.1"
    assert resolve_client_ip("10.0.0.5", "127.0.0.1", ["10.0.0.0/24"]) == "10.0.0.5"
