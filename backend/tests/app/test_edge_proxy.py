"""SP4-2 (B-2772): the edge-proxy topology. The API sits behind one edge proxy at 10.9.9.9, the
only listed `SHIFDE_TRUSTED_PROXIES` entry, with loopback trust off. Requests arrive from that
peer over an httpx `ASGITransport`, so no loopback alias is involved."""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.settings import Settings
from tests._support.fullapp import ORIGIN, World

ADMIN_A = "admin@demo-a.local"
EDGE = "10.9.9.9"
LOGIN = "/api/auth/local/login"


def _ips(owner: Session) -> tuple[set[str | None], set[str | None]]:
    audit = {
        r[0]
        for r in owner.execute(
            text("SELECT actor_ip FROM shifde.audit_events WHERE action LIKE 'identity.%'")
        )
    }
    sess = {r[0] for r in owner.execute(text("SELECT ip FROM shifde.sessions"))}
    return audit, sess


def _logins(
    settings: Settings, world: World, peer: str, forwarded: list[str]
) -> list[httpx.Response]:
    """POST one local login per `X-Forwarded-For` value, in order, from `peer`."""
    cfg = settings.model_copy(
        update={
            "trusted_proxies": [f"{EDGE}/32"],
            "trust_loopback_proxy": False,
            "login_rate_per_minute": 2,
        }
    )
    app = create_app(cfg)
    creds = {"email": ADMIN_A, "password": world.passwords[ADMIN_A]}

    async def run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app, client=(peer, 50000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers={"Origin": ORIGIN}
        ) as c:
            return [
                await c.post(LOGIN, json=creds, headers={"X-Forwarded-For": xff})
                for xff in forwarded
            ]

    try:
        return asyncio.run(run())
    finally:
        app.state.db.dispose()  # ASGITransport runs no lifespan


def test_the_edge_supplied_address_is_believed(
    settings: Settings, world: World, owner_session: Session
) -> None:
    first, second, third, other = _logins(
        settings, world, EDGE, ["203.0.113.7", "203.0.113.7", "203.0.113.7", "203.0.113.8"]
    )
    assert first.status_code == 200 and second.status_code == 200
    assert third.status_code == 429 and third.json()["code"] == "rate_limited"
    assert other.status_code == 200  # a different real client has its own limiter key
    audit_ips, session_ips = _ips(owner_session)
    assert audit_ips == {"203.0.113.7", "203.0.113.8"}
    assert session_ips == {"203.0.113.7", "203.0.113.8"}


def test_an_unlisted_peers_header_is_ignored(
    settings: Settings, world: World, owner_session: Session
) -> None:
    responses = _logins(settings, world, "10.9.9.10", ["1.1.1.1", "1.1.1.2", "1.1.1.3"])
    assert [r.status_code for r in responses] == [200, 200, 429]  # rotating values buy nothing
    audit_ips, session_ips = _ips(owner_session)
    assert audit_ips == {"10.9.9.10"} and session_ips == {"10.9.9.10"}


def test_a_client_chosen_value_before_the_edge_hop_is_not_believed(
    settings: Settings, world: World, owner_session: Session
) -> None:
    (ok,) = _logins(settings, world, EDGE, ["6.6.6.6, 203.0.113.7"])
    assert ok.status_code == 200
    audit_ips, session_ips = _ips(owner_session)
    assert audit_ips == {"203.0.113.7"} and session_ips == {"203.0.113.7"}
