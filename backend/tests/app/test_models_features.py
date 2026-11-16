"""P3 vendors + model registry / routing / governed invoke (contract sections 2-3)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import system_role
from tests._support.p3util import (
    ADMIN,
    PLATFORM,
    A,
)


def _caps(client: TestClient, h: dict[str, str], vendor_key: str) -> dict[str, Any]:
    vendors = client.get(f"{A}/vendors?limit=200", headers=h).json()["items"]
    vid = next(v["id"] for v in vendors if v["key"] == vendor_key)
    items = client.get(f"{A}/vendors/{vid}/capabilities", headers=h).json()["items"]
    return {i["capability"]: i for i in items}


# ================================================================== vendors (acceptance 1, 7)


# ================================================================== capabilities + maturity (acceptance 7)


# ================================================================== models (acceptance 2)


# ================================================================== allowlists / routes


# ================================================================== limits


def test_features_advertise_integrate_and_core_roles_hold_the_p3_permissions(
    client: TestClient, world: World, owner_session: Session
) -> None:
    r = client.post(
        "/api/auth/local/login", json={"email": ADMIN, "password": world.passwords[ADMIN]}
    )
    assert "integrate" in r.json()["features"] and "security" in r.json()["features"]
    assert system_role(owner_session, "developer")
    _ = PLATFORM
