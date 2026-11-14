"""`tenancy.repository` against the real tables, as `shifde_app` in the client's own context, and
the secrets scope check that needs workspaces and projects (carry_forward secrets, B-802)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.core.crypto import KeyRing
from shifde.core.db import Database, platform_admin_context
from shifde.core.errors import Conflict
from shifde.core.ids import uuid7
from shifde.secrets import repository as secrets_repository
from shifde.secrets import service as secrets_service
from shifde.tenancy import repository
from tests.tenancy.conftest import World, app_session

LIMIT = 100


def test_workspace_lifecycle_in_the_own_client(world: World, app_db: Database) -> None:
    a = world.a
    with app_session(app_db, a.tenant) as s:
        new = uuid7()
        repository.insert_workspace(
            s, {"id": new, "tenant_id": a.tenant, "slug": "ops", "name": "Ops"}
        )
        found = repository.get_workspace_by_slug(s, a.tenant, "ops")
        assert found is not None and found.id == new and found.row_version == 1
        assert repository.update_workspace(s, a.tenant, new, 2, {"name": "x"}) is None  # stale
        updated = repository.update_workspace(s, a.tenant, new, 1, {"name": "Ops 2"})
        assert updated is not None and updated.name == "Ops 2" and updated.row_version == 2
        assert updated.updated_at is not None  # trg_workspaces_updated_at
        archived = repository.update_workspace(s, a.tenant, new, 2, {"status": "archived"})
        assert archived is not None
        active = repository.list_workspaces(
            s, tenant_id=a.tenant, only_ids=None, include_archived=False, after=None, limit=LIMIT
        )
        assert {w.id for w in active} == {a.ws, a.ws2}
        every = repository.list_workspaces(
            s, tenant_id=a.tenant, only_ids=None, include_archived=True, after=None, limit=LIMIT
        )
        assert [w.id for w in every] == sorted([a.ws, a.ws2, new])
        assert repository.count_workspaces(s, a.tenant, [a.ws]) == 1
        assert repository.workspace_ids(s, a.tenant, [a.ws2, new]) == [a.ws2]
        page = repository.list_workspaces(
            s, tenant_id=a.tenant, only_ids=None, include_archived=True, after=every[0].id, limit=1
        )
        assert [w.id for w in page] == [every[1].id]


def test_projects_follow_the_visibility_sets(world: World, app_db: Database) -> None:
    a = world.a
    with app_session(app_db, a.tenant) as s:
        other = uuid7()
        repository.insert_project(
            s,
            {"id": other, "tenant_id": a.tenant, "workspace_id": a.ws, "slug": "p2", "name": "P2"},
        )
        found = repository.get_project_by_slug(s, a.tenant, a.ws, "p2")
        assert found is not None and found.id == other
        assert repository.get_project_by_id(s, a.tenant, a.project) is not None

        def listed(visible_ws: list[UUID] | None, visible_projects: list[UUID]) -> set[UUID]:
            rows = repository.list_projects(
                s,
                tenant_id=a.tenant,
                workspace_id=a.ws,
                visible_workspace_ids=visible_ws,
                visible_project_ids=visible_projects,
                include_archived=False,
                after=None,
                limit=LIMIT,
            )
            return {p.id for p in rows}

        assert listed(None, []) == {a.project, other}
        assert listed([a.ws], []) == {a.project, other}
        assert listed([], [other]) == {other}
        assert listed([a.ws2], []) == set()
        assert (
            repository.count_projects(
                s,
                tenant_id=a.tenant,
                workspace_id=None,
                visible_workspace_ids=[],
                visible_project_ids=[other],
            )
            == 1
        )
        updated = repository.update_project(s, a.tenant, other, 1, {"status": "archived"})
        assert updated is not None and updated.row_version == 2
        assert listed(None, []) == {a.project}


def test_tenants_are_created_in_the_platform_administrator_context_only(
    world: World, app_db: Database
) -> None:
    slug = f"new-{uuid7().hex[-12:]}"
    with app_session(app_db, None) as s:
        with platform_admin_context(s):
            new = uuid7()
            repository.insert_tenant(s, {"id": new, "slug": slug, "name": "New client"})
            assert repository.tenant_slug_exists(s, slug)
            updated = repository.update_tenant(s, new, 1, {"description": "d"})
            assert updated is not None and updated.row_version == 2
        s.commit()
    with app_session(app_db, world.a.tenant) as s:
        assert repository.get_tenant_by_slug(s, slug) is None
        own = repository.update_tenant(s, world.a.tenant, 1, {"name": "A renamed"})
        assert own is not None and own.name == "A renamed"
        with pytest.raises(DBAPIError, match="permission denied"):
            s.execute(
                text("UPDATE shifde.tenants SET slug = 'x' WHERE id = :i"), {"i": world.a.tenant}
            )


def test_a_secret_scope_must_be_a_workspace_or_project_of_its_own_client(
    world: World, app_db: Database, owner_db: Database
) -> None:
    a, b = world.a, world.b
    keyring = KeyRing.parse(KeyRing.generate_entry("k1"))
    for scope_type, own, foreign in (("workspace", a.ws, b.ws), ("project", a.project, b.project)):
        with app_session(app_db, a.tenant) as s:
            meta = secrets_service.create_secret(
                s,
                keyring,
                tenant_id=a.tenant,
                name=f"{scope_type}-own",
                value="scoped-secret-value-0001",
                scope_type=scope_type,
                scope_id=own,
                created_by=None,
            )
            assert meta.id is not None
            s.commit()
        for scope_id in (foreign, uuid7()):
            # the service maps every integrity error to its name conflict; the trigger message is
            # asserted on the owner's raw statement below (shifde_app cannot update the scope)
            with app_session(app_db, a.tenant) as s, pytest.raises(Conflict):
                secrets_service.create_secret(
                    s,
                    keyring,
                    tenant_id=a.tenant,
                    name=f"{scope_type}-foreign",
                    value="scoped-secret-value-0002",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    created_by=None,
                )
            with (
                owner_db.session() as s,
                pytest.raises(DBAPIError, match="secret scope is not in this tenant"),
            ):
                s.execute(
                    text(
                        "UPDATE shifde.secrets SET scope_type = :st, scope_id = :sc WHERE id = :i"
                    ),
                    {"st": scope_type, "sc": scope_id, "i": meta.id},
                )
    with app_session(app_db, a.tenant) as s:
        names = s.execute(text("SELECT name FROM shifde.secrets")).scalars().all()
        assert sorted(names) == ["project-own", "workspace-own"]


def test_a_project_resolves_to_its_workspace_only_within_its_client(
    world: World, app_db: Database
) -> None:
    a, b = world.a, world.b
    with app_session(app_db, a.tenant) as s:
        assert secrets_repository.project_workspace_id(s, a.project, tenant_id=a.tenant) == a.ws
        assert secrets_repository.project_workspace_id(s, b.project, tenant_id=a.tenant) is None
        assert secrets_repository.project_workspace_id(s, b.project, tenant_id=b.tenant) is None
