"""Cross-client isolation of the vendor catalog (B-1301).

Rows: `vendors` holds the global catalog (`tenant_id` NULL, readable by every client, written only
in the platform-administrator context) and each client's private vendors. Client A cannot read,
list, change or insert client B's, nor write a global row, through SQL as `shifde_app`.

Routes (`tests/vendors/conftest.py`): every vendor route has a test here (`ROUTE_TESTS`) that
first gets the caller's own object (200), then asks for B's under B's org and under A's and gets
exactly the answer a missing slug or id gets (404), and finds B's rows absent from A's lists.
"""

from __future__ import annotations

from typing import Any  # Any: JSON response bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, platform_admin_context
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.vendors import api as vendors_api
from shifde.vendors import repository
from tests.identity.harness import login, person
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People
from tests.vendors.conftest import vendors_client

_VARYING = {"instance", "request_id"}
_NEW = {"key": "new-one", "name": "New", "category": "other"}


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    """Refused exactly like a slug or id that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


def _keys(r: httpx.Response) -> set[str]:
    assert r.status_code == 200, r.text
    return {v["key"] for v in r.json()["items"]}


def _make(c: TestClient, org: str, headers: dict[str, str], key: str) -> dict[str, Any]:
    r = c.post(f"/api/v1/orgs/{org}/vendors", json={**_NEW, "key": key}, headers=headers)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


# ------------------------------------------------------------------ SQL as shifde_app
def _insert(s: Session, vendor_id: UUID, tenant: UUID | None) -> None:
    repository.insert_vendor(
        s,
        {
            "id": vendor_id,
            "tenant_id": tenant,
            "key": f"k-{vendor_id.hex[-12:]}",
            "name": "n",
            "category": "other",
            "description": None,
            "version": None,
            "status": "active",
            "configuration_requirements": [],
            "adapter_keys": [],
            "created_by": None,
        },
    )


def test_a_sees_the_global_rows_and_its_own_but_never_bs(world: World, app_db: Database) -> None:
    a, b = world.a, world.b
    va, vb = uuid7(), uuid7()
    with app_session(app_db, a.tenant) as s:
        _insert(s, va, a.tenant)
        s.commit()
    with app_session(app_db, b.tenant) as s:
        _insert(s, vb, b.tenant)
        s.commit()
    with app_session(app_db, a.tenant) as s:
        assert repository.get_vendor(s, va) is not None
        assert repository.get_vendor(s, vb) is None
        rows = repository.list_vendors(
            s, category=None, status=None, only_global=False, after=None, limit=500
        )
        ids = {r["id"] for r in rows}
        assert va in ids and vb not in ids
        assert {r["tenant_id"] for r in rows} <= {None, a.tenant}
        assert repository.update_vendor(s, vb, None, {"name": "taken"}) is None
        s.rollback()
    with app_session(app_db, None) as s:  # no client context: the global rows only
        rows = repository.list_vendors(
            s, category=None, status=None, only_global=False, after=None, limit=500
        )
        assert rows and all(r["tenant_id"] is None for r in rows)
        assert repository.get_vendor(s, va) is None


def test_a_client_cannot_write_for_another_client_or_the_global_catalog(
    world: World, app_db: Database, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with app_session(app_db, a.tenant) as s:
        for tenant in (b.tenant, None):  # a row for B, a row every client would see
            with pytest.raises(DBAPIError):
                _insert(s, uuid7(), tenant)
            s.rollback()
        glob = s.execute(
            text("SELECT id FROM shifde.vendors WHERE tenant_id IS NULL AND key = 'snowflake'")
        ).scalar_one()
        assert repository.update_vendor(s, glob, None, {"name": "Hacked"}) is None
        s.rollback()
        with pytest.raises(DBAPIError):  # no column grant: a vendor cannot change client
            s.execute(
                text("UPDATE shifde.vendors SET tenant_id = :b WHERE id = :i"),
                {"b": b.tenant, "i": glob},
            )
        s.rollback()
        with pytest.raises(DBAPIError):  # and no DELETE grant
            s.execute(text("DELETE FROM shifde.vendors WHERE id = :i"), {"i": glob})
        s.rollback()
    name = owner_session.execute(
        text("SELECT name FROM shifde.vendors WHERE id = :i"), {"i": glob}
    ).scalar_one()
    assert name == "Snowflake"
    with app_session(app_db, None) as s:  # the platform administrator's context writes global rows
        with platform_admin_context(s):
            _insert(s, uuid7(), None)
        s.commit()
        owner_session.execute(
            text("DELETE FROM shifde.vendors WHERE created_by IS NULL AND key LIKE 'k-%'")
        )
        owner_session.commit()


# ------------------------------------------------------------------ routes
ROUTE_TESTS = {
    ("GET", "/api/v1/adapters"): "test_adapter_routes_hold_no_client_data",
    ("GET", "/api/v1/adapters/{key}/conformance-runs"): "test_adapter_routes_hold_no_client_data",
    ("GET", "/api/v1/orgs/{org}/vendors"): "test_a_cannot_list_or_create_in_bs_vendors",
    ("POST", "/api/v1/orgs/{org}/vendors"): "test_a_cannot_list_or_create_in_bs_vendors",
    ("GET", "/api/v1/orgs/{org}/vendors/{vendor_id}"): "test_a_cannot_read_or_change_bs_vendor",
    ("PATCH", "/api/v1/orgs/{org}/vendors/{vendor_id}"): "test_a_cannot_read_or_change_bs_vendor",
    ("POST", "/api/v1/orgs/{org}/vendors/{vendor_id}/status"): (
        "test_a_cannot_read_or_change_bs_vendor"
    ),
    ("GET", "/api/v1/orgs/{org}/vendors/{vendor_id}/capabilities"): (
        "test_a_cannot_read_or_change_bs_vendor"
    ),
    ("GET", "/api/v1/platform/vendors"): "test_the_platform_routes_show_only_the_global_catalog",
    ("POST", "/api/v1/platform/vendors"): "test_the_platform_routes_show_only_the_global_catalog",
    ("GET", "/api/v1/platform/vendors/{vendor_id}"): (
        "test_the_platform_routes_show_only_the_global_catalog"
    ),
    ("PATCH", "/api/v1/platform/vendors/{vendor_id}"): (
        "test_the_platform_routes_show_only_the_global_catalog"
    ),
}


def test_every_vendor_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in vendors_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


def test_a_cannot_list_or_create_in_bs_vendors(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with vendors_client(settings) as c:
        ha = login(c, people.owner_a)
        own = _make(c, a.slug, ha, "a-private")
        assert "a-private" in _keys(c.get(f"/api/v1/orgs/{a.slug}/vendors?limit=200"))
        assert own["tenant_id"] == str(a.tenant)
    with vendors_client(settings) as c:
        hb = login(c, people.owner_b)
        _make(c, b.slug, hb, "b-private")
    with vendors_client(settings) as c:
        ha = login(c, people.owner_a)
        listed = _keys(c.get(f"/api/v1/orgs/{a.slug}/vendors?limit=200"))
        assert "a-private" in listed and "b-private" not in listed
        assert "snowflake" in listed  # the global catalog
        _same_404(
            c.get(f"/api/v1/orgs/{b.slug}/vendors"), c.get("/api/v1/orgs/no-such-client/vendors")
        )
        _same_404(
            c.post(f"/api/v1/orgs/{b.slug}/vendors", json=_NEW, headers=ha),
            c.post("/api/v1/orgs/no-such-client/vendors", json=_NEW, headers=ha),
        )
    count = owner_session.execute(
        text("SELECT count(*) FROM shifde.vendors WHERE tenant_id = :t AND key = 'new-one'"),
        {"t": b.tenant},
    ).scalar_one()
    assert count == 0


def test_a_cannot_read_or_change_bs_vendor(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with vendors_client(settings) as c:
        hb = login(c, people.owner_b)
        theirs = _make(c, b.slug, hb, "b-secret")
        assert c.get(f"/api/v1/orgs/{b.slug}/vendors/{theirs['id']}").status_code == 200
    with vendors_client(settings) as c:
        ha = login(c, people.owner_a)
        mine = _make(c, a.slug, ha, "a-mine")
        base = f"/api/v1/orgs/{a.slug}/vendors"
        assert c.get(f"{base}/{mine['id']}").status_code == 200
        assert c.get(f"{base}/{mine['id']}/capabilities").status_code == 200
        patch = {"name": "renamed", "row_version": mine["row_version"]}
        assert c.patch(f"{base}/{mine['id']}", json=patch, headers=ha).status_code == 200
        status = {"status": "disabled"}
        assert c.post(f"{base}/{mine['id']}/status", json=status, headers=ha).status_code == 200
        ghost = uuid7()
        theirs_id = theirs["id"]
        for suffix in ("", "/capabilities"):  # B's id under A's org and under B's
            _same_404(c.get(f"{base}/{theirs_id}{suffix}"), c.get(f"{base}/{ghost}{suffix}"))
            _same_404(
                c.get(f"/api/v1/orgs/{b.slug}/vendors/{theirs_id}{suffix}"),
                c.get(f"/api/v1/orgs/no-such-client/vendors/{theirs_id}{suffix}"),
            )
        _same_404(
            c.patch(f"{base}/{theirs_id}", json=patch, headers=ha),
            c.patch(f"{base}/{ghost}", json=patch, headers=ha),
        )
        _same_404(
            c.post(f"{base}/{theirs_id}/status", json=status, headers=ha),
            c.post(f"{base}/{ghost}/status", json=status, headers=ha),
        )
        _same_404(
            c.patch(f"/api/v1/orgs/{b.slug}/vendors/{theirs_id}", json=patch, headers=ha),
            c.patch(f"/api/v1/orgs/no-such-client/vendors/{theirs_id}", json=patch, headers=ha),
        )
    row = owner_session.execute(
        text("SELECT name, status FROM shifde.vendors WHERE id = :i"), {"i": UUID(theirs_id)}
    ).one()
    assert (row.name, row.status) == (_NEW["name"], "active")


def test_the_platform_routes_show_only_the_global_catalog(
    settings: Settings, world: World, people: People
) -> None:
    a = world.a
    with vendors_client(settings) as c:
        ha = login(c, people.owner_a)
        mine = _make(c, a.slug, ha, "a-private")
        for method, url in (
            ("get", "/api/v1/platform/vendors"),
            ("get", f"/api/v1/platform/vendors/{mine['id']}"),
        ):  # a client user, even an owner, is not the platform administrator
            assert getattr(c, method)(url).status_code == 403
        assert c.post("/api/v1/platform/vendors", json=_NEW, headers=ha).status_code == 403
        patch = {"name": "x"}
        assert (
            c.patch(f"/api/v1/platform/vendors/{mine['id']}", json=patch, headers=ha).status_code
            == 403
        )
    with vendors_client(settings) as c:
        hp = login(c, people.platform)
        created = c.post("/api/v1/platform/vendors", json=_NEW, headers=hp)
        assert created.status_code == 201 and created.json()["tenant_id"] is None
        gid = created.json()["id"]
        assert c.get(f"/api/v1/platform/vendors/{gid}").status_code == 200
        assert "a-private" not in _keys(c.get("/api/v1/platform/vendors?limit=200"))
        ghost = uuid7()
        _same_404(
            c.get(f"/api/v1/platform/vendors/{mine['id']}"),
            c.get(f"/api/v1/platform/vendors/{ghost}"),
        )
        _same_404(
            c.patch(f"/api/v1/platform/vendors/{mine['id']}", json={"name": "x"}, headers=hp),
            c.patch(f"/api/v1/platform/vendors/{ghost}", json={"name": "x"}, headers=hp),
        )
        patched = c.patch(f"/api/v1/platform/vendors/{gid}", json={"name": "Renamed"}, headers=hp)
        assert patched.status_code == 200 and patched.json()["name"] == "Renamed"


def test_adapter_routes_hold_no_client_data(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    with vendors_client(settings) as c:
        assert c.get("/api/v1/adapters").status_code == 401
        assert c.get("/api/v1/adapters/postgres/conformance-runs").status_code == 401
        login(c, people.owner_a)
        mine = c.get("/api/v1/adapters")
        assert mine.status_code == 200 and mine.json()["items"]
        assert c.get("/api/v1/adapters/postgres/conformance-runs").status_code == 200
    with vendors_client(settings) as c:
        login(c, people.owner_b)
        assert c.get("/api/v1/adapters").json() == mine.json()  # the same registry for everyone
    # a person with no membership holds `vendor:read` nowhere: the evidence is refused
    stranger = f"stranger-{uuid7().hex[-12:]}@route.test"
    person(owner_session, stranger)
    with vendors_client(settings) as c:
        login(c, stranger)
        assert c.get("/api/v1/adapters/postgres/conformance-runs").status_code == 403
