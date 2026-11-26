"""`delivery_0010` in the database (issue #397, B-4192): `shifde_app` may UPDATE
`delivery_targets.bindings`, and `trg_delivery_targets_bindings_guard` admits only a change of the
`version_id` key, to a canonical UUID, of a Palantir target; RLS keeps another organization's rows
out of reach. Every write here is a direct statement as `shifde_app` in organization A's context
(no route exists yet); rows are created as the schema owner."""

from __future__ import annotations

import json
import secrets
from typing import Any  # Any: JSON bindings
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config
from tests.delivery.routes import FABRIC_BINDINGS, PALANTIR_BINDINGS
from tests.packaging.test_target_kind_db import _scratch
from tests.tenancy.conftest import World, app_session

BINDINGS: dict[str, dict[str, Any]] = {
    "palantir": PALANTIR_BINDINGS,
    "databricks": {"warehouse_id": "abc123def456", "version_id": str(uuid4())},
    "fabric": {**FABRIC_BINDINGS, "version_id": str(uuid4())},
}
SET_VERSION = "bindings = jsonb_set(bindings, '{version_id}', to_jsonb(CAST(:v AS text)))"


def _target(owner_db: Database, tenant: UUID, ws: UUID, kind: str = "palantir") -> UUID:
    """An active target of `kind` in `tenant`, with its own claim (schema owner)."""
    claim, target = uuid7(), uuid7()
    with owner_db.session() as o:
        o.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        o.execute(
            text(
                "INSERT INTO shifde.delivery_platform_claims (id, tenant_id, platform_kind, "
                "address_key, created_by) VALUES (:c, :t, :k, :a, :c)"
            ),
            {"c": claim, "t": tenant, "k": kind, "a": secrets.token_hex(32)},
        )
        o.execute(
            text(
                "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, name, "
                "platform_kind, address, claim_id, environment, bindings, credential_ref, "
                "created_by) VALUES (:i, :t, :w, 'x', :k, 'https://x.example.com', :c, 'dev', "
                "CAST(:b AS jsonb), 'ref', :i)"
            ),
            {
                "i": target,
                "t": tenant,
                "w": ws,
                "k": kind,
                "c": claim,
                "b": json.dumps(BINDINGS[kind]),
            },
        )
        o.commit()
    return target


def _row(owner_db: Database, target: UUID) -> tuple[Any, ...]:
    with owner_db.session() as o:
        return tuple(
            o.execute(
                text(
                    "SELECT tenant_id, workspace_id, project_id, name, platform_kind, address, "
                    "claim_id, environment, bindings::text, credential_ref, status, revision, "
                    "created_by, created_at, updated_at FROM shifde.delivery_targets "
                    "WHERE id = :i"
                ),
                {"i": target},
            ).one()
        )


def _refused(app_db: Database, world: World, sql: str, **params: Any) -> None:
    """`sql` as `shifde_app` in A's context fails with 42501."""
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        with pytest.raises(DBAPIError) as raised:
            s.execute(text(sql), params)
        assert getattr(raised.value.orig, "sqlstate", None) == "42501", raised.value
        s.rollback()


# ------------------------------------------------------------------ what is admitted
def test_a_version_id_change_on_its_own_palantir_target_succeeds(
    app_db: Database, owner_db: Database, world: World
) -> None:
    target = _target(owner_db, world.a.tenant, world.a.ws)
    before = _row(owner_db, target)
    version = str(uuid4())
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        done = s.execute(
            text(
                f"UPDATE shifde.delivery_targets SET {SET_VERSION}, revision = revision + 1 "
                "WHERE id = :i AND revision = :r"
            ),
            {"i": target, "v": version, "r": before[11]},
        )
        assert done.rowcount == 1
        s.commit()
    after = _row(owner_db, target)
    assert json.loads(after[8]) == {**PALANTIR_BINDINGS, "version_id": version}
    assert after[11] == before[11] + 1
    assert after[14] is not None  # updated_at, set by delivery_0001's trigger, needs no grant
    # nothing else moved
    assert after[:8] + after[9:11] + after[12:14] == before[:8] + before[9:11] + before[12:14]


def test_a_status_claim_and_revision_update_alone_still_succeeds(
    app_db: Database, owner_db: Database, world: World
) -> None:
    """`archive_target`'s statement (the one UPDATE in `src`) and a revision bump."""
    for kind in ("palantir", "databricks"):
        target = _target(owner_db, world.a.tenant, world.a.ws, kind)
        with app_session(app_db, world.a.tenant, world.a.member) as s:
            assert (
                s.execute(
                    text(
                        "UPDATE shifde.delivery_targets SET revision = revision + 1 WHERE id = :i"
                    ),
                    {"i": target},
                ).rowcount
                == 1
            )
            assert (
                s.execute(
                    text(
                        "UPDATE shifde.delivery_targets SET status = 'archived', claim_id = NULL, "
                        "revision = revision + 1 WHERE id = :i AND status = 'active'"
                    ),
                    {"i": target},
                ).rowcount
                == 1
            )
            s.commit()
        row = _row(owner_db, target)
        assert (row[10], row[6], row[11]) == ("archived", None, 3)
        assert json.loads(row[8]) == BINDINGS[kind]


def test_setting_bindings_to_its_current_value_is_not_a_change(
    app_db: Database, owner_db: Database, world: World
) -> None:
    target = _target(owner_db, world.a.tenant, world.a.ws, "databricks")
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        done = s.execute(
            text("UPDATE shifde.delivery_targets SET bindings = bindings WHERE id = :i"),
            {"i": target},
        )
        assert done.rowcount == 1
        s.rollback()


# ------------------------------------------------------------------ what the guard refuses
@pytest.mark.parametrize(
    "assignment",
    [
        # another key changed, added or removed
        "bindings = jsonb_set(bindings, '{frontend_origin}', '\"https://evil.example.com\"')",
        "bindings = jsonb_set(bindings, '{marketplace_rid}', "
        "'\"ri.marketplace.main.local.0a1b2c3d-0000-4000-8000-0000000000ff\"')",
        'bindings = bindings || \'{"host": "https://evil.example.com"}\'::jsonb',
        "bindings = bindings - 'ontology_rid'",
        # version_id together with another key
        "bindings = bindings || jsonb_build_object('version_id', gen_random_uuid()::text, "
        "'allow_preview_apis', true)",
        # version_id removed, or not a canonical UUID string
        "bindings = bindings - 'version_id'",
        "bindings = jsonb_set(bindings, '{version_id}', '\"not-a-uuid\"')",
        "bindings = jsonb_set(bindings, '{version_id}', '\"\"')",
        "bindings = jsonb_set(bindings, '{version_id}', 'null')",
        "bindings = jsonb_set(bindings, '{version_id}', '42')",
        "bindings = jsonb_set(bindings, '{version_id}', to_jsonb(upper(gen_random_uuid()::text)))",
        "bindings = jsonb_set(bindings, '{version_id}', "
        "to_jsonb(replace(gen_random_uuid()::text, '-', '')))",
        "bindings = jsonb_set(bindings, '{version_id}', "
        "to_jsonb('{' || gen_random_uuid()::text || '}'))",
        "bindings = jsonb_set(bindings, '{version_id}', "
        "to_jsonb(gen_random_uuid()::text || E'\\n'))",
        # not an object at all
        "bindings = '[]'::jsonb",
        "bindings = '{}'::jsonb",
    ],
)
def test_a_bindings_change_other_than_the_version_id_is_refused(
    app_db: Database, owner_db: Database, world: World, assignment: str
) -> None:
    target = _target(owner_db, world.a.tenant, world.a.ws)
    before = _row(owner_db, target)
    # the assignment is one of the literals above
    _refused(
        app_db, world, f"UPDATE shifde.delivery_targets SET {assignment} WHERE id = :i", i=target
    )  # nosec B608
    assert _row(owner_db, target) == before


@pytest.mark.parametrize("kind", ["databricks", "fabric"])
def test_a_version_id_change_on_a_non_palantir_target_is_refused(
    app_db: Database, owner_db: Database, world: World, kind: str
) -> None:
    target = _target(owner_db, world.a.tenant, world.a.ws, kind)
    before = _row(owner_db, target)
    _refused(
        app_db,
        world,
        f"UPDATE shifde.delivery_targets SET {SET_VERSION} WHERE id = :i",  # nosec B608
        i=target,
        v=str(uuid4()),
    )
    assert _row(owner_db, target) == before


@pytest.mark.parametrize(
    "assignment",
    [
        "address = 'https://evil.example.com'",
        "credential_ref = 'another-ref'",
        "name = 'renamed'",
        "platform_kind = 'databricks'",
        "environment = 'prod'",
        "workspace_id = gen_random_uuid()",
        "project_id = gen_random_uuid()",
        "created_by = gen_random_uuid()",
        "created_at = now()",
        "updated_at = now()",
        "tenant_id = gen_random_uuid()",
        "id = gen_random_uuid()",
    ],
)
def test_an_update_of_any_other_column_is_refused(
    app_db: Database, owner_db: Database, world: World, assignment: str
) -> None:
    target = _target(owner_db, world.a.tenant, world.a.ws)
    before = _row(owner_db, target)
    # the assignment is one of the literals above
    _refused(
        app_db, world, f"UPDATE shifde.delivery_targets SET {assignment} WHERE id = :i", i=target
    )  # nosec B608
    _refused(
        app_db,
        world,
        f"UPDATE shifde.delivery_targets SET {SET_VERSION}, {assignment} WHERE id = :i",  # nosec B608
        i=target,
        v=str(uuid4()),
    )
    assert _row(owner_db, target) == before


def test_another_organizations_palantir_target_is_out_of_reach(
    app_db: Database, owner_db: Database, world: World
) -> None:
    theirs = _target(owner_db, world.b.tenant, world.b.ws)
    before = _row(owner_db, theirs)
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        for where, params in (
            ("id = :i", {"i": theirs}),
            ("tenant_id = :t", {"t": world.b.tenant}),
            ("true", {}),
        ):
            done = s.execute(
                text(
                    f"UPDATE shifde.delivery_targets SET {SET_VERSION}, "  # nosec B608
                    f"revision = revision + 1 WHERE {where}"
                ),
                {**params, "v": str(uuid4())},
            )
            assert done.rowcount == 0
        s.commit()
    # a session of no organization reaches nothing either
    with app_session(app_db, None) as s:
        done = s.execute(
            text(f"UPDATE shifde.delivery_targets SET {SET_VERSION} WHERE id = :i"),  # nosec B608
            {"i": theirs, "v": str(uuid4())},
        )
        assert done.rowcount == 0
        s.commit()
    assert _row(owner_db, theirs) == before


def test_the_schema_owner_is_not_bound_by_the_guard(owner_db: Database, world: World) -> None:
    """The table's owner can drop the trigger, so the guard does not bind it (its data migrations
    and the test fixtures that set bindings keep working)."""
    target = _target(owner_db, world.a.tenant, world.a.ws, "databricks")
    with owner_db.session() as o:
        assert o.execute(text("SELECT current_user")).scalar_one() == "shifde_owner"
        done = o.execute(
            text("UPDATE shifde.delivery_targets SET bindings = '{}'::jsonb WHERE id = :i"),
            {"i": target},
        )
        assert done.rowcount == 1
        o.rollback()


# ------------------------------------------------------------------ the catalog
_UPDATE_COLUMNS = (
    "SELECT column_name FROM information_schema.column_privileges WHERE grantee = 'shifde_app' "
    "AND table_schema = 'shifde' AND table_name = 'delivery_targets' "
    "AND privilege_type = 'UPDATE' ORDER BY column_name"
)
_TABLE_PRIVILEGES = (
    "SELECT privilege_type FROM information_schema.table_privileges WHERE grantee = 'shifde_app' "
    "AND table_schema = 'shifde' AND table_name = 'delivery_targets' ORDER BY privilege_type"
)
_GUARD = (
    "SELECT p.prosecdef, p.proconfig, pg_catalog.pg_get_userbyid(p.proowner), "
    "pg_catalog.has_function_privilege('public', p.oid, 'EXECUTE'), "
    "pg_catalog.has_function_privilege('shifde_app', p.oid, 'EXECUTE') "
    "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'shifde' AND p.proname = 'delivery_targets_bindings_guard'"
)
_TRIGGER = (
    "SELECT pg_catalog.pg_get_triggerdef(t.oid) FROM pg_catalog.pg_trigger t "
    "WHERE t.tgrelid = 'shifde.delivery_targets'::regclass "
    "AND t.tgname = 'trg_delivery_targets_bindings_guard'"
)
_RLS = (
    "SELECT c.relrowsecurity, c.relforcerowsecurity, "
    "(SELECT array_agg(p.polname ORDER BY p.polname) FROM pg_catalog.pg_policy p "
    "WHERE p.polrelid = c.oid) FROM pg_catalog.pg_class c "
    "WHERE c.oid = 'shifde.delivery_targets'::regclass"
)


def _catalog(c: Connection) -> dict[str, Any]:
    return {
        "update": [r[0] for r in c.execute(text(_UPDATE_COLUMNS))],
        "table": [r[0] for r in c.execute(text(_TABLE_PRIVILEGES))],
        "guard": [tuple(r) for r in c.execute(text(_GUARD))],
        "trigger": [r[0] for r in c.execute(text(_TRIGGER))],
        "rls": tuple(c.execute(text(_RLS)).one()),
    }


UPGRADED: dict[str, Any] = {
    "update": ["bindings", "claim_id", "revision", "status"],
    # column-level grants are listed with the table privileges they are not
    "table": ["INSERT", "SELECT"],
    "guard": [(False, ["search_path=pg_catalog, pg_temp"], "shifde_owner", False, False)],
    "trigger": [
        "CREATE TRIGGER trg_delivery_targets_bindings_guard BEFORE UPDATE OF bindings ON "
        "shifde.delivery_targets FOR EACH ROW EXECUTE FUNCTION "
        "shifde.delivery_targets_bindings_guard()"
    ],
    "rls": (True, True, ["tenant_isolation"]),
}
DOWNGRADED: dict[str, Any] = {
    **UPGRADED,
    "update": ["claim_id", "revision", "status"],
    "guard": [],
    "trigger": [],
}


def test_the_grant_guard_and_policies_in_the_catalog(owner_db: Database) -> None:
    with owner_db.session() as o:
        assert _catalog(o.connection()) == UPGRADED


def test_the_downgrade_drops_only_what_it_added_and_the_upgrade_restores_it(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    with _scratch(run_id, admin_url, owner_url, app_url, "dlv10rt") as url:
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.connect() as c:
                assert _catalog(c) == UPGRADED
            command.downgrade(cfg, "delivery_0009")
            with engine.connect() as c:
                assert _catalog(c) == DOWNGRADED
            command.upgrade(cfg, "heads")
            with engine.connect() as c:
                assert _catalog(c) == UPGRADED
        finally:
            engine.dispose()


def test_no_definer_writes_the_table_and_the_app_role_is_not_its_owner(owner_db: Database) -> None:
    """The guard's owner exemption is safe only while no SECURITY DEFINER function (which runs as
    the owner) mentions `delivery_targets` and `shifde_app` is no member of the table's owner."""
    with owner_db.session() as o:
        definers = o.execute(
            text(
                "SELECT p.proname FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'shifde' AND p.prosecdef "
                "AND p.prosrc ILIKE '%delivery_targets%' ORDER BY p.proname"
            )
        ).all()
        assert definers == []
        member = o.execute(
            text(
                "SELECT pg_catalog.pg_has_role('shifde_app', c.relowner, 'MEMBER'), "
                "pg_catalog.pg_has_role('shifde_app', c.relowner, 'USAGE') "
                "FROM pg_catalog.pg_class c WHERE c.oid = 'shifde.delivery_targets'::regclass"
            )
        ).one()
        assert tuple(member) == (False, False)


def test_the_triggers_on_the_table_are_pinned(owner_db: Database) -> None:
    """A later BEFORE UPDATE trigger that sorts after the guard could rewrite NEW.bindings after it
    has checked them; adding any trigger to the table must revisit the guard."""
    with owner_db.session() as o:
        triggers = o.execute(
            text(
                "SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid) FROM pg_catalog.pg_trigger t "
                "WHERE t.tgrelid = 'shifde.delivery_targets'::regclass AND NOT t.tgisinternal "
                "ORDER BY t.tgname"
            )
        ).all()
    assert [tuple(r) for r in triggers] == [
        ("trg_delivery_targets_bindings_guard", UPGRADED["trigger"][0]),
        (
            "trg_delivery_targets_updated_at",
            "CREATE TRIGGER trg_delivery_targets_updated_at BEFORE UPDATE ON "
            "shifde.delivery_targets FOR EACH ROW EXECUTE FUNCTION shifde.set_updated_at()",
        ),
    ]
