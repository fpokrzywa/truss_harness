"""Fabric targets (SP8-5): the Entra tenant binding, `binding_in_use` on one workspace, and no way to
change a registered target's address, bindings or credential reference (precondition C)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any  # Any: JSON bodies
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.core.db import Database
from shifde.delivery import api as delivery_api
from tests.connections.routes import as_user
from tests.delivery.routes import (
    FABRIC_BINDINGS,
    counts,
    register,
    sp8_address,
    sp8_body,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws2_slug, ws_slug

ENTRA = "0A1B2C3D-0000-4000-8000-00000000ABCD"
IN_USE = "Another active target on this workspace already binds this lakehouse or notebook."


# ------------------------------------------------------------------ the Entra tenant binding
def test_an_uppercase_entra_tenant_is_stored_and_returned_lowercase(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    bindings = {**FABRIC_BINDINGS, "entra_tenant_id": ENTRA}
    body = sp8_body("fabric", ws_slug(world.a), sp8_address("fabric"), bindings=bindings)
    made = register(client, h, world.a.slug, body)
    assert made["bindings"] == {**bindings, "entra_tenant_id": ENTRA.lower()}
    got = client.get(f"{targets(world.a.slug)}/{made['id']}", headers=h).json()
    assert got["bindings"] == made["bindings"]
    with owner_db.session() as o:
        stored = o.execute(
            text("SELECT bindings FROM shifde.delivery_targets WHERE id = :i"), {"i": made["id"]}
        ).scalar_one()
    assert stored == made["bindings"]


@pytest.mark.parametrize(
    "entra",
    [
        None,
        "",
        "contoso.onmicrosoft.com",
        ENTRA.replace("-", ""),
        "{" + ENTRA + "}",
        f"urn:uuid:{ENTRA}",
        ENTRA + "\n",
        ENTRA[:-1] + "g",
        ENTRA[:-1],
    ],
)
def test_a_missing_or_malformed_entra_tenant_is_422_and_not_echoed(
    client: TestClient, world: World, people: People, owner_db: Database, entra: str | None
) -> None:
    h = as_user(client, people.owner_a)
    bindings: dict[str, Any] = {**FABRIC_BINDINGS, "entra_tenant_id": entra}
    if entra is None:
        del bindings["entra_tenant_id"]
    body = sp8_body("fabric", ws_slug(world.a), sp8_address("fabric"), bindings=bindings)
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert all(e["loc"][:2] == ["body", "bindings"] for e in r.json()["errors"]), r.text
    if entra:
        assert entra.strip() not in r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "x"},
        {"lakehouse_name": "1Lake"},
        {"notebook_name": FABRIC_BINDINGS["lakehouse_name"]},
        {"workspace": str(uuid4())},
    ],
)
def test_a_refused_binding_never_echoes_the_entra_tenant(
    client: TestClient, world: World, people: People, owner_db: Database, change: dict[str, str]
) -> None:
    h = as_user(client, people.owner_a)
    bindings = {**FABRIC_BINDINGS, "entra_tenant_id": ENTRA, **change}
    body = sp8_body("fabric", ws_slug(world.a), sp8_address("fabric"), bindings=bindings)
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert ENTRA not in r.text and ENTRA.lower() not in r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


# ------------------------------------------------------------------ binding_in_use
LAKE, NOTEBOOK = FABRIC_BINDINGS["lakehouse_name"], FABRIC_BINDINGS["notebook_name"]


@pytest.mark.parametrize(
    ("lakehouse", "notebook"),
    [
        (LAKE, "OtherNotebook"),
        ("OtherLake", NOTEBOOK),
        (NOTEBOOK, "OtherNotebook"),
        ("OtherLake", LAKE),
        (LAKE.upper(), "OtherNotebook"),
        ("OtherLake", NOTEBOOK.lower()),
        (NOTEBOOK.swapcase(), "OtherNotebook"),
    ],
)
def test_a_reused_item_name_on_one_workspace_is_binding_in_use(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    lakehouse: str,
    notebook: str,
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    url = sp8_address("fabric")
    first = register(client, h, a.slug, sp8_body("fabric", ws_slug(a), url))
    bindings = {**FABRIC_BINDINGS, "lakehouse_name": lakehouse, "notebook_name": notebook}
    body = sp8_body("fabric", ws2_slug(a), url, bindings=bindings)
    r = client.post(targets(a.slug), headers=h, json=body)
    assert r.status_code == 409, r.text
    assert (r.json()["code"], r.json()["detail"]) == ("binding_in_use", IN_USE)
    assert first["id"] not in r.text and first["name"] not in r.text
    assert counts(owner_db, a.tenant) == (1, 1)
    # another workspace may bind the same names
    elsewhere = {**body, "address": sp8_address("fabric"), "name": "elsewhere"}
    register(client, h, a.slug, elsewhere)
    assert counts(owner_db, a.tenant) == (2, 2)
    # archiving frees the names on the claim
    assert client.post(f"{targets(a.slug)}/{first['id']}/archive", headers=h).is_success
    assert register(client, h, a.slug, body)["bindings"] == bindings


def test_distinct_item_names_on_one_workspace_are_accepted(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    url = sp8_address("fabric")
    register(client, h, world.a.slug, sp8_body("fabric", ws_slug(world.a), url))
    other = {**FABRIC_BINDINGS, "lakehouse_name": "Lake_2", "notebook_name": "Notebook_2"}
    register(client, h, world.a.slug, sp8_body("fabric", ws_slug(world.a), url, bindings=other))
    assert counts(owner_db, world.a.tenant) == (2, 1)


def test_another_organizations_names_are_no_conflict_and_not_revealed(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """B binding A's names on its own workspace is accepted; on A's workspace B gets the fixed
    claim refusal, never `binding_in_use`."""
    url = sp8_address("fabric")
    h = as_user(client, people.owner_a)
    register(client, h, world.a.slug, sp8_body("fabric", ws_slug(world.a), url))
    h = as_user(client, people.owner_b)
    r = client.post(
        targets(world.b.slug), headers=h, json=sp8_body("fabric", ws_slug(world.b), url)
    )
    assert (r.status_code, r.json()["code"]) == (409, "platform_already_registered"), r.text
    register(client, h, world.b.slug, sp8_body("fabric", ws_slug(world.b), sp8_address("fabric")))
    assert counts(owner_db, world.b.tenant) == (1, 1)


# ------------------------------------------------------------------ immutable after registration
@pytest.mark.parametrize(
    "assignment",
    [
        "address = 'https://api.fabric.microsoft.com/v1/workspaces/' || gen_random_uuid()",
        'bindings = bindings || \'{"entra_tenant_id": "x"}\'::jsonb',
        "credential_ref = 'another-ref'",
    ],
)
def test_shifde_app_cannot_update_a_targets_address_bindings_or_credential(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    assignment: str,
) -> None:
    h = as_user(client, people.owner_a)
    body = sp8_body("fabric", ws_slug(world.a), sp8_address("fabric"))
    made = register(client, h, world.a.slug, body)

    def row() -> tuple[Any, ...]:
        with owner_db.session() as o:
            found = o.execute(
                text(
                    "SELECT address, bindings::text, credential_ref, revision "
                    "FROM shifde.delivery_targets WHERE id = :i"
                ),
                {"i": made["id"]},
            ).one()
        return tuple(found)

    before = row()
    with app_session(app_db, world.a.tenant, world.a.member) as s:
        with pytest.raises(DBAPIError) as raised:
            # the assignment is one of the literals above
            s.execute(
                text(f"UPDATE shifde.delivery_targets SET {assignment} WHERE id = :i"),  # nosec B608
                {"i": made["id"]},
            )
        assert getattr(raised.value.orig, "sqlstate", None) == "42501"
        s.rollback()
    assert row() == before


def test_no_route_changes_a_delivery_target() -> None:
    methods = {
        (method, route.path)
        for route in delivery_api.router.routes
        for method in getattr(route, "methods", set())
        if "/delivery/targets" in getattr(route, "path", "")
    }
    base = "/api/v1/orgs/{org}/delivery/targets"
    assert methods == {
        ("POST", base),
        ("GET", base),
        ("GET", base + "/{target_id}"),
        ("POST", base + "/{target_id}/archive"),
    }


def test_the_openapi_document_has_no_put_or_patch_on_a_target() -> None:
    document = json.loads(
        (Path(__file__).resolve().parents[2] / "openapi.json").read_text(encoding="utf-8")
    )
    found = {p: set(ops) for p, ops in document["paths"].items() if "/delivery/targets" in p}
    assert found and all(not ops & {"put", "patch", "delete"} for ops in found.values())
