"""Cross-client cases for the SETH tests moved into `tests/app` (audit, connections, models,
build artifacts, sessions): a person of one client reaches nothing of another through the full
app, and what a client creates is not in another client's lists (B-2250).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import login, post, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B, connection
from tests._support.p4util import art, prompt_spec, tenant_scope

# GET routes of the moved areas; B's administrator gets 404 under A's slug whatever the id
A_ROUTES = [
    "/audit-events",
    "/audit-events/verify",
    "/connections",
    "/models",
    "/model-providers",
    "/prompts",
    "/agents",
    "/tools",
    "/skills",
    "/roles",
    "/role-bindings",
    "/users",
    "/groups",
]


@pytest.mark.parametrize("route", A_ROUTES)
def test_another_clients_administrator_gets_404_on_every_moved_area(
    client: TestClient, world: World, route: str
) -> None:
    bh = login(client, world.passwords, ADMIN_B)
    assert client.get(f"{A}{route}", headers=bh).status_code == 404
    assert client.get(f"{B}{route}", headers=bh).status_code == 200
    client.cookies.clear()
    assert client.get(f"{A}{route}").status_code == 401


def test_what_one_client_creates_is_in_no_other_clients_lists(
    client: TestClient, world: World, owner_session: Session
) -> None:
    ah = login(client, world.passwords, ADMIN)
    made = connection(
        client,
        ah,
        {
            "name": "only-in-a",
            "connection_type": "rest",
            "adapter_key": "rest.http",
            "config": {"base_url": "https://example.com"},
        },
    )
    prompt = art(client, ah, "prompts", "only-in-a", prompt_spec(), tenant_scope(owner_session))
    actions_a = {
        e["action"] for e in client.get(f"{A}/audit-events?limit=200", headers=ah).json()["items"]
    }
    assert "connection.created" in actions_a
    bh = login(client, world.passwords, ADMIN_B)
    names_b = [c["name"] for c in client.get(f"{B}/connections", headers=bh).json()["items"]]
    assert "only-in-a" not in names_b
    keys_b = [p["key"] for p in client.get(f"{B}/prompts", headers=bh).json()["items"]]
    assert "only-in-a" not in keys_b
    # the ids are no oracle: B asking for A's objects through its own slug finds nothing
    assert client.get(f"{B}/connections/{made['id']}", headers=bh).status_code == 404
    assert client.get(f"{B}/prompts/{prompt['id']}", headers=bh).status_code == 404
    # nor can B act on them through A's slug
    assert client.post(f"{A}/connections/{made['id']}/test", headers=bh).status_code == 404
    events_b = client.get(f"{B}/audit-events?limit=200", headers=bh).json()["items"]
    assert all(e["action"] != "connection.created" for e in events_b)
    assert tenant_id(owner_session, "demo-b") != tenant_id(owner_session, "demo-a")


def test_a_platform_group_filled_in_one_client_confers_nothing_in_another(
    client: TestClient, world: World, owner_session: Session
) -> None:
    """The moved IAM group tests bind a platform group in demo-a; demo-b's member in it gets
    nothing from that binding (B-928: bindings are per client)."""
    ph = login(client, world.passwords, "platform@shifde.local")
    group = post(client, "/api/v1/platform/groups", ph, {"slug": "g-x", "name": "G X"})
    ah = login(client, world.passwords, ADMIN)
    role = post(
        client, f"{A}/roles", ah, {"key": "ro", "name": "ro", "permissions": ["audit:read"]}
    )
    bound = client.post(
        f"{A}/role-bindings",
        headers=ah,
        json={
            "subject_type": "group",
            "subject_id": group["id"],
            "role_id": role["id"],
            "scope": {"type": "tenant", "id": str(tenant_id(owner_session, "demo-a"))},
        },
    )
    assert bound.status_code == 201, bound.text
    ph = login(client, world.passwords, "platform@shifde.local")
    viewer = owner_session.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = 'viewer@demo-a.local'")
    ).scalar_one()
    filled = client.put(
        f"/api/v1/platform/groups/{group['id']}/members",
        json={"user_ids": [str(viewer)], "row_version": group["row_version"]},
        headers=ph,
    )
    assert filled.status_code == 200, filled.text
    vh = login(client, world.passwords, "viewer@demo-a.local")
    assert "audit:read" in client.get(f"{A}/me/permissions", headers=vh).json()["permissions"]
    bh = login(client, world.passwords, ADMIN_B)
    assert client.get(f"{A}/audit-events", headers=bh).status_code == 404
    roles_b = client.get(f"{B}/roles", headers=bh).json()["items"]
    assert all(r["name"] != "ro" for r in roles_b)  # the role bound in A is not B's
    assert client.get(f"{B}/groups", headers=bh).json()["items"] == []
