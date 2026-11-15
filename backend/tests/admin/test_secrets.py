"""The secret routes (admin part 3b, B-1641): SETH 3ba0e06 `test_p2_secrets.py` tests that drive
them (carry_forward row 38), assertions unchanged.

SETH's seeded `demo-a`/`demo-b` world is the tenancy route world, as in
`tests/admin/test_credentials.py`: `A`/`B` are its two clients, `ADMIN` is A's owner and
`admin@demo-b.local` is B's owner. SETH's at-rest query read the only secret of its database;
other tests' rows share this one, so it names A's client and the secret.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests.admin.conftest import admin_client
from tests.identity.harness import PASSWORD
from tests.identity.harness import login as harness_login
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

VALUE = "Sup3r-Secret-Value-12345"


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with admin_client(settings) as c:
        yield c


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    """SETH's `p2util.login`: a fresh sign-in."""
    client.cookies.clear()
    return harness_login(client, email, password)


def post(client: TestClient, url: str, h: dict[str, str], body: dict[str, Any]) -> Any:
    r = client.post(url, json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_secret_api_is_write_only_and_versioned(
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    A = f"/api/v1/orgs/{world.a.slug}"
    h = login(client, people.owner_a)
    created = post(client, f"{A}/secrets", h, {"name": "db-password", "value": VALUE})
    assert set(created) == {
        "id",
        "name",
        "kind",
        "scope",
        "status",
        "version",
        "created_at",
        "rotated_at",
        "last_used_at",
        "row_version",
    }
    assert created["version"] == 1 and created["scope"] is None and created["status"] == "active"
    assert VALUE not in str(created)
    dup = client.post(
        f"{A}/secrets", json={"name": "db-password", "value": "other-value-1"}, headers=h
    )
    assert dup.status_code == 409 and "other-value-1" not in dup.text
    listing = client.get(f"{A}/secrets", headers=h)
    assert VALUE not in listing.text and [s["name"] for s in listing.json()["items"]] == [
        "db-password"
    ]
    rot = client.post(
        f"{A}/secrets/{created['id']}/rotate", json={"value": "Rotated-Value-98765"}, headers=h
    )
    assert rot.status_code == 200 and rot.json()["version"] == 2 and rot.json()["rotated_at"]
    assert "Rotated-Value-98765" not in rot.text
    stale = client.post(
        f"{A}/secrets/{created['id']}/rotate",
        json={"value": "x-value-1", "row_version": 1},
        headers=h,
    )
    assert stale.status_code == 409
    dis = client.patch(
        f"{A}/secrets/{created['id']}",
        json={"status": "disabled", "row_version": rot.json()["row_version"]},
        headers=h,
    )
    assert dis.status_code == 200 and dis.json()["status"] == "disabled"
    assert (
        client.post(
            f"{A}/secrets/{created['id']}/rotate", json={"value": "again-value-1"}, headers=h
        ).status_code
        == 409
    )
    bad_kind = client.post(
        f"{A}/secrets", json={"name": "k", "value": "v-value-1", "kind": "weird"}, headers=h
    )
    assert bad_kind.status_code == 422 and "v-value-1" not in bad_kind.text
    # at rest: ciphertext only, wrapped DEK, KEK id
    row = owner_session.execute(
        text(
            "SELECT ciphertext, wrapped_dek, nonce, kek_id FROM shifde.secrets "
            "WHERE tenant_id = :t AND name = 'db-password'"
        ),
        {"t": world.a.tenant},
    ).one()
    assert VALUE.encode() not in bytes(row[0]) and b"Rotated-Value" not in bytes(row[0])
    assert len(bytes(row[1])) == 12 + 32 + 16 and len(bytes(row[2])) == 12 and row[3]
    # nothing leaked into any log line produced by these requests
    seen = capsys.readouterr()
    for needle in (VALUE, "Rotated-Value-98765", "other-value-1", "v-value-1", "again-value-1"):
        assert needle not in seen.out and needle not in seen.err


def test_secret_scopes_validation_and_tenant_isolation(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, B = f"/api/v1/orgs/{world.a.slug}", f"/api/v1/orgs/{world.b.slug}"
    h = login(client, people.owner_a)
    ws = client.get(f"{A}/workspaces", headers=h).json()["items"][0]
    ok = post(
        client,
        f"{A}/secrets",
        h,
        {
            "name": "ws-secret",
            "value": "scoped-value-1",
            "scope": {"type": "workspace", "id": ws["id"]},
        },
    )
    assert ok["scope"] == {"type": "workspace", "id": ws["id"]}
    bad = (
        {"name": "n1", "value": "v-value-1", "scope": {"type": "workspace"}},
        {"name": "n2", "value": "v-value-1", "scope": {"type": "tenant", "id": ws["id"]}},
        {
            "name": "n3",
            "value": "v-value-1",
            "scope": {"type": "workspace", "id": str(UUID(int=7))},
        },
        {"name": "n4", "value": "v-value-1", "tenant_id": str(UUID(int=7))},
    )
    for body in bad:
        assert client.post(f"{A}/secrets", json=body, headers=h).status_code == 422, body
    hb = login(client, people.owner_b)
    other = post(
        client, f"{B}/secrets", hb, {"name": "ws-secret", "value": "b-value-1"}
    )  # same name, other tenant
    assert client.get(f"{B}/secrets", headers=hb).json()["items"][0]["id"] == other["id"]
    # tenant B cannot rotate/disable tenant A's secret by id
    assert (
        client.post(
            f"{B}/secrets/{ok['id']}/rotate", json={"value": "stolen-value-1"}, headers=hb
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{B}/secrets/{ok['id']}", json={"status": "disabled", "row_version": 1}, headers=hb
        ).status_code
        == 404
    )
    assert client.get(f"{A}/secrets", headers=hb).status_code == 404
