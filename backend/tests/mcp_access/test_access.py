"""Who can call `/mcp` (SP8 design 3.2, 3.7 items 3 and 4; section 10 items 1 to 6 and 19 to 21):
the mount switch, the transport guards, the one generic 401, `mcp:access` at organization level and
the per-client switch, each refusal audited in the key's own client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import anyio
import httpx
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import apikeys
from tests._support.fullapp import World
from tests._support.tenants import bind_user
from tests.identity.conftest import app_session, owner_membership, owner_user
from tests.mcp_access.helpers import (
    ACCEPT,
    ADMIN_A,
    ADMIN_B,
    ORG_A,
    ORG_B,
    V1_TOOLS,
    A,
    B,
    make_key,
    mcp_client,
    mcp_rows,
    mcp_settings,
    ok,
    project,
    rpc,
    set_mcp,
    sign_in,
    tenant,
)


def _bare(c: TestClient, headers: dict[str, str] | None = None) -> httpx.Response:
    return c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**ACCEPT, **(headers or {})},
    )


# ------------------------------------------------------------------ mount switch, SDK client
def test_mcp_is_not_mounted_when_the_installation_switch_is_off(
    settings: Settings, world: World
) -> None:
    with mcp_client(settings) as c:
        assert _bare(c).status_code == 404
        h = sign_in(c, world, ADMIN_A)  # the settings routes are there either way
        assert c.get(f"{A}/mcp/settings", headers=h).status_code == 200


def test_the_sdk_client_initializes_and_lists_exactly_the_nine_v1_tools(
    settings: Settings, world: World
) -> None:
    configured = mcp_settings(settings)
    with mcp_client(configured) as c:
        _, key = make_key(c, world, ADMIN_A, A)
        set_mcp(c, world, ADMIN_A, A)

    async def session() -> tuple[list[dict[str, object]], dict[str, object]]:
        app = create_app(configured)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with (
                    httpx.AsyncClient(
                        transport=transport,
                        base_url="http://testserver",
                        headers={"Authorization": f"Bearer {key}"},
                    ) as http,
                    streamable_http_client("http://testserver/mcp", http_client=http) as (
                        read,
                        write,
                        _,
                    ),
                    ClientSession(read, write) as mcp,
                ):
                    await mcp.initialize()
                    listed = await mcp.list_tools()
                    me = await mcp.call_tool("whoami", {})
            tools = [t.model_dump() for t in listed.tools]
            return tools, json.loads(me.content[0].text)  # type: ignore[union-attr]
        finally:
            app.state.db.dispose()

    tools, me = anyio.run(session)
    assert {t["name"] for t in tools} == V1_TOOLS and len(tools) == 9
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)  # type: ignore[index]
    assert me["client_slug"] == ORG_A and me["key_name"] == "mcp key"
    assert "start_run" not in me["tools"]  # NULL allowed_tools: the default set


# ------------------------------------------------------------------ transport guards
def test_origin_host_and_body_size_are_refused_before_the_tools(
    mcp: TestClient, world: World
) -> None:
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A)
    auth = {"Authorization": f"Bearer {key}"}
    origin = rpc(mcp, key, "tools/list", headers={"Origin": "https://evil.example"})
    assert origin.status_code == 403 and origin.json() == {"error": "origin_not_allowed"}
    host = rpc(mcp, key, "tools/list", headers={"Host": "evil.example"})
    assert host.status_code == 421
    big = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"pad":"' + b"x" * 262144
    declared = mcp.post(
        "/mcp", content=big, headers={**ACCEPT, **auth, "Content-Type": "application/json"}
    )
    assert declared.status_code == 413

    def chunks() -> Iterator[bytes]:
        for _ in range(9):
            yield b"y" * 32768

    streamed = mcp.post(
        "/mcp", content=chunks(), headers={**ACCEPT, **auth, "Content-Type": "application/json"}
    )
    assert "content-length" not in {k.lower() for k in streamed.request.headers}
    assert streamed.status_code == 413
    # just under the cap goes on to the tools (and fails as JSON, not as a size)
    assert rpc(mcp, key, "tools/list").status_code == 200


# ------------------------------------------------------------------ one generic 401
def test_every_refused_credential_gets_one_identical_401(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    _, key = make_key(mcp, world, ADMIN_A, A)
    revoked_id, revoked = make_key(mcp, world, ADMIN_A, A, name="revoked")
    h = sign_in(mcp, world, ADMIN_A)
    assert mcp.post(f"{A}/api-keys/{revoked_id}/revoke", headers=h).status_code == 200
    expired_id, expired = make_key(mcp, world, ADMIN_A, A, name="expired")
    owner_session.execute(
        text(
            "UPDATE shifde.api_keys SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = :k"
        ),
        {"k": expired_id},
    )
    owner_session.commit()
    set_mcp(mcp, world, ADMIN_A, A)
    responses = {
        "cookie only": _bare(mcp),  # signed in above: the session cookie is sent
        "basic": _bare(mcp, {"Authorization": "Basic YWRtaW46cGFzc3dvcmQ="}),
        "executor": _bare(mcp, {"Authorization": "Bearer shx_test_abcdefgh_" + "a" * 43}),
        "unknown": _bare(mcp, {"Authorization": "Bearer shf_test_ZZZZZZZZ_" + "a" * 43}),
        "revoked": _bare(mcp, {"Authorization": f"Bearer {revoked}"}),
        "expired": _bare(mcp, {"Authorization": f"Bearer {expired}"}),
    }
    mcp.cookies.clear()
    responses["no header"] = _bare(mcp)
    first = responses["no header"]
    assert first.status_code == 401 and first.json() == {"error": "invalid_credentials"}
    for name, r in responses.items():
        assert r.status_code == 401, name
        assert r.content == first.content, name
    assert _bare(mcp, {"Authorization": f"Bearer {key}"}).status_code == 200


def test_a_key_whose_secret_does_not_verify_writes_only_the_installation_row(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A)

    def counts() -> tuple[int, int]:
        owner_session.rollback()
        row = owner_session.execute(
            text(
                "SELECT count(*) FILTER (WHERE tenant_id IS NULL), "
                "count(*) FILTER (WHERE tenant_id IS NOT NULL) FROM shifde.audit_events"
            )
        ).one()
        return int(row[0]), int(row[1])

    before = counts()
    wrong = key[:-1] + ("a" if key[-1] != "a" else "b")
    r = _bare(mcp, {"Authorization": f"Bearer {wrong}"})
    assert r.status_code == 401
    installation, clients = counts()
    assert (installation, clients) == (before[0] + 1, before[1])
    owner_session.rollback()
    action = owner_session.execute(
        text(
            "SELECT action FROM shifde.audit_events WHERE tenant_id IS NULL "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    ).scalar_one()
    assert action == apikeys.REJECTED_ACTION


def test_repeated_failures_from_one_source_trip_the_api_key_failure_limiter(
    settings: Settings, world: World
) -> None:
    with mcp_client(mcp_settings(settings, api_key_failures_per_minute=2)) as c:
        bad = {"Authorization": "Bearer shf_test_ZZZZZZZZ_" + "b" * 43}
        assert [_bare(c, bad).status_code for _ in range(3)] == [401, 401, 429]
        assert _bare(c, bad).json() == {"error": "rate_limited"}


# ------------------------------------------------------------------ mcp:access
def _developer(
    owner: Session, app_db: Database, settings: Settings, tenant_id: UUID, scopes: list[str]
) -> tuple[UUID, str]:
    """A member of `tenant_id` bound to `developer` at organization level, with a personal key
    of `scopes`: (user id, secret)."""
    user = owner_user(owner, f"dev-{uuid7().hex[:8]}@demo-a.local")
    owner_membership(owner, tenant_id, user)
    bind_user(owner, tenant_id, user, "developer")
    owner.commit()
    s = app_session(app_db, tenant_id, user)
    try:
        _, secret = apikeys.mint(
            s,
            settings,
            tenant_id=tenant_id,
            name="developer key",
            principal_type="user",
            principal_id=user,
            scopes=scopes,
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=user,
        )
        s.commit()
    finally:
        s.close()
    return user, secret


def test_mcp_access_needs_the_key_mask_and_an_organization_level_grant(
    mcp: TestClient,
    world: World,
    settings: Settings,
    owner_session: Session,
    app_db: Database,
) -> None:
    a = tenant(owner_session, ORG_A)
    set_mcp(mcp, world, ADMIN_A, A)
    _, masked = make_key(mcp, world, ADMIN_A, A, scopes=["project:read"])
    r = _bare(mcp, {"Authorization": f"Bearer {masked}"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}

    user, secret = _developer(owner_session, app_db, settings, a, ["mcp:access", "project:read"])
    dev = {"Authorization": f"Bearer {secret}"}
    assert _bare(mcp, dev).status_code == 200
    owner_session.execute(
        text("DELETE FROM shifde.role_bindings WHERE subject_id = :u"), {"u": user}
    )
    owner_session.commit()
    lost = _bare(mcp, dev)
    assert lost.status_code == 403 and lost.json() == {"error": "forbidden"}
    # a binding at project level only is not enough
    owner_session.execute(
        text(
            "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, role_id, "
            "scope_type, scope_id) SELECT :i, :t, 'user', :u, r.id, 'project', :p "
            "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = 'developer'"
        ),
        {"i": uuid7(), "t": a, "u": user, "p": project(owner_session, ORG_A)},
    )
    owner_session.commit()
    assert _bare(mcp, dev).status_code == 403
    refusals = mcp_rows(owner_session, a, "mcp.access_refused")
    assert [r["details"]["reason"] for r in refusals] == ["permission"] * 3
    assert mcp_rows(owner_session, tenant(owner_session, ORG_B), "mcp.access_refused") == []
    denied = mcp_rows(owner_session, a, "authz.denied")
    assert [r["details"].get("permission") for r in denied] == ["mcp:access"] * 3


# ------------------------------------------------------------------ the per-client switch
def test_a_disabled_client_gets_mcp_disabled_audited_in_its_own_client_only(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    a, b = tenant(owner_session, ORG_A), tenant(owner_session, ORG_B)
    _, key_a = make_key(mcp, world, ADMIN_A, A)
    _, key_b = make_key(mcp, world, ADMIN_B, B)
    set_mcp(mcp, world, ADMIN_A, A)
    refused = _bare(mcp, {"Authorization": f"Bearer {key_b}"})  # B has no settings row
    assert refused.status_code == 403 and refused.json() == {"error": "mcp_disabled"}
    set_mcp(mcp, world, ADMIN_B, B, enabled=False)  # a row that says off
    assert _bare(mcp, {"Authorization": f"Bearer {key_b}"}).json() == {"error": "mcp_disabled"}
    assert ok(mcp, key_a, "whoami")["client_slug"] == ORG_A
    in_b = mcp_rows(owner_session, b, "mcp.access_refused")
    assert [r["details"]["reason"] for r in in_b] == ["disabled", "disabled"]
    assert mcp_rows(owner_session, a, "mcp.access_refused") == []
