"""One platform per organization (design 3, B-2501; lead ruling B-2523).

Claims are written only by the `shifde.delivery_claim_platform` definer, which reads the client
from the session context and serializes claims on one address with an advisory lock. Another
organization's attempt gets the fixed `409 platform_already_registered` and learns nothing about
the holder; archiving the last target on a claim releases it.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.adapters.native.addresses import address_key
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery.service import PLATFORM_ALREADY_REGISTERED
from tests.connections.routes import as_user
from tests.delivery.routes import address, counts, delivery_client, register, target_body, targets
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws2_slug, ws_slug

CLAIM = text("SELECT shifde.delivery_claim_platform('databricks', :k)")


def _assert_fixed_409(r: Any, world: World, url: str) -> None:
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "platform_already_registered"
    assert body["detail"] == PLATFORM_ALREADY_REGISTERED
    assert not body.get("errors")
    for leak in (
        "uq_delivery",
        "delivery_platform_claims",
        "delivery_claim_platform",
        "unique",
        "DETAIL",
        address_key(url.lower()),
        world.a.slug,
        str(world.a.tenant),
        world.a.tenant.hex,
    ):
        assert leak not in r.text, leak


def test_targets_of_one_organization_share_one_claim(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    url = address()
    first = register(client, h, world.a.slug, target_body(ws_slug(world.a), url))
    second = register(
        client, h, world.a.slug, target_body(ws2_slug(world.a), url.upper() + ":443/")
    )
    assert first["address"] == second["address"] == url
    assert counts(owner_db, world.a.tenant) == (2, 1)
    with owner_db.session() as o:
        claims = o.execute(
            text("SELECT DISTINCT claim_id FROM shifde.delivery_targets WHERE tenant_id = :t"),
            {"t": world.a.tenant},
        ).all()
    assert len(claims) == 1


@pytest.mark.parametrize("variant", ["same", "upper", "port", "slash"])
def test_another_organization_gets_the_fixed_409(
    client: TestClient, world: World, people: People, owner_db: Database, variant: str
) -> None:
    url = address()
    host = url.removeprefix("https://")
    sent = {
        "same": url,
        "upper": f"HTTPS://{host.upper()}",
        "port": f"{url}:443",
        "slash": f"{url}/",
    }[variant]
    register(
        client, as_user(client, people.owner_a), world.a.slug, target_body(ws_slug(world.a), url)
    )
    h = as_user(client, people.owner_b)
    r = client.post(targets(world.b.slug), headers=h, json=target_body(ws_slug(world.b), sent))
    _assert_fixed_409(r, world, url)
    assert counts(owner_db, world.b.tenant) == (0, 0)
    assert counts(owner_db, world.a.tenant) == (1, 1)


def test_archiving_the_last_target_releases_the_claim(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    url = address()
    ha = as_user(client, people.owner_a)
    one = register(client, ha, world.a.slug, target_body(ws_slug(world.a), url))
    two = register(client, ha, world.a.slug, target_body(ws2_slug(world.a), url))
    assert client.post(f"{targets(world.a.slug)}/{one['id']}/archive", headers=ha).is_success
    assert counts(owner_db, world.a.tenant) == (2, 1)  # the other target still holds it
    hb = as_user(client, people.owner_b)
    blocked = client.post(
        targets(world.b.slug), headers=hb, json=target_body(ws_slug(world.b), url)
    )
    _assert_fixed_409(blocked, world, url)
    ha = as_user(client, people.owner_a)
    assert client.post(f"{targets(world.a.slug)}/{two['id']}/archive", headers=ha).is_success
    assert counts(owner_db, world.a.tenant) == (2, 0)
    hb = as_user(client, people.owner_b)
    taken = register(client, hb, world.b.slug, target_body(ws_slug(world.b), url))
    assert taken["address"] == url
    assert counts(owner_db, world.b.tenant) == (1, 1)
    # and now A is the one refused
    ha = as_user(client, people.owner_a)
    again = client.post(targets(world.a.slug), headers=ha, json=target_body(ws_slug(world.a), url))
    assert again.status_code == 409 and again.json()["code"] == "platform_already_registered"


def test_the_same_organization_registering_again_reuses_its_claim(
    app_db: Database, world: World
) -> None:
    key = address_key(address())
    principal = uuid7()
    with app_session(app_db, world.a.tenant, principal) as s:
        first = s.execute(CLAIM, {"k": key}).scalar_one()
        s.commit()
    with app_session(app_db, world.a.tenant, principal) as s:
        assert s.execute(CLAIM, {"k": key}).scalar_one() == first
        assert s.execute(CLAIM, {"k": key}).scalar_one() == first
        s.commit()
    with app_session(app_db, world.b.tenant, uuid7()) as s:
        assert s.execute(CLAIM, {"k": key}).scalar_one() is None
        s.commit()


def test_two_organizations_racing_for_one_address(
    app_db: Database, owner_db: Database, world: World
) -> None:
    """A holds the advisory lock with its claim uncommitted; B's call waits for it and, once A
    commits, answers None (no claim, nothing about A)."""
    key = address_key(address())
    result: list[UUID | None] = []
    with app_session(app_db, world.a.tenant, uuid7()) as sa:
        won = sa.execute(CLAIM, {"k": key}).scalar_one()
        assert isinstance(won, UUID)

        def b_claims() -> None:
            with app_session(app_db, world.b.tenant, uuid7()) as sb:
                result.append(sb.execute(CLAIM, {"k": key}).scalar_one())
                sb.commit()

        racer = threading.Thread(target=b_claims)
        racer.start()
        racer.join(timeout=1.0)
        assert racer.is_alive()  # B waits on A's lock
        sa.commit()
    racer.join(timeout=30)
    assert result == [None]
    with owner_db.session() as o:
        holders = (
            o.execute(
                text(
                    "SELECT tenant_id FROM shifde.delivery_platform_claims WHERE address_key = :k"
                ),
                {"k": key},
            )
            .scalars()
            .all()
        )
    assert holders == [world.a.tenant]


def test_concurrent_registrations_through_the_routes(
    settings: Settings, world: World, people: People, owner_db: Database
) -> None:
    """Both organizations post the same new address at once, five times: each time exactly one
    gets the target and the other the fixed 409."""

    def post(
        start: threading.Barrier, c: TestClient, h: dict[str, str], slug: str, body: Any
    ) -> Any:
        start.wait()
        return c.post(targets(slug), headers=h, json=body)

    with delivery_client(settings) as ca, delivery_client(settings) as cb:
        ha, hb = as_user(ca, people.owner_a), as_user(cb, people.owner_b)
        for _ in range(5):
            url = address()
            start = threading.Barrier(2)
            with ThreadPoolExecutor(2) as pool:
                ra = pool.submit(
                    post, start, ca, ha, world.a.slug, target_body(ws_slug(world.a), url)
                )
                rb = pool.submit(
                    post, start, cb, hb, world.b.slug, target_body(ws_slug(world.b), url)
                )
                answers = sorted([ra.result().status_code, rb.result().status_code])
            assert answers == [201, 409]
            loser = rb.result() if rb.result().status_code == 409 else ra.result()
            assert loser.json()["detail"] == PLATFORM_ALREADY_REGISTERED
            with owner_db.session() as o:
                assert (
                    o.execute(
                        text(
                            "SELECT count(*) FROM shifde.delivery_platform_claims "
                            "WHERE address_key = :k"
                        ),
                        {"k": address_key(url)},
                    ).scalar_one()
                    == 1
                )


def test_the_definer_needs_a_client_and_a_principal(app_db: Database, world: World) -> None:
    key = address_key(address())
    for tenant, principal in ((None, uuid7()), (world.a.tenant, None), (None, None)):
        with app_session(app_db, tenant, principal) as s:
            with pytest.raises(DBAPIError, match="platform claim refused"):
                s.execute(CLAIM, {"k": key})
            s.rollback()


def test_rls_and_grants_on_the_claim_and_target_tables(
    client: TestClient, app_db: Database, world: World, people: People
) -> None:
    for side, email in ((world.a, people.owner_a), (world.b, people.owner_b)):
        register(client, as_user(client, email), side.slug, target_body(ws_slug(side), address()))
    with app_session(app_db, world.a.tenant, uuid7()) as s:
        claims = s.execute(text("SELECT tenant_id FROM shifde.delivery_platform_claims")).all()
        assert [c.tenant_id for c in claims] == [world.a.tenant]  # B's claim is not visible
        rows = s.execute(text("SELECT tenant_id FROM shifde.delivery_targets")).all()
        assert [r.tenant_id for r in rows] == [world.a.tenant]
        own_claim = s.execute(text("SELECT id FROM shifde.delivery_platform_claims")).scalar_one()
        for sql, params in (
            # no INSERT on claims at all: the definer is the only writer (B-2523)
            (
                "INSERT INTO shifde.delivery_platform_claims (id, tenant_id, platform_kind, "
                "address_key, created_by) VALUES (:i, :t, 'databricks', :k, :i)",
                {"i": uuid7(), "t": world.b.tenant, "k": "0" * 64},
            ),
            (
                "INSERT INTO shifde.delivery_platform_claims (id, tenant_id, platform_kind, "
                "address_key, created_by) VALUES (:i, :t, 'databricks', :k, :i)",
                {"i": uuid7(), "t": world.a.tenant, "k": "1" * 64},
            ),
            (
                "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, name, "
                "platform_kind, address, claim_id, environment, bindings, credential_ref, "
                "created_by) VALUES (:i, :t, :w, 'x', 'databricks', 'https://x', NULL, 'dev', "
                "'{}', 'ref', :i)",
                {"i": uuid7(), "t": world.b.tenant, "w": world.b.ws},
            ),
            ("UPDATE shifde.delivery_platform_claims SET created_by = :i", {"i": uuid7()}),
            ("UPDATE shifde.delivery_platform_claims SET updated_at = now()", {}),
            ("DELETE FROM shifde.delivery_targets", {}),
            ("UPDATE shifde.delivery_targets SET name = 'renamed'", {}),
        ):
            with pytest.raises(DBAPIError):
                s.execute(text(sql), params)
            s.rollback()
        # B's rows are out of reach of an UPDATE or DELETE under A's context
        assert (
            s.execute(
                text("DELETE FROM shifde.delivery_platform_claims WHERE tenant_id = :t"),
                {"t": world.b.tenant},
            ).rowcount
            == 0
        )
        assert (
            s.execute(
                text("UPDATE shifde.delivery_targets SET revision = 9 WHERE tenant_id = :t"),
                {"t": world.b.tenant},
            ).rowcount
            == 0
        )
        assert own_claim is not None
        s.rollback()
