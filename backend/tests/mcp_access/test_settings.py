"""`GET|PUT /api/v1/orgs/{org}/mcp/settings` (SP8 design 3.6, 3.7 item 2) and `mcp_client_settings`
at the SQL level (3.5, 3.7 items 2 and 8): defaults, validation, permissions, the audit event, the
cross-client 404s, forced row-level security, the grants, the checks and the role grants."""

from __future__ import annotations

import re
from typing import Any, get_args  # Any: JSON bodies
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from shifde.mcp_access import schemas
from shifde.mcp_access.tools import TOOLS
from tests._support.fullapp import World
from tests.mcp_access.helpers import (
    ADMIN_A,
    ADMIN_B,
    ORG_A,
    ORG_B,
    V1_TOOLS,
    A,
    B,
    mcp_client,
    mcp_rows,
    set_mcp,
    sign_in,
    tenant,
)
from tests.mcp_access.test_access import _developer

DEFAULTS = {"enabled": False, "rate_per_minute": 60, "max_cost_per_call": 1.0}
BODY = {"enabled": True, "rate_per_minute": 30, "max_cost_per_call": 2.5, "allowed_tools": None}


def test_a_missing_row_reads_as_the_defaults_and_put_upserts_with_an_audit_event(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    h = sign_in(mcp, world, ADMIN_A)
    r = mcp.get(f"{A}/mcp/settings", headers=h)
    assert r.status_code == 200, r.text
    got = r.json()
    assert {k: got[k] for k in DEFAULTS} == DEFAULTS and got["allowed_tools"] is None
    assert got["updated_by"] is None and got["endpoint_url"] is None
    put = mcp.put(
        f"{A}/mcp/settings",
        json={**BODY, "allowed_tools": ["whoami", "start_run", "whoami"]},
        headers=h,
    )
    assert put.status_code == 200, put.text
    assert put.json()["allowed_tools"] == ["whoami", "start_run"]
    assert put.json()["max_cost_per_call"] == 2.5 and put.json()["updated_by"] is not None
    assert mcp.get(f"{A}/mcp/settings", headers=h).json() == put.json()
    events = mcp_rows(owner_session, tenant(owner_session, ORG_A), "mcp.settings.updated")
    assert len(events) == 1
    assert events[0]["details"]["old"]["enabled"] is False
    assert events[0]["details"]["new"] == {
        "enabled": True,
        "rate_per_minute": 30,
        "max_cost_per_call": "2.5000",
        "allowed_tools": ["whoami", "start_run"],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"rate_per_minute": 0},
        {"rate_per_minute": 601},
        {"max_cost_per_call": 0},
        {"max_cost_per_call": 25.01},
        {"allowed_tools": ["whoami", "assist_accept"]},
        {"allowed_tools": ["drop_table"]},
        {"surprise": True},
    ],
)
def test_put_out_of_range_is_422(mcp: TestClient, world: World, change: dict[str, Any]) -> None:
    h = sign_in(mcp, world, ADMIN_A)
    r = mcp.put(f"{A}/mcp/settings", json={**BODY, **change}, headers=h)
    assert r.status_code == 422, r.text


def test_put_accepts_the_three_sp8_3_tools(mcp: TestClient, world: World) -> None:
    tools = ["board_get", "assist_ask", "assist_get"]
    assert set_mcp(mcp, world, ADMIN_A, A, allowed_tools=tools)["allowed_tools"] == tools
    h = sign_in(mcp, world, ADMIN_A)
    everything = sorted(V1_TOOLS)
    r = mcp.put(f"{A}/mcp/settings", json={**BODY, "allowed_tools": everything}, headers=h)
    assert r.status_code == 200 and r.json()["allowed_tools"] == everything, r.text


def test_the_tool_names_are_the_same_everywhere(owner_session: Session) -> None:
    check = owner_session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_mcp_client_settings__allowed_tools'"
        )
    ).scalar_one()
    admitted = set(re.findall(r"'([a-z_]+)'", check))
    assert len(schemas.V1_TOOLS) == len(set(schemas.V1_TOOLS)) == 12
    assert set(schemas.V1_TOOLS) == set(get_args(schemas.ToolName)) == set(TOOLS) == admitted
    assert set(schemas.V1_TOOLS) - set(schemas.DEFAULT_TOOLS) == {"start_run", "assist_ask"}


def test_another_clients_settings_are_404_and_unchanged(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    mine = set_mcp(mcp, world, ADMIN_B, B, rate_per_minute=7)
    h = sign_in(mcp, world, ADMIN_A)
    assert mcp.get(f"{B}/mcp/settings", headers=h).status_code == 404
    assert mcp.put(f"{B}/mcp/settings", json=BODY, headers=h).status_code == 404
    unknown = "/api/v1/orgs/no-such-org/mcp/settings"
    assert (
        mcp.get(unknown, headers=h).json()["detail"]
        == (mcp.get(f"{B}/mcp/settings", headers=h).json()["detail"])
    )
    hb = sign_in(mcp, world, ADMIN_B)
    assert mcp.get(f"{B}/mcp/settings", headers=hb).json() == mine
    assert mcp_rows(owner_session, tenant(owner_session, ORG_A), "mcp.settings.updated") == []


def test_a_developer_reads_but_cannot_write(
    mcp: TestClient, settings: Settings, owner_session: Session, app_db: Any
) -> None:
    a = tenant(owner_session, ORG_A)
    _, secret = _developer(
        owner_session, app_db, settings, a, ["mcp:settings:read", "mcp:settings:write"]
    )
    h = {"Authorization": f"Bearer {secret}"}
    assert mcp.get(f"{A}/mcp/settings", headers=h).status_code == 200
    assert mcp.put(f"{A}/mcp/settings", json=BODY, headers=h).status_code == 403


def test_endpoint_url_is_the_installations_public_url(settings: Settings, world: World) -> None:
    configured = settings.model_copy(update={"mcp_public_url": "https://seth.example.com/mcp"})
    with mcp_client(configured) as c:
        h = sign_in(c, world, ADMIN_A)
        assert c.get(f"{A}/mcp/settings", headers=h).json()["endpoint_url"] == (
            "https://seth.example.com/mcp"
        )


# ------------------------------------------------------------------ SQL level
def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return orig.sqlstate if isinstance(orig, psycopg.Error) else None


def _as_app(app_engine: Engine, tenant_id: UUID, sql: str, **params: Any) -> list[Any]:
    with app_engine.connect() as c, c.begin():
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        result = c.execute(text(sql), params)
        return list(result) if result.returns_rows else []


def test_shifde_app_sees_and_writes_only_its_own_row(
    world: World, owner_session: Session, app_engine: Engine
) -> None:
    a, b = tenant(owner_session, ORG_A), tenant(owner_session, ORG_B)
    owner_session.execute(
        text(
            "INSERT INTO shifde.mcp_client_settings (tenant_id, enabled) VALUES (:a, true), (:b, true)"
        ),
        {"a": a, "b": b},
    )
    owner_session.commit()
    rows = _as_app(app_engine, a, "SELECT tenant_id FROM shifde.mcp_client_settings")
    assert [r[0] for r in rows] == [a]

    def refused(sql: str, **params: Any) -> str | None:
        with pytest.raises(DBAPIError) as caught:
            _as_app(app_engine, a, sql, **params)
        return _sqlstate(caught.value)

    assert refused("DELETE FROM shifde.mcp_client_settings") == "42501"
    assert refused("UPDATE shifde.mcp_client_settings SET tenant_id = :b", b=b) == "42501"
    assert (
        refused("INSERT INTO shifde.mcp_client_settings (tenant_id) VALUES (:b)", b=b) == "42501"
    )  # the policy's WITH CHECK
    # the column-wise grant lets A change its own row
    _as_app(app_engine, a, "UPDATE shifde.mcp_client_settings SET rate_per_minute = 5")
    owner_session.rollback()
    assert (
        owner_session.execute(
            text("SELECT rate_per_minute FROM shifde.mcp_client_settings WHERE tenant_id = :b"),
            {"b": b},
        ).scalar_one()
        == 60
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "rate_per_minute = 0",
        "rate_per_minute = 601",
        "max_cost_per_call = 0",
        "max_cost_per_call = 25.01",
        "allowed_tools = ARRAY['whoami', 'drop_table']",
    ],
)
def test_checks_refuse_out_of_range_values(
    world: World, owner_session: Session, app_engine: Engine, assignment: str
) -> None:
    a = tenant(owner_session, ORG_A)
    owner_session.execute(
        text("INSERT INTO shifde.mcp_client_settings (tenant_id) VALUES (:a)"), {"a": a}
    )
    owner_session.commit()
    with pytest.raises(DBAPIError) as caught:
        _as_app(app_engine, a, f"UPDATE shifde.mcp_client_settings SET {assignment}")
    assert _sqlstate(caught.value) == "23514"


def test_the_sp8_3_tool_names_are_admitted_by_the_check(
    world: World, owner_session: Session, app_engine: Engine
) -> None:
    a = tenant(owner_session, ORG_A)
    _as_app(
        app_engine,
        a,
        "INSERT INTO shifde.mcp_client_settings (tenant_id, allowed_tools) "
        "VALUES (:a, ARRAY['board_get', 'assist_ask', 'assist_get'])",
        a=a,
    )


def test_role_grants_of_the_mcp_permissions(owner_session: Session) -> None:
    def held(role: str) -> set[str]:
        return set(
            owner_session.execute(
                text(
                    "SELECT rp.permission_key FROM shifde.role_permissions rp "
                    "JOIN shifde.roles r ON r.id = rp.role_id "
                    "WHERE r.tenant_id IS NULL AND r.key = :k AND rp.permission_key LIKE 'mcp:%'"
                ),
                {"k": role},
            ).scalars()
        )

    assert held("tenant_administrator") == {"mcp:access", "mcp:settings:read", "mcp:settings:write"}
    assert held("developer") == {"mcp:access", "mcp:settings:read"}
    assert held("platform_administrator") == set()
    others = owner_session.execute(
        text(
            "SELECT DISTINCT r.key FROM shifde.role_permissions rp JOIN shifde.roles r "
            "ON r.id = rp.role_id WHERE rp.permission_key LIKE 'mcp:%'"
        )
    ).scalars()
    assert set(others) == {"tenant_administrator", "developer"}

    def privileged(key: str) -> bool:
        return bool(
            owner_session.execute(
                text("SELECT is_privileged FROM shifde.permissions WHERE key = :k"), {"k": key}
            ).scalar_one()
        )

    assert privileged("mcp:settings:write") is True
    assert privileged("mcp:access") is False and privileged("mcp:settings:read") is False


def test_the_permission_catalog_offers_the_mcp_scopes(mcp: TestClient, world: World) -> None:
    """The API key screen's scope checklist reads this catalog (no front-end change needed)."""
    h = sign_in(mcp, world, ADMIN_A)
    r = mcp.get("/api/v1/permissions", params={"limit": 200}, headers=h)
    assert r.status_code == 200, r.text
    keys = {p["key"] for p in r.json()["items"]}
    assert {"mcp:access", "mcp:settings:read", "mcp:settings:write"} <= keys
