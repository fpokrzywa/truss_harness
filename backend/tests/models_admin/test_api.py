"""models_admin part 2a routes beyond isolation: the vendor gate on new providers, model lifecycle,
the SHI provider's configuration and its audited per-client switch (DECISIONS B-1711..B-1719)."""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database, set_tenant
from shifde.models_admin import repository
from tests.connections.conftest import owner_vendor
from tests.connections.routes import activate, as_user, connection, org, post
from tests.models_admin.routes import PLATFORM, fake_model_stack, shared_model, switch
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def test_a_new_provider_naming_a_vendor_that_is_not_active_is_refused(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """carry_forward (vendors), provider half of SETH `test_p3_connections.py::
    test_new_connections_and_providers_naming_a_non_active_vendor_are_refused`: the vendor
    status gates new use; a provider that already names the vendor keeps working."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    with owner_db.session() as o:
        vendor = str(owner_vendor(o, world.a.tenant))
        o.commit()
    ids = fake_model_stack(client, h, A, vendor_id=vendor)
    with owner_db.session() as o:
        set_tenant(o, world.a.tenant)
        o.execute(
            text("UPDATE shifde.vendors SET status = 'disabled' WHERE id = :i"), {"i": vendor}
        )
        o.commit()
    body = {
        "name": "prov-new",
        "adapter_key": "fake",
        "connection_id": ids["connection"],
        "vendor_id": vendor,
    }
    refused = post(client, f"{A}/model-providers", h, body, 422)
    assert "disabled" in str(refused)
    # no vendor named: not gated
    post(client, f"{A}/model-providers", h, {**body, "name": "prov-plain", "vendor_id": None})
    got = client.get(f"{A}/model-providers/{ids['provider']}", headers=h)
    assert got.status_code == 200 and got.json()["vendor_id"] == vendor
    kept = client.patch(f"{A}/model-providers/{ids['provider']}", headers=h, json={"name": "kept"})
    assert kept.status_code == 200 and kept.json()["vendor_id"] == vendor, kept.text


def test_model_lifecycle_transitions(client: TestClient, world: World, people: People) -> None:
    """SETH `test_model_lifecycle_transitions_b7` without its invoke and effective-model checks
    (part 2b): the transition table, the terminal retired state and the duplicate key."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A, lifecycle="preview")
    url = f"{A}/models/{ids['model']}"

    def move(to: str) -> int:
        row = client.get(url, headers=h).json()
        r = client.patch(url, json={"lifecycle": to, "row_version": row["row_version"]}, headers=h)
        return r.status_code

    assert move("active") == 200
    assert move("preview") == 409  # active -> preview is not allowed
    assert move("deprecated") == 200
    assert move("active") == 200  # deprecated -> active
    assert move("retired") == 200
    assert move("active") == 409  # retired is terminal
    row = client.get(url, headers=h).json()
    renamed = client.patch(
        url, json={"display_name": "x", "row_version": row["row_version"]}, headers=h
    )
    assert renamed.status_code == 409
    dup = {
        "provider_id": ids["provider"],
        "model_key": "fake-chat",
        "display_name": "d",
        "context_window": 1,
        "max_output_tokens": 1,
    }
    assert client.post(f"{A}/models", headers=h, json=dup).status_code == 409
    hv = as_user(client, people.viewer_a)
    assert client.get(url, headers=hv).status_code == 200  # model:read
    assert client.patch(url, headers=hv, json={"display_name": "v"}).status_code == 403


def test_the_platform_administrator_configures_the_shi_provider(
    client: TestClient, owner_db: Database, people: People
) -> None:
    hp = as_user(client, people.platform)
    assert client.get(f"{PLATFORM}/shi-provider", headers=hp).status_code == 404
    bad = client.put(f"{PLATFORM}/shi-provider", headers=hp, json={"adapter_key": "rest.http"})
    assert bad.status_code == 422, bad.text  # not a model provider adapter
    made = client.put(f"{PLATFORM}/shi-provider", headers=hp, json={"adapter_key": "fake"})
    assert made.status_code == 200, made.text
    out = made.json()
    assert (out["id"], out["status"], out["connection_id"], out["settings"]) == (
        str(repository.SHI_PROVIDER_ID),
        "disabled",
        None,
        {},
    )
    on = client.put(
        f"{PLATFORM}/shi-provider",
        headers=hp,
        json={"adapter_key": "fake", "status": "active", "row_version": out["row_version"]},
    )
    assert on.status_code == 200 and on.json()["status"] == "active", on.text
    stale = client.put(
        f"{PLATFORM}/shi-provider",
        headers=hp,
        json={"adapter_key": "fake", "name": "x", "row_version": out["row_version"]},
    )
    assert stale.status_code == 409
    moved = client.put(f"{PLATFORM}/shi-provider", headers=hp, json={"adapter_key": "openai"})
    assert moved.status_code == 422, moved.text  # the adapter is fixed once created
    with owner_db.session() as o:
        events = o.execute(
            text(
                "SELECT tenant_id, details FROM shifde.audit_events "
                "WHERE action = 'model.shi_provider.configured' ORDER BY occurred_at"
            )
        ).all()
    assert [e.tenant_id for e in events] == [None, None]  # installation level only
    assert [e.details["created"] for e in events] == [True, False]


def _switch_events(owner_db: Database) -> list[Any]:  # Any: rows
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT tenant_id, target_id, details FROM shifde.audit_events "
                    "WHERE action = 'model.shi_provider.access_changed' ORDER BY occurred_at, id"
                )
            ).all()
        )


def test_the_switch_is_set_by_the_platform_administrator_and_shown_to_the_owner(
    client: TestClient, owner_db: Database, world: World, people: People
) -> None:
    A, B = org(world.a.slug), org(world.b.slug)
    hp = as_user(client, people.platform)
    url = f"{PLATFORM}/orgs/{world.a.slug}/shi-provider-access"
    assert client.get(url, headers=hp).json()["enabled"] is False  # off by default
    unknown = client.put(
        f"{PLATFORM}/orgs/no-such-org-x/shi-provider-access", headers=hp, json={"enabled": True}
    )
    assert unknown.status_code == 404 and unknown.json()["detail"] == "organization not found"
    on = client.put(url, headers=hp, json={"enabled": True})
    assert on.status_code == 200, on.text
    assert (on.json()["enabled"], on.json()["row_version"]) == (True, 1)
    stale = client.put(url, headers=hp, json={"enabled": False, "row_version": 7})
    assert stale.status_code == 409
    platform_id = UUID(on.json()["changed_by"])
    # the owner sees the switch; B's stays off
    h = as_user(client, people.owner_a)
    shown = client.get(f"{A}/shi-provider-access", headers=h).json()
    assert (shown["enabled"], shown["row_version"]) == (True, 1)
    hb = as_user(client, people.owner_b)
    assert client.get(f"{B}/shi-provider-access", headers=hb).json()["enabled"] is False
    hp = as_user(client, people.platform)
    off = client.put(url, headers=hp, json={"enabled": False, "row_version": 1})
    assert off.status_code == 200 and off.json()["enabled"] is False, off.text
    events = _switch_events(owner_db)
    # each change: once at installation level, once in A's own trail; nothing in B's
    assert sorted(str(e.tenant_id) for e in events) == sorted(
        [str(world.a.tenant)] * 2 + ["None"] * 2
    )
    in_a = [e for e in events if e.tenant_id == world.a.tenant]
    assert [e.details["enabled"] for e in in_a] == [True, False]
    assert [e.details["previously_enabled"] for e in in_a] == [False, True]
    with owner_db.session() as o:
        actors = o.execute(
            text(
                "SELECT DISTINCT actor_id FROM shifde.audit_events "
                "WHERE action = 'model.shi_provider.access_changed'"
            )
        ).scalars()
        assert set(actors) == {platform_id}
        row = o.execute(
            text("SELECT changed_by FROM shifde.shi_provider_access WHERE tenant_id = :a"),
            {"a": world.a.tenant},
        ).scalar_one()
        assert row == platform_id


def test_the_shared_models_are_visible_only_while_the_switch_is_on(
    client: TestClient, app_db: Database, world: World, people: People
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    hp = as_user(client, people.platform)
    client.put(f"{PLATFORM}/shi-provider", headers=hp, json={"adapter_key": "fake"})
    model = shared_model(app_db)
    h = as_user(client, people.owner_a)

    def visible() -> tuple[bool, bool, int]:
        listed = {m["id"] for m in client.get(f"{A}/models", headers=h).json()["items"]}
        got = client.get(f"{A}/models/{model}", headers=h).status_code
        providers = client.get(f"{A}/model-providers", headers=h).json()["items"]
        return (
            model in listed,
            any(p["id"] == str(repository.SHI_PROVIDER_ID) for p in providers),
            got,
        )

    assert visible() == (False, False, 404)
    switch(client, people.platform, world.a.slug, True)
    h = as_user(client, people.owner_a)
    assert visible() == (True, True, 200)
    switch(client, people.platform, world.a.slug, False)
    h = as_user(client, people.owner_a)
    assert visible() == (False, False, 404)


def test_a_model_connection_from_another_adapter_is_refused(
    client: TestClient, world: World, people: People
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    conn = connection(
        client,
        h,
        {"name": "fake-gw", "connection_type": "model_api", "adapter_key": "fake", "config": {}},
        A,
    )
    activate(client, h, conn["id"], A)
    body = {"name": "p", "adapter_key": "rest.http", "connection_id": conn["id"]}
    assert client.post(f"{A}/model-providers", headers=h, json=body).status_code == 422


def test_the_guard_functions_pin_their_search_path(owner_db: Database) -> None:
    """models_admin review 1 item 8 (models_admin_0002)."""
    with owner_db.session() as o:
        rows = dict(
            o.execute(
                text(
                    "SELECT p.proname, p.proconfig FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'shifde' AND p.proname IN "
                    "('model_providers_guard', 'models_guard', 'connections_guard')"
                )
            ).all()
        )
    assert rows == {
        name: ["search_path=pg_catalog, shifde, pg_temp"]
        for name in ("model_providers_guard", "models_guard", "connections_guard")
    }
