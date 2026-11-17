"""Cross-client isolation of tenancy's data layer, as `shifde_app` (B-116 item 12).

Client A cannot read, list or change client B's tenant row, workspaces or projects through the
repository or raw SQL, nothing is visible without a client context, and the
platform-administrator flag reaches the list of clients only, never a client's workspaces or
projects (B-116 item 10).

Routes (part 2b, `tests/tenancy/routes.py`): every tenancy route has a test here (`ROUTE_TESTS`)
that first gets the caller's own object (200), then asks for B's under B's org and under A's
and gets exactly the answer a missing slug gets (404), and finds B's rows absent from A's lists.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, platform_admin_context, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity import apikeys
from shifde.tenancy import api as tenancy_api
from shifde.tenancy import repository
from tests._support.fake_oidc import resolver as fake_oidc_resolver
from tests._support.tenants import bind_user
from tests.identity import oidc_flow
from tests.identity.harness import COOKIE, login
from tests.tenancy.conftest import World, app_session, owner_member
from tests.tenancy.routes import (
    People,
    make_people,
    pinned_session,
    project_slug,
    tenancy_client,
    ws2_slug,
    ws_slug,
)

LIMIT = 100


def test_a_sees_only_its_own_tenant_row(world: World, app_db: Database) -> None:
    a, b = world.a, world.b
    with app_session(app_db, a.tenant) as s:
        own = repository.get_tenant(s, a.tenant)
        assert own is not None and own.slug == a.slug
        assert repository.get_tenant(s, b.tenant) is None
        assert repository.get_tenant_by_slug(s, b.slug) is None
        assert not repository.tenant_slug_exists(s, b.slug)
        listed = repository.list_tenants(s, include_archived=True, after=None, limit=LIMIT)
        assert [t.id for t in listed] == [a.tenant]


def test_a_cannot_read_or_list_bs_workspaces_or_projects(world: World, app_db: Database) -> None:
    a, b = world.a, world.b
    with app_session(app_db, a.tenant) as s:
        for tenant in (a.tenant, b.tenant):  # B's ids under A's key or under B's
            assert repository.get_workspace(s, tenant, b.ws) is None
            assert repository.get_project_by_id(s, tenant, b.project) is None
        assert repository.get_workspace_by_slug(s, b.tenant, "anything") is None
        assert (
            repository.list_workspaces(
                s, tenant_id=b.tenant, only_ids=None, include_archived=True, after=None, limit=9
            )
            == []
        )
        assert repository.workspace_ids(s, b.tenant, None) == []
        assert repository.count_workspaces(s, b.tenant, None) == 0
        assert (
            repository.list_projects(
                s,
                tenant_id=b.tenant,
                workspace_id=b.ws,
                visible_workspace_ids=None,
                visible_project_ids=(),
                include_archived=True,
                after=None,
                limit=LIMIT,
            )
            == []
        )
        assert (
            repository.count_projects(
                s,
                tenant_id=b.tenant,
                workspace_id=None,
                visible_workspace_ids=None,
                visible_project_ids=(),
            )
            == 0
        )
        for table in ("tenants", "workspaces", "projects"):
            column = "id" if table == "tenants" else "tenant_id"
            rows = s.execute(text(f"SELECT {column} FROM shifde.{table}")).scalars().all()
            assert set(rows) == {a.tenant}, table
        own = repository.workspace_ids(s, a.tenant, None)
        assert set(own) == {a.ws, a.ws2}


def test_a_cannot_change_bs_tenant_workspaces_or_projects(
    world: World, app_db: Database, owner_db: Database
) -> None:
    a, b = world.a, world.b
    with app_session(app_db, a.tenant) as s:
        assert repository.update_tenant(s, b.tenant, 1, {"name": "taken"}) is None
        assert repository.update_workspace(s, b.tenant, b.ws, 1, {"name": "taken"}) is None
        assert repository.update_workspace(s, a.tenant, b.ws, 1, {"name": "taken"}) is None
        assert repository.update_project(s, b.tenant, b.project, 1, {"name": "taken"}) is None
        for table, key in (("tenants", b.tenant), ("workspaces", b.ws), ("projects", b.project)):
            changed = s.execute(
                text(f"UPDATE shifde.{table} SET name = 'taken' WHERE id = :i"), {"i": key}
            )
            assert changed.rowcount == 0, table
        s.commit()
    with owner_db.session() as owner:
        for table, key in (("tenants", b.tenant), ("workspaces", b.ws), ("projects", b.project)):
            name = owner.execute(
                text(f"SELECT name FROM shifde.{table} WHERE id = :i"), {"i": key}
            ).scalar_one()
            assert name != "taken", table


def test_a_cannot_create_rows_in_b_or_hang_a_project_on_bs_workspace(
    world: World, app_db: Database
) -> None:
    a, b = world.a, world.b
    with (
        app_session(app_db, a.tenant) as s,
        pytest.raises(DBAPIError, match="row-level security"),
    ):
        repository.insert_workspace(
            s, {"id": uuid7(), "tenant_id": b.tenant, "slug": "w", "name": "w"}
        )
    with (
        app_session(app_db, a.tenant) as s,
        pytest.raises(DBAPIError, match="row-level security"),
    ):
        repository.insert_tenant(s, {"id": uuid7(), "slug": f"t-{uuid7().hex}", "name": "t"})
    with app_session(app_db, a.tenant) as s:
        # the composite key is checked as the owner: B's workspace is unknown under A's key,
        # exactly like a workspace that does not exist
        for workspace in (b.ws, uuid7()):
            with pytest.raises(DBAPIError, match="fk_projects__workspace"):
                repository.insert_project(
                    s,
                    {
                        "id": uuid7(),
                        "tenant_id": a.tenant,
                        "workspace_id": workspace,
                        "slug": "p",
                        "name": "p",
                    },
                )
            s.rollback()
            set_tenant(s, a.tenant)


def test_nothing_is_visible_without_a_client_context(world: World, app_db: Database) -> None:
    with app_session(app_db, None, world.a.member) as s:
        for table in ("tenants", "workspaces", "projects"):
            count = s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one()
            assert count == 0, table
        assert repository.list_tenants(s, include_archived=True, after=None, limit=LIMIT) == []
        assert repository.get_tenant(s, world.a.tenant) is None
        # installation settings are installation-wide (global_tables.txt), not client data
        assert set(repository.get_installation_settings(s)) == {
            "branding",
            "feature_flags",
            "auth_modes",
            "default_limits",
        }


def test_the_admin_flag_lists_clients_but_reaches_no_workspace_or_project(
    world: World, app_db: Database
) -> None:
    with app_session(app_db, None) as s, platform_admin_context(s):
        ids = {
            t.id for t in repository.list_tenants(s, include_archived=True, after=None, limit=10**6)
        }
        assert {world.a.tenant, world.b.tenant} <= ids
        for table in ("workspaces", "projects"):
            count = s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one()
            assert count == 0, table
        assert repository.get_workspace(s, world.b.tenant, world.b.ws) is None


# ------------------------------------------------------------------ routes (part 2b)
ROUTE_TESTS = {
    ("GET", "/api/v1/orgs"): "test_org_lists_show_only_the_callers_clients",
    ("POST", "/api/v1/orgs"): "test_only_the_platform_administrator_creates_a_client",
    ("GET", "/api/v1/orgs/{org}"): "test_a_cannot_read_or_change_bs_client",
    ("PATCH", "/api/v1/orgs/{org}"): "test_a_cannot_read_or_change_bs_client",
    ("GET", "/api/v1/orgs/{org}/members"): "test_members_are_listed_only_in_their_own_client",
    ("GET", "/api/v1/orgs/{org}/workspaces"): (
        "test_a_cannot_list_read_create_or_change_bs_workspaces"
    ),
    ("POST", "/api/v1/orgs/{org}/workspaces"): (
        "test_a_cannot_list_read_create_or_change_bs_workspaces"
    ),
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}"): (
        "test_a_cannot_list_read_create_or_change_bs_workspaces"
    ),
    ("PATCH", "/api/v1/orgs/{org}/workspaces/{ws}"): (
        "test_a_cannot_list_read_create_or_change_bs_workspaces"
    ),
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}/projects"): (
        "test_a_cannot_list_read_create_or_change_bs_projects"
    ),
    ("POST", "/api/v1/orgs/{org}/workspaces/{ws}/projects"): (
        "test_a_cannot_list_read_create_or_change_bs_projects"
    ),
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}"): (
        "test_a_cannot_list_read_create_or_change_bs_projects"
    ),
    ("PATCH", "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}"): (
        "test_a_cannot_list_read_create_or_change_bs_projects"
    ),
    ("GET", "/api/v1/orgs/{org}/dashboard"): "test_the_dashboard_counts_only_its_own_client",
    ("GET", "/api/v1/platform/status"): (
        "test_platform_status_answers_the_platform_administrator_and_no_client"
    ),
}


def test_every_tenancy_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in tenancy_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


_VARYING = {"instance", "request_id"}


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    """Refused exactly like a slug or id that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


def _slugs(r: httpx.Response) -> set[str]:
    assert r.status_code == 200, r.text
    return {item["slug"] for item in r.json()["items"]}


def test_org_lists_show_only_the_callers_clients(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with tenancy_client(settings) as c:
        login(c, people.owner_a)
        assert _slugs(c.get("/api/v1/orgs")) == {a.slug}
        assert c.get("/api/v1/orgs?scope=all").status_code == 403
    with tenancy_client(settings) as c:
        login(c, people.both)
        assert _slugs(c.get("/api/v1/orgs")) == {a.slug, b.slug}
    token, _ = pinned_session(owner_session, people.both_id, b.tenant)
    with tenancy_client(settings, cookies={COOKIE: token}) as c:
        assert _slugs(c.get("/api/v1/orgs")) == {b.slug}  # pinned: B only
    with tenancy_client(settings) as c:
        login(c, people.platform)
        assert _slugs(c.get("/api/v1/orgs")) == set()  # the flag lists no client of its own
        everything = c.get("/api/v1/orgs?scope=all&include_archived=true&limit=200")
        assert everything.status_code == 200
        items = everything.json()["items"]
        assert all(set(o) == {"id", "slug", "name", "status"} for o in items)


def test_the_platform_administrators_client_list_pages_with_a_cursor(
    settings: Settings, world: World, people: People
) -> None:
    """A page that is not the last carries a cursor (it used to be read after the rollback,
    outside the platform-admin context, and failed with a 500), and the next page follows."""
    with tenancy_client(settings) as c:
        login(c, people.platform)
        first = c.get("/api/v1/orgs?scope=all&include_archived=true&limit=1")
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        assert cursor
        second = c.get(f"/api/v1/orgs?scope=all&include_archived=true&limit=1&cursor={cursor}")
        assert second.status_code == 200
        assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]


def test_only_the_platform_administrator_creates_a_client(
    settings: Settings, world: World, people: People
) -> None:
    slug = f"new-{uuid7().hex[-12:]}"
    with tenancy_client(settings) as c:
        headers = login(c, people.owner_a)
        r = c.post("/api/v1/orgs", json={"slug": slug, "name": "N"}, headers=headers)
        assert r.status_code == 403
        taken = c.post("/api/v1/orgs", json={"slug": world.b.slug, "name": "N"}, headers=headers)
        assert taken.status_code == 403  # B's slug: the same refusal, no conflict hint
    with tenancy_client(settings) as c:
        headers = login(c, people.platform)
        body = {"slug": slug, "name": "N", "admin_email": people.owner_b}
        created = c.post("/api/v1/orgs", json=body, headers=headers)
        assert created.status_code == 201, created.text
    for email, sees in ((people.owner_a, False), (people.owner_b, True)):
        with tenancy_client(settings) as c:
            login(c, email)
            assert (slug in _slugs(c.get("/api/v1/orgs"))) is sees
            assert c.get(f"/api/v1/orgs/{slug}").status_code == (200 if sees else 404)


def test_a_cannot_read_or_change_bs_client(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    with tenancy_client(settings) as c:
        headers = login(c, people.owner_a)
        own = c.get(f"/api/v1/orgs/{a.slug}")
        assert own.status_code == 200 and own.json()["id"] == str(a.tenant)
        missing = c.get("/api/v1/orgs/no-such-client")
        _same_404(c.get(f"/api/v1/orgs/{b.slug}"), missing)
        patch = {"name": "renamed", "row_version": own.json()["row_version"]}
        assert c.patch(f"/api/v1/orgs/{a.slug}", json=patch, headers=headers).status_code == 200
        _same_404(
            c.patch(f"/api/v1/orgs/{b.slug}", json=patch, headers=headers),
            c.patch("/api/v1/orgs/no-such-client", json=patch, headers=headers),
        )
    name = owner_session.execute(
        text("SELECT name FROM shifde.tenants WHERE id = :t"), {"t": b.tenant}
    ).scalar_one()
    assert name == b.slug
    with tenancy_client(settings) as c:  # and the other way round
        login(c, people.owner_b)
        assert c.get(f"/api/v1/orgs/{b.slug}").status_code == 200
        _same_404(c.get(f"/api/v1/orgs/{a.slug}"), c.get("/api/v1/orgs/no-such-client"))


def test_members_are_listed_only_in_their_own_client(
    settings: Settings, world: World, people: People
) -> None:
    with tenancy_client(settings) as c:
        login(c, people.owner_a)
        r = c.get(f"/api/v1/orgs/{world.a.slug}/members?limit=200")
        assert r.status_code == 200
        emails = {m["user"]["email"] for m in r.json()["items"]}
        assert people.owner_a in emails and people.both in emails
        assert people.owner_b not in emails
        _same_404(
            c.get(f"/api/v1/orgs/{world.b.slug}/members"),
            c.get("/api/v1/orgs/no-such-client/members"),
        )


def test_a_cannot_list_read_create_or_change_bs_workspaces(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    base_a, base_b = f"/api/v1/orgs/{a.slug}/workspaces", f"/api/v1/orgs/{b.slug}/workspaces"
    gone = "/api/v1/orgs/no-such-client/workspaces"
    with tenancy_client(settings) as c:
        headers = login(c, people.owner_a)
        listed = _slugs(c.get(f"{base_a}?limit=200"))
        assert ws_slug(a) in listed and not {ws_slug(b), ws2_slug(b)} & listed
        own = c.get(f"{base_a}/{ws_slug(a)}")
        assert own.status_code == 200
        _same_404(c.get(f"{base_a}/{ws_slug(b)}"), c.get(f"{base_a}/w-missing"))
        _same_404(c.get(f"{base_b}/{ws_slug(b)}"), c.get(f"{gone}/{ws_slug(b)}"))
        _same_404(c.get(base_b), c.get(gone))
        patch = {"name": "renamed", "row_version": own.json()["row_version"]}
        assert c.patch(f"{base_a}/{ws_slug(a)}", json=patch, headers=headers).status_code == 200
        for path, reference in (
            (f"{base_a}/{ws_slug(b)}", f"{base_a}/w-missing"),
            (f"{base_b}/{ws_slug(b)}", f"{gone}/{ws_slug(b)}"),
        ):
            _same_404(
                c.patch(path, json=patch, headers=headers),
                c.patch(reference, json=patch, headers=headers),
            )
        new = {"slug": f"n-{uuid7().hex[-12:]}", "name": "N"}
        assert c.post(base_a, json=new, headers=headers).status_code == 201
        _same_404(
            c.post(base_b, json=new, headers=headers),
            c.post(gone, json=new, headers=headers),
        )
    rows = owner_session.execute(
        text("SELECT slug, name FROM shifde.workspaces WHERE tenant_id = :t"), {"t": b.tenant}
    ).all()
    assert {r.slug for r in rows} == {ws_slug(b), ws2_slug(b)}
    assert all(r.name == r.slug for r in rows)  # B's names untouched


def test_a_cannot_list_read_create_or_change_bs_projects(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    a, b = world.a, world.b
    own_ws = f"/api/v1/orgs/{a.slug}/workspaces/{ws_slug(a)}/projects"
    targets = [  # (B's workspace, the same path with a missing workspace or client)
        (
            f"/api/v1/orgs/{a.slug}/workspaces/{ws_slug(b)}/projects",  # under A's org
            f"/api/v1/orgs/{a.slug}/workspaces/w-missing/projects",
        ),
        (
            f"/api/v1/orgs/{b.slug}/workspaces/{ws_slug(b)}/projects",  # under B's org
            f"/api/v1/orgs/no-such-client/workspaces/{ws_slug(b)}/projects",
        ),
    ]
    with tenancy_client(settings) as c:
        headers = login(c, people.owner_a)
        assert _slugs(c.get(own_ws)) == {project_slug(a)}
        own = c.get(f"{own_ws}/{project_slug(a)}")
        assert own.status_code == 200
        patch = {"name": "renamed", "row_version": own.json()["row_version"]}
        new = {"slug": f"n-{uuid7().hex[-12:]}", "name": "N"}
        assert (
            c.patch(f"{own_ws}/{project_slug(a)}", json=patch, headers=headers).status_code == 200
        )
        assert c.post(own_ws, json=new, headers=headers).status_code == 201
        # B's project named under A's own workspace
        _same_404(c.get(f"{own_ws}/{project_slug(b)}"), c.get(f"{own_ws}/p-missing"))
        _same_404(
            c.patch(f"{own_ws}/{project_slug(b)}", json=patch, headers=headers),
            c.patch(f"{own_ws}/p-missing", json=patch, headers=headers),
        )
        for base, gone_ws in targets:
            missing_list = c.get(gone_ws)
            missing_get = c.get(f"{gone_ws}/{project_slug(b)}")
            missing_patch = c.patch(f"{gone_ws}/{project_slug(b)}", json=patch, headers=headers)
            missing_post = c.post(gone_ws, json=new, headers=headers)
            _same_404(c.get(base), missing_list)
            _same_404(c.get(f"{base}/{project_slug(b)}"), missing_get)
            _same_404(
                c.patch(f"{base}/{project_slug(b)}", json=patch, headers=headers), missing_patch
            )
            _same_404(c.post(base, json=new, headers=headers), missing_post)
    rows = owner_session.execute(
        text("SELECT slug, name FROM shifde.projects WHERE tenant_id = :t"), {"t": b.tenant}
    ).all()
    assert [(r.slug, r.name) for r in rows] == [(project_slug(b), project_slug(b))]


def test_the_dashboard_counts_only_its_own_client(
    settings: Settings, world: World, people: People
) -> None:
    a, b = world.a, world.b
    with tenancy_client(settings) as c:
        login(c, people.owner_a)
        own = c.get(f"/api/v1/orgs/{a.slug}/dashboard")
        assert own.status_code == 200
        assert own.json()["counts"]["workspaces"] == 2 and own.json()["counts"]["projects"] == 1
        assert str(b.tenant) not in own.text and b.slug not in own.text
        _same_404(
            c.get(f"/api/v1/orgs/{b.slug}/dashboard"),
            c.get("/api/v1/orgs/no-such-client/dashboard"),
        )
        _same_404(
            c.get(f"/api/v1/orgs/{a.slug}/dashboard?workspace={ws_slug(b)}"),
            c.get(f"/api/v1/orgs/{a.slug}/dashboard?workspace=w-missing"),
        )


# ------------------------------------------------------------------ sessions and keys
def _mint(app_db: Database, settings: Settings, tenant: UUID, sa: UUID) -> tuple[UUID, str]:
    with app_session(app_db, tenant) as s:
        row, full = apikeys.mint(
            s,
            settings,
            tenant_id=tenant,
            name="probe",
            principal_type="service_account",
            principal_id=sa,
            scopes=["workspace:read"],
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=None,
        )
        key_id = row.id
        s.commit()
    return key_id, full


def test_a_session_selects_only_a_client_it_may_see_and_a_pin_holds(
    settings: Settings, world: World, people: People, app_db: Database, owner_session: Session
) -> None:
    """Identity review 3 items 2-3, review 4 item 3: a session selects a client only through
    `resolve_org`. An unpinned session of a member of A and B sees A's key under A and B's under
    B; a session pinned to B gets the 404 of a missing client for A; `/api/auth/session` lists
    only the pinned client (`_session_out`); a member of A without a binding cannot select A."""
    a, b = world.a, world.b
    key_a, _ = _mint(app_db, settings, a.tenant, a.service_account)
    key_b, _ = _mint(app_db, settings, b.tenant, b.service_account)
    probe = "/api/v1/probe-scoped/orgs"
    free, _ = pinned_session(owner_session, people.both_id, None)
    with tenancy_client(settings, cookies={COOKIE: free}) as c:
        orgs = {m["org"]["id"] for m in c.get("/api/auth/session").json()["memberships"]}
        assert orgs == {str(a.tenant), str(b.tenant)}
        listed_a = c.get(f"{probe}/{a.slug}/api-keys")
        assert listed_a.status_code == 200
        assert str(key_a) in listed_a.json() and str(key_b) not in listed_a.json()
        listed_b = c.get(f"{probe}/{b.slug}/api-keys").json()
        assert str(key_b) in listed_b and str(key_a) not in listed_b
    pin_b, _ = pinned_session(owner_session, people.both_id, b.tenant)
    with tenancy_client(settings, cookies={COOKIE: pin_b}) as c:
        orgs = {m["org"]["id"] for m in c.get("/api/auth/session").json()["memberships"]}
        assert orgs == {str(b.tenant)}
        assert str(key_b) in c.get(f"{probe}/{b.slug}/api-keys").json()
        _same_404(c.get(f"{probe}/{a.slug}/api-keys"), c.get(f"{probe}/no-such-client/api-keys"))
        _same_404(c.get(f"/api/v1/orgs/{a.slug}"), c.get("/api/v1/orgs/no-such-client"))
    unbound, _ = pinned_session(owner_session, world.a.member, None)
    with tenancy_client(settings, cookies={COOKIE: unbound}) as c:
        _same_404(c.get(f"{probe}/{a.slug}/api-keys"), c.get(f"{probe}/no-such-client/api-keys"))


def test_an_api_key_addresses_only_its_own_client_and_needs_a_grant(
    settings: Settings, world: World, app_db: Database, owner_session: Session
) -> None:
    a, b = world.a, world.b
    _, key = _mint(app_db, settings, a.tenant, a.service_account)
    headers = {"Authorization": f"Bearer {key}"}
    with tenancy_client(settings) as c:
        missing = c.get("/api/v1/orgs/no-such-client", headers=headers)
        # no binding: the key resolves its own client, the PDP grants nothing, so it is hidden
        _same_404(c.get(f"/api/v1/orgs/{a.slug}", headers=headers), missing)
        owner_session.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) SELECT :i, :t, 'service_account', :s, r.id, "
                "'tenant', :t FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = 'viewer'"
            ),
            {"i": uuid7(), "t": a.tenant, "s": a.service_account},
        )
        owner_session.commit()
        assert c.get(f"/api/v1/orgs/{a.slug}", headers=headers).status_code == 200
        _same_404(c.get(f"/api/v1/orgs/{b.slug}", headers=headers), missing)
        _same_404(c.get(f"/api/v1/orgs/{b.slug}/workspaces", headers=headers), missing)
        assert c.get("/api/v1/orgs", headers=headers).json()["items"] == []


def test_tenant_idp_signs_in_existing_members_only_and_pins_the_session(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    """SETH 3ba0e06 `test_p2_oidc.py` (moved, tenancy part 2b): a client's IdP signs in only its
    existing members and pins the session to that client; tenancy routes then answer for that
    client only, and an unknown subject is never provisioned."""
    a, b = world.a, world.b
    issuer = oidc_flow.sso("a")
    idp = oidc_flow.owner_oidc_idp(owner_session, a.tenant, issuer)
    oidc_flow.owner_identity(owner_session, people.both_id, idp, issuer, "sub-123")
    owner_session.commit()
    fake = oidc_flow.provider()
    s = settings.model_copy(update={"oidc_jwks_min_refresh_seconds": 0})
    with tenancy_client(s, http_transport=fake.transport(), resolver=fake_oidc_resolver) as c:
        r = oidc_flow.sign_in(c, fake, idp, issuer, sub="sub-123")
        assert r.status_code == 302 and r.headers["location"] == "/", r.headers
        sess = c.get("/api/auth/session").json()
        assert [m["org"]["slug"] for m in sess["memberships"]] == [a.slug]  # pinned to A
        assert c.get(f"/api/v1/orgs/{a.slug}/workspaces").status_code == 200
        assert c.get(f"/api/v1/orgs/{b.slug}/workspaces").status_code == 404  # no hopping
        assert _slugs(c.get("/api/v1/orgs")) == {a.slug}
        c.cookies.clear()
        r = oidc_flow.sign_in(c, fake, idp, issuer, code="c2", sub="stranger")
        assert r.headers["location"] == oidc_flow.FAIL
    pinned = owner_session.execute(
        text("SELECT active_tenant_id FROM shifde.sessions WHERE user_id = :u"),
        {"u": people.both_id},
    ).scalar_one()
    assert pinned == a.tenant
    assert oidc_flow.failures(owner_session)[-1][0] == "unknown_identity_jit_off"


def test_platform_status_answers_the_platform_administrator_and_no_client(
    settings: Settings, world: World, people: People, owner_session: Session
) -> None:
    """SEC Minor-2 (APP-2, B-2401): installation status is the platform administrator's. A's
    owner, A's viewer, B's owner, a person in both clients and that person's session pinned to B
    are refused alike (403, no status in the body); the platform administrator gets it."""
    url = "/api/v1/platform/status"
    for email in (people.owner_a, people.viewer_a, people.owner_b, people.both):
        with tenancy_client(settings) as c:
            login(c, email)
            r = c.get(url)
            assert r.status_code == 403, (email, r.text)
            assert "worker" not in r.json() and "database" not in r.json()
    token, _ = pinned_session(owner_session, people.both_id, world.b.tenant)
    with tenancy_client(settings, cookies={COOKIE: token}) as c:
        assert c.get(url).status_code == 403
    with tenancy_client(settings) as c:
        login(c, people.platform)
        r = c.get(url)
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"version", "environment", "database", "auth", "worker"}


def test_platform_status_refuses_the_platform_administrators_pinned_session_and_api_key(
    settings: Settings, world: World, people: People, app_db: Database, owner_session: Session
) -> None:
    """Review 1 note (B-2442): the platform administrator, made an administrator of A so the pin
    and the key are valid, is refused (403, no status in the body) on a session pinned to A and
    with a user API key of A; the same person's unpinned session gets the status (not vacuous)."""
    url = "/api/v1/platform/status"
    a = world.a
    admin_id = owner_session.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": people.platform}
    ).scalar_one()
    owner_member(owner_session, a.tenant, admin_id)
    bind_user(owner_session, a.tenant, admin_id, "tenant_administrator")
    owner_session.commit()
    with app_session(app_db, a.tenant) as s:
        _, key = apikeys.mint(
            s,
            settings,
            tenant_id=a.tenant,
            name="platform-admin-key",
            principal_type="user",
            principal_id=admin_id,
            scopes=["workspace:read"],
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=admin_id,
        )
        s.commit()
    pin_a, _ = pinned_session(owner_session, admin_id, a.tenant)
    with tenancy_client(settings, cookies={COOKIE: pin_a}) as c:
        assert c.get(f"/api/v1/orgs/{a.slug}").status_code == 200  # the pin is a valid session
        r = c.get(url)
        assert r.status_code == 403, r.text
        assert "worker" not in r.json() and "database" not in r.json()
    with tenancy_client(settings) as c:
        headers = {"Authorization": f"Bearer {key}"}
        assert c.get(f"/api/v1/orgs/{a.slug}", headers=headers).status_code == 200  # valid key
        r = c.get(url, headers=headers)
        assert r.status_code == 403, r.text
        assert "worker" not in r.json() and "database" not in r.json()
    free, _ = pinned_session(owner_session, admin_id, None)
    with tenancy_client(settings, cookies={COOKIE: free}) as c:
        assert c.get(url).status_code == 200
