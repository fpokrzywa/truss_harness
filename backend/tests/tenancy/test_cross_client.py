"""Cross-client isolation of tenancy's data layer, as `shifde_app` (B-116 item 12).

Part 1 adds no route; part 2 adds the route cases. Client A cannot read, list or change client
B's tenant row, workspaces or projects through the repository or raw SQL, nothing is visible
without a client context, and the platform-administrator flag reaches the list of clients only,
never a client's workspaces or projects (B-116 item 10).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.core.db import Database, platform_admin_context, set_tenant
from shifde.core.ids import uuid7
from shifde.tenancy import repository
from tests.tenancy.conftest import World, app_session

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
