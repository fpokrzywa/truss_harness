"""A suspended client (findings Item 12, F-03, B-2511), as `shifde_app` under forced RLS.

Only the platform administrator suspends an active client or unsuspends a suspended one
(`POST /api/v1/platform/orgs/{org}/suspend|unsuspend`); everyone else gets one 403 whatever the
slug, and each change is one audit row in that client's own log. A session pinned to a
suspended client is refused like one pinned to an archived client, and resolves again after
unsuspend; no run starts in a suspended client. The owner sees the status but cannot edit, or
unsuspend, the client.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.db import Database
from shifde.core.errors import NotFound, ScopeArchived
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import repository as identity_repo
from shifde.identity import service as identity_service
from shifde.runtime import schemas as rs
from shifde.runtime.governance import PolicyBudgetHooks
from shifde.runtime.service import RunService
from shifde.tenancy import service as tenancy
from tests._support.tenants import bind_user
from tests.build.service_support import REGISTRY, service_for
from tests.identity.harness import COOKIE, login
from tests.tenancy.conftest import Client, World, app_session, owner_member
from tests.tenancy.routes import People, make_people, pinned_session, tenancy_client

SUSPENDED, UNSUSPENDED = "tenancy.org.suspended", "tenancy.org.unsuspended"
_VARYING = {"instance", "request_id"}


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


def _url(slug: str, action: str = "suspend") -> str:
    return f"/api/v1/platform/orgs/{slug}/{action}"


def _user_id(owner: Session, email: str) -> UUID:
    return owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
    ).scalar_one()


def _row(owner: Session, tenant: UUID) -> tuple[str, int]:
    owner.rollback()  # a fresh snapshot
    row = owner.execute(
        text("SELECT status, row_version FROM shifde.tenants WHERE id = :t"), {"t": tenant}
    ).one()
    return row[0], row[1]


def _audit(owner: Session, tenant: UUID) -> list[tuple[str, dict[str, object]]]:
    owner.rollback()
    rows = owner.execute(
        text(
            "SELECT action, details FROM shifde.audit_events WHERE tenant_id = :t "
            "AND action IN (:s, :u) ORDER BY occurred_at, id"
        ),
        {"t": tenant, "s": SUSPENDED, "u": UNSUSPENDED},
    )
    return [(r[0], r[1]) for r in rows]


def _set_status(owner: Session, tenant: UUID, status: str) -> None:
    owner.execute(
        text("UPDATE shifde.tenants SET status = :s WHERE id = :t"), {"s": status, "t": tenant}
    )
    owner.commit()


def _same(r: httpx.Response, reference: httpx.Response, status: int) -> None:
    assert r.status_code == reference.status_code == status, (r.status_code, r.text)
    assert {k: v for k, v in r.json().items() if k not in _VARYING} == {
        k: v for k, v in reference.json().items() if k not in _VARYING
    }


# ------------------------------------------------------------------ (a) suspend and unsuspend
def test_the_platform_administrator_suspends_and_unsuspends_a_client(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    _, version = _row(owner_session, a.tenant)
    with tenancy_client(settings) as c:
        headers = login(c, people.platform)
        r = c.post(_url(a.slug), headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"id": str(a.tenant), "slug": a.slug, "name": a.slug,
                            "status": "suspended"}  # fmt: skip
        assert _row(owner_session, a.tenant) == ("suspended", version + 1)
        [(action, details)] = _audit(owner_session, a.tenant)
        assert action == SUSPENDED and details["by_platform_administrator"] is True
        listed = c.get("/api/v1/orgs?scope=all&limit=200")
        assert {"slug": a.slug, "status": "suspended"}.items() <= next(
            o for o in listed.json()["items"] if o["slug"] == a.slug
        ).items()
        r = c.post(_url(a.slug, "unsuspend"), headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"
    assert _row(owner_session, a.tenant) == ("active", version + 2)
    assert [act for act, _ in _audit(owner_session, a.tenant)] == [SUSPENDED, UNSUSPENDED]
    assert _audit(owner_session, b.tenant) == []
    owner_session.rollback()
    assert (
        owner_session.execute(
            text(
                "SELECT count(*) FROM shifde.audit_events WHERE action IN (:s, :u) "
                "AND (tenant_id IS NULL OR tenant_id <> :t)"
            ),
            {"s": SUSPENDED, "u": UNSUSPENDED, "t": a.tenant},
        ).scalar_one()  # fmt: skip
        == 0
    )


# ------------------------------------------------------------------ (b) everyone else: 403
def test_everyone_but_the_platform_administrator_gets_one_403_whatever_the_slug(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    before = _row(owner_session, a.tenant)

    def refused(c: httpx.Client, headers: dict[str, str]) -> None:
        for action in ("suspend", "unsuspend"):
            missing = c.post(_url("no-such-client", action), headers=headers)
            assert missing.status_code == 403, missing.text
            for slug in (a.slug, world.b.slug):
                _same(c.post(_url(slug, action), headers=headers), missing, 403)

    for email in (people.owner_a, people.viewer_a, people.owner_b, people.both):
        with tenancy_client(settings) as c:
            refused(c, login(c, email))
    owner_a = _user_id(owner_session, people.owner_a)
    token, csrf = pinned_session(owner_session, owner_a, a.tenant)
    with tenancy_client(settings, cookies={COOKIE: token}) as c:
        assert c.get(f"/api/v1/orgs/{a.slug}").status_code == 200  # the pin is valid
        refused(c, {"X-CSRF-Token": csrf})
    admin = _user_id(owner_session, people.platform)  # made A's owner so the pin is valid
    owner_member(owner_session, a.tenant, admin)
    bind_user(owner_session, a.tenant, admin, "tenant_administrator")
    owner_session.commit()
    token, csrf = pinned_session(owner_session, admin, a.tenant)
    with tenancy_client(settings, cookies={COOKIE: token}) as c:
        assert c.get(f"/api/v1/orgs/{a.slug}").status_code == 200
        refused(c, {"X-CSRF-Token": csrf})
    assert _row(owner_session, a.tenant) == before
    assert _audit(owner_session, a.tenant) == []
    with tenancy_client(settings) as c:  # not vacuous: the unpinned administrator may
        assert c.post(_url(a.slug), headers=login(c, people.platform)).status_code == 200


# ------------------------------------------------------------------ (c) only from the right state
def test_suspend_needs_an_active_client_and_unsuspend_a_suspended_one(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with tenancy_client(settings) as c:
        headers = login(c, people.platform)
        assert c.post(_url("no-such-client"), headers=headers).status_code == 404
        unsuspend_active = c.post(_url(a.slug, "unsuspend"), headers=headers)
        assert unsuspend_active.status_code == 409 and unsuspend_active.json()["code"] == "conflict"
        assert c.post(_url(a.slug), headers=headers).status_code == 200
        before = _row(owner_session, a.tenant)
        assert c.post(_url(a.slug), headers=headers).status_code == 409  # already suspended
        assert _row(owner_session, a.tenant) == before
        _set_status(owner_session, b.tenant, "archived")
        archived = _row(owner_session, b.tenant)
        for action in ("suspend", "unsuspend"):
            r = c.post(_url(b.slug, action), headers=headers)
            assert r.status_code == 409, (action, r.text)
        assert _row(owner_session, b.tenant) == archived
    assert [act for act, _ in _audit(owner_session, a.tenant)] == [SUSPENDED]
    assert _audit(owner_session, b.tenant) == []


# ------------------------------------------------------------------ (d) a pin to a suspended client
def test_a_pin_to_a_suspended_client_is_refused_like_an_archived_one_until_unsuspended(
    settings: Settings, world: World, people: People, app_db: Database, owner_session: Session
) -> None:
    a, b = world.a, world.b
    owner_a, owner_b = (_user_id(owner_session, e) for e in (people.owner_a, people.owner_b))
    pin_a, _ = pinned_session(owner_session, owner_a, a.tenant)
    pin_b, _ = pinned_session(owner_session, owner_b, b.tenant)
    hashes = {t: identity_service.hash_token(p) for t, p in ((a.tenant, pin_a), (b.tenant, pin_b))}
    with tenancy_client(settings, cookies={COOKIE: pin_a}) as c:
        assert c.get(f"/api/v1/orgs/{a.slug}").status_code == 200  # resolves before
    with tenancy_client(settings) as c:
        assert c.post(_url(a.slug), headers=login(c, people.platform)).status_code == 200
    _set_status(owner_session, b.tenant, "archived")
    with app_session(app_db, None) as s:
        assert identity_repo.resolve_session(s, hashes[a.tenant]) is None
        assert identity_repo.resolve_session(s, hashes[b.tenant]) is None
    with tenancy_client(settings, cookies={COOKIE: pin_a}) as c:
        suspended = c.get(f"/api/v1/orgs/{a.slug}")
    with tenancy_client(settings, cookies={COOKIE: pin_b}) as c:
        archived = c.get(f"/api/v1/orgs/{b.slug}")
    _same(suspended, archived, 401)
    with tenancy_client(settings) as c:
        r = c.post(_url(a.slug, "unsuspend"), headers=login(c, people.platform))
        assert r.status_code == 200
    with app_session(app_db, None) as s:
        assert identity_repo.resolve_session(s, hashes[a.tenant]) is not None
    with tenancy_client(settings, cookies={COOKIE: pin_a}) as c:
        assert c.get(f"/api/v1/orgs/{a.slug}").status_code == 200


# ------------------------------------------------------------------ (e) no run starts
def _runs(owner: Session, tenant: UUID) -> int:
    owner.rollback()
    return int(
        owner.execute(
            text("SELECT count(*) FROM shifde.runs WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
    )


def _try_run(app_db: Database, settings: Settings, c: Client, user: UUID) -> None:
    """`user`'s run in `c`, with the client status the database holds (as `resolve_org` reads
    it); the target does not exist, so a run that passes the client gate is a 404."""
    with app_session(app_db, c.tenant, user) as s:
        header = tenancy.tenant_header(s, c.tenant)
        assert header is not None
        sc = service_for(s, c.tenant, user, settings, status=header[2]).sc
        runs = RunService(sc, IntegrationRuntime(settings=settings, registry=REGISTRY),
                          PolicyBudgetHooks())  # fmt: skip
        body = rs.RunCreate(kind="agent", agent_id=uuid7(), version_id=uuid7(),
                            workspace="w", project="p", input="hello")  # fmt: skip
        runs.create(body, None)


def test_no_run_starts_in_a_suspended_client(
    settings: Settings, world: World, people: People, app_db: Database, owner_session: Session
) -> None:
    a = world.a
    owner_a = _user_id(owner_session, people.owner_a)
    with pytest.raises(NotFound):  # active: past the client gate (not vacuous)
        _try_run(app_db, settings, a, owner_a)
    with tenancy_client(settings) as c:
        assert c.post(_url(a.slug), headers=login(c, people.platform)).status_code == 200
    before = _runs(owner_session, a.tenant)
    with pytest.raises(ScopeArchived):
        _try_run(app_db, settings, a, owner_a)
    assert _runs(owner_session, a.tenant) == before


# ------------------------------------------------------------------ (f) (g) the owner's view
def test_the_owner_sees_the_suspension_and_cannot_edit_or_lift_it(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    with tenancy_client(settings) as c:
        assert c.post(_url(a.slug), headers=login(c, people.platform)).status_code == 200
    before = _row(owner_session, a.tenant)
    with tenancy_client(settings) as c:
        headers = login(c, people.owner_a)
        listed = c.get("/api/v1/orgs")
        assert [(o["slug"], o["status"]) for o in listed.json()["items"]] == [(a.slug, "suspended")]
        own = c.get(f"/api/v1/orgs/{a.slug}")
        assert own.status_code == 200 and own.json()["status"] == "suspended"
        version = own.json()["row_version"]
        for patch in ({"status": "active"}, {"status": "archived"}, {"name": "renamed"}):
            r = c.patch(f"/api/v1/orgs/{a.slug}", json={**patch, "row_version": version},
                        headers=headers)  # fmt: skip
            assert r.status_code == 409, (patch, r.text)
            assert r.json()["code"] == "scope_archived"
            assert r.json()["detail"] == "organization is suspended"
    assert _row(owner_session, a.tenant) == before


def test_the_owner_cannot_patch_a_client_to_suspended(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a = world.a
    before = _row(owner_session, a.tenant)
    with tenancy_client(settings) as c:
        headers = login(c, people.owner_a)
        body = {"status": "suspended", "row_version": before[1]}
        r = c.patch(f"/api/v1/orgs/{a.slug}", json=body, headers=headers)
        assert r.status_code == 422, r.text
    assert _row(owner_session, a.tenant) == before
