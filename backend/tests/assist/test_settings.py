"""SP7-2 (design sections 4, 8 and 11): the organization's assist settings and switch, and the
assist role grants.

With no row the settings read as the defaults. Enable switches assist on and pins the current
catalog version; disable switches it off and keeps the pin. PUT sets only the routing fields and
the cost cap and refuses routing without a model (`router_model_required`), another client's or a
not allowed router model (`router_model_not_allowed`) and a cap that is not above 0. Every write
records one audit event; a member without `assist:admin` gets 403 on every settings route. The
system roles hold exactly the assist keys of design section 8; the platform administrator none.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.assist.catalog import CATALOG_VERSION
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, VIEWER, A, B
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import model, rows

SETTINGS = f"{A}/assist/settings"
ROLE_GRANTS = {
    "tenant_administrator": {
        "assist:request:read", "assist:request:write", "assist:proposal:accept",
        "assist:install", "assist:admin",
    },
    "workspace_administrator": {
        "assist:request:read", "assist:request:write", "assist:proposal:accept", "assist:install",
    },
    "project_administrator": {
        "assist:request:read", "assist:request:write", "assist:proposal:accept", "assist:install",
    },
    "developer": {"assist:request:read", "assist:request:write", "assist:proposal:accept"},
    "viewer": {"assist:request:read"},
    "operator": {"assist:request:read"},
    "auditor": {"assist:request:read"},
    "platform_administrator": set(),
}  # fmt: skip


def _put(h: Hdr, body: dict[str, Any]) -> httpx.Response:
    sent: httpx.Response = h.client.put(SETTINGS, headers=h, json=body)
    return sent


def _audit(owner: Session) -> list[str]:
    return [
        r[0]
        for r in rows(
            owner,
            "SELECT action FROM shifde.audit_events WHERE tenant_id = :t "
            "AND action LIKE 'assist.%' ORDER BY occurred_at, id",
            t=tenant_id(owner, "demo-a"),
        )
    ]


def test_settings_defaults_switch_and_pin(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    got = admin.client.get(SETTINGS, headers=admin)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "assist_enabled": False, "catalog_version": None,
        "current_catalog_version": CATALOG_VERSION, "model_routing_enabled": False,
        "router_model_id": None, "specialist_max_cost": 1.0, "updated_at": None,
    }  # fmt: skip
    assert rows(owner_session, "SELECT 1 FROM shifde.assist_settings") == []
    on = admin.client.post(f"{A}/assist/enable", headers=admin).json()
    assert (on["assist_enabled"], on["catalog_version"]) == (True, CATALOG_VERSION)
    off = admin.client.post(f"{A}/assist/disable", headers=admin).json()
    assert (off["assist_enabled"], off["catalog_version"]) == (False, CATALOG_VERSION)
    router = model(admin)
    put = _put(admin, {"model_routing_enabled": True, "router_model_id": router,
                       "specialist_max_cost": "2.5"})  # fmt: skip
    assert put.status_code == 200, put.text
    assert (put.json()["model_routing_enabled"], put.json()["router_model_id"]) == (True, router)
    assert put.json()["specialist_max_cost"] == 2.5
    # the switch and the pin are left alone by PUT
    assert (put.json()["assist_enabled"], put.json()["catalog_version"]) == (False, CATALOG_VERSION)
    [(by, at)] = rows(owner_session, "SELECT updated_by, updated_at FROM shifde.assist_settings")
    assert by is not None and at is not None
    assert _audit(owner_session) == ["assist.enabled", "assist.disabled", "assist.settings.updated"]


def test_settings_put_validation(client: TestClient, world: World, owner_session: Session) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    admin_b = user_session(client, world.passwords, ADMIN_B)
    theirs = model(admin_b, B)
    mine, allowed = model(admin), model(admin)

    def refused(body: dict[str, Any], code: str) -> None:
        r = _put(admin, body)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == code, r.text

    base = {"model_routing_enabled": True, "specialist_max_cost": "1"}
    refused({**base, "router_model_id": None}, "router_model_required")
    refused({**base, "router_model_id": theirs}, "router_model_not_allowed")
    tenant = str(tenant_id(owner_session, "demo-a"))
    owner_session.rollback()
    put = admin.client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "tenant", "scope_id": tenant},
        json={"enabled": True, "model_ids": [allowed]},
        headers=admin,
    )
    assert put.status_code == 200, put.text
    refused({**base, "router_model_id": mine}, "router_model_not_allowed")
    for cost in ("0", "-1"):
        refused({**base, "router_model_id": allowed, "specialist_max_cost": cost},
                "validation_error")  # fmt: skip
    assert _put(admin, {**base, "router_model_id": allowed}).status_code == 200
    assert rows(owner_session, "SELECT count(*) FROM shifde.assist_settings") == [(1,)]
    assert _audit(owner_session) == ["assist.settings.updated"]


def test_a_non_admin_gets_403_on_settings(
    client: TestClient, world: World, owner_session: Session
) -> None:
    viewer = user_session(client, world.passwords, VIEWER)
    body = {"model_routing_enabled": False, "specialist_max_cost": "1"}
    for r in (
        viewer.client.get(SETTINGS, headers=viewer),
        _put(viewer, body),
        viewer.client.post(f"{A}/assist/enable", headers=viewer),
        viewer.client.post(f"{A}/assist/disable", headers=viewer),
    ):
        assert r.status_code == 403, r.text
    assert rows(owner_session, "SELECT 1 FROM shifde.assist_settings") == []
    assert _audit(owner_session) == []


def test_the_system_roles_hold_the_assist_keys_of_the_design(owner_session: Session) -> None:
    grants: dict[str, set[str]] = {key: set() for key in ROLE_GRANTS}
    for role, permission in rows(
        owner_session,
        "SELECT r.key, rp.permission_key FROM shifde.roles r LEFT JOIN shifde.role_permissions rp "
        "ON rp.role_id = r.id AND starts_with(rp.permission_key, 'assist:') "
        "WHERE r.tenant_id IS NULL",
    ):
        if permission is not None:
            grants.setdefault(role, set()).add(permission)
    assert grants == ROLE_GRANTS
    privileged = rows(
        owner_session,
        "SELECT key FROM shifde.permissions WHERE starts_with(key, 'assist:') AND is_privileged "
        "ORDER BY key",
    )
    assert privileged == [("assist:admin",), ("assist:install",)]
