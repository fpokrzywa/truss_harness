"""One platform per organization (design 3, B-2501; lead ruling B-2524).

Claims are written only by the `shifde.delivery_claim_platform` definer, which reads the client
from the session context and serializes claims on one address with an advisory lock. Another
organization's attempt gets the fixed `409 platform_already_registered` and learns nothing about
the holder; archiving the last target on a claim releases it. Snowflake accounts (SP6-3) are
claimed the same way, under their own platform kind (delivery_0007 widens the CHECKs), and so are
Fabric workspaces, Palantir Foundry stacks and Docker daemons (SP8-1, delivery_0008): every
accepted form of one address is one claim, and another organization gets the same fixed 409.
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

from shifde.adapters.native.addresses import (
    SnowflakeAddressRefused,
    address_key,
    snowflake_claim_identity,
)
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery.service import PLATFORM_ALREADY_REGISTERED
from tests.connections.routes import as_user
from tests.delivery.routes import (
    SNOWFLAKE_BINDINGS,
    SP8_KINDS,
    address,
    counts,
    delivery_client,
    register,
    snowflake_address,
    snowflake_body,
    sp8_address,
    sp8_body,
    target_body,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws2_slug, ws_slug

CLAIM = text("SELECT shifde.delivery_claim_platform('databricks', :k)")
SNOWFLAKE_CLAIM = text("SELECT shifde.delivery_claim_platform('snowflake', :k)")
SNOWFLAKE_REFUSED = str(SnowflakeAddressRefused())


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


def test_another_port_on_the_same_host_is_refused(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """Only `:443` is accepted, so the claim key is host-only: B cannot claim A's workspace again
    under another port (B-2524)."""
    url = address()
    register(
        client, as_user(client, people.owner_a), world.a.slug, target_body(ws_slug(world.a), url)
    )
    hb = as_user(client, people.owner_b)
    ported = client.post(
        targets(world.b.slug), headers=hb, json=target_body(ws_slug(world.b), f"{url}:8443")
    )
    assert ported.status_code == 422, ported.text
    assert ported.json()["code"] == "validation_error"
    assert world.a.slug not in ported.text and str(world.a.tenant) not in ported.text
    plain = client.post(targets(world.b.slug), headers=hb, json=target_body(ws_slug(world.b), url))
    _assert_fixed_409(plain, world, url)
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
            # no INSERT on claims at all: the definer is the only writer (B-2524)
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


def _claim_state(owner_db: Database, tenant: UUID, url: str) -> tuple[int, int]:
    """(claims on `url`, active targets on `url`) of `tenant`, read as the schema owner."""
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_platform_claims "
                "WHERE tenant_id = :t AND address_key = :k), "
                "(SELECT count(*) FROM shifde.delivery_targets t "
                "JOIN shifde.delivery_platform_claims c "
                "ON c.tenant_id = t.tenant_id AND c.id = t.claim_id "
                "WHERE t.tenant_id = :t AND t.status = 'active' AND c.address_key = :k), "
                "(SELECT count(*) FROM shifde.delivery_targets "
                "WHERE tenant_id = :t AND status = 'active' AND address = :u)"
            ),
            {"t": tenant, "k": address_key(url), "u": url},
        ).one()
    claims, held, active = int(row[0]), int(row[1]), int(row[2])
    assert held == active  # every active target on the address holds its claim
    return claims, active


def test_archive_racing_register_in_one_organization(
    settings: Settings, world: World, people: People, owner_db: Database
) -> None:
    """A archives its only target on an address while registering another on it, at once, five
    times: the end state is either a claim with an active target, or no claim and no active
    target; never a claim without a target or a target without its claim."""

    def archive(start: threading.Barrier, c: TestClient, h: dict[str, str], tid: str) -> Any:
        start.wait()
        return c.post(f"{targets(world.a.slug)}/{tid}/archive", headers=h)

    def post(start: threading.Barrier, c: TestClient, h: dict[str, str], body: Any) -> Any:
        start.wait()
        return c.post(targets(world.a.slug), headers=h, json=body)

    with delivery_client(settings) as c1, delivery_client(settings) as c2:
        h1, h2 = as_user(c1, people.owner_a), as_user(c2, people.owner_a)
        for _ in range(5):
            url = address()
            first = register(c1, h1, world.a.slug, target_body(ws_slug(world.a), url))
            start = threading.Barrier(2)
            with ThreadPoolExecutor(2) as pool:
                archived = pool.submit(archive, start, c1, h1, first["id"])
                posted = pool.submit(post, start, c2, h2, target_body(ws2_slug(world.a), url))
                assert archived.result().status_code == 200, archived.result().text
                assert posted.result().status_code == 201, posted.result().text
            claims, active = _claim_state(owner_db, world.a.tenant, url)
            assert (claims, active) in ((1, 1), (0, 0))
            assert (claims, active) == (1, 1)  # the registration always ends holding a claim


def test_the_app_role_cannot_delete_a_claim_an_active_target_holds(
    client: TestClient, app_db: Database, owner_db: Database, world: World, people: People
) -> None:
    url = address()
    register(
        client, as_user(client, people.owner_a), world.a.slug, target_body(ws_slug(world.a), url)
    )
    with app_session(app_db, world.a.tenant, uuid7()) as s:
        own_claim = s.execute(text("SELECT id FROM shifde.delivery_platform_claims")).scalar_one()
        with pytest.raises(DBAPIError):
            s.execute(
                text("DELETE FROM shifde.delivery_platform_claims WHERE id = :c"), {"c": own_claim}
            )
        s.rollback()
    assert _claim_state(owner_db, world.a.tenant, url) == (1, 1)


# ------------------------------------------------------------------ Snowflake claims (SP6-3)
def test_snowflake_targets_of_one_organization_share_one_claim(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    h = as_user(client, people.owner_a)
    url = snowflake_address()
    first = register(client, h, world.a.slug, snowflake_body(ws_slug(world.a), url))
    bindings = {**SNOWFLAKE_BINDINGS, "package": "PKG_2", "application": "APP_2"}
    second = register(
        client,
        h,
        world.a.slug,
        snowflake_body(ws2_slug(world.a), url.upper() + ":443/", bindings=bindings),
    )
    assert first["address"] == second["address"] == url
    assert counts(owner_db, world.a.tenant) == (2, 1)


def test_another_organization_registering_a_snowflake_account_gets_the_fixed_409(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    url = snowflake_address()
    ha = as_user(client, people.owner_a)
    register(client, ha, world.a.slug, snowflake_body(ws_slug(world.a), url))
    hb = as_user(client, people.owner_b)
    r = client.post(
        targets(world.b.slug), headers=hb, json=snowflake_body(ws_slug(world.b), url.upper())
    )
    _assert_fixed_409(r, world, url)
    # byte for byte the Databricks refusal, but for the varying request fields
    dbx = address()
    register(
        client, as_user(client, people.owner_a), world.a.slug, target_body(ws_slug(world.a), dbx)
    )
    hb = as_user(client, people.owner_b)
    d = client.post(targets(world.b.slug), headers=hb, json=target_body(ws_slug(world.b), dbx))
    varying = {"instance", "request_id"}
    assert {k: v for k, v in r.json().items() if k not in varying} == {
        k: v for k, v in d.json().items() if k not in varying
    }
    assert counts(owner_db, world.b.tenant) == (0, 0)
    assert counts(owner_db, world.a.tenant) == (2, 2)


@pytest.mark.parametrize(
    "form",
    [
        "https://{host}.privatelink.snowflakecomputing.com",
        "https://{upper}.PRIVATELINK.snowflakecomputing.com:443/",
        "https://{underscored}.snowflakecomputing.com",
    ],
    ids=["privatelink", "privatelink-upper", "underscore"],
)
def test_another_organization_claiming_a_snowflake_account_in_another_url_form_gets_the_409(
    client: TestClient, world: World, people: People, owner_db: Database, form: str
) -> None:
    """A Snowflake claim keys on the account's claim identity (B-3942), so B claiming A's account
    through its privatelink (or any other) URL form gets the fixed 409, not a second claim."""
    url = snowflake_address()
    host = url.removeprefix("https://").removesuffix(".snowflakecomputing.com")
    ha = as_user(client, people.owner_a)
    register(client, ha, world.a.slug, snowflake_body(ws_slug(world.a), url))
    other = form.format(host=host, upper=host.upper(), underscored=host.replace("-", "_"))
    hb = as_user(client, people.owner_b)
    r = client.post(targets(world.b.slug), headers=hb, json=snowflake_body(ws_slug(world.b), other))
    _assert_fixed_409(r, world, url)
    assert address_key(snowflake_claim_identity(url)) not in r.text
    assert counts(owner_db, world.b.tenant) == (0, 0)
    assert counts(owner_db, world.a.tenant) == (1, 1)


def test_a_privatelink_holder_refuses_another_organization_the_plain_url(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    url = snowflake_address()
    private = url.replace(".snowflakecomputing.com", ".privatelink.snowflakecomputing.com")
    ha = as_user(client, people.owner_a)
    made = register(client, ha, world.a.slug, snowflake_body(ws_slug(world.a), private))
    assert made["address"] == private
    hb = as_user(client, people.owner_b)
    r = client.post(targets(world.b.slug), headers=hb, json=snowflake_body(ws_slug(world.b), url))
    _assert_fixed_409(r, world, private)
    assert counts(owner_db, world.b.tenant) == (0, 0)


@pytest.mark.parametrize(
    "url",
    [
        "https://xy12345.snowflakecomputing.com",
        "https://xy12345.privatelink.snowflakecomputing.com",
        "https://xy12345.us-east-2.aws.snowflakecomputing.com",
        "https://xy12345.us-east-2.aws.privatelink.snowflakecomputing.com",
        "https://myorg-acct.us-east-1.snowflakecomputing.com",
    ],
    ids=["locator", "locator-privatelink", "locator-region", "locator-region-pl", "org-region"],
)
def test_a_snowflake_address_without_a_claim_identity_claims_nothing(
    client: TestClient, world: World, people: People, owner_db: Database, url: str
) -> None:
    """An account-locator form cannot be shown to be the same account as an org-account name, so
    it is refused before any claim (422 at `body.address`, the fixed message) for every
    organization: B cannot reach A's account through its locator either."""
    ha = as_user(client, people.owner_a)
    register(client, ha, world.a.slug, snowflake_body(ws_slug(world.a), snowflake_address()))
    for slug, person, ws in (
        (world.a.slug, people.owner_a, ws2_slug(world.a)),
        (world.b.slug, people.owner_b, ws_slug(world.b)),
    ):
        h = as_user(client, person)
        r = client.post(targets(slug), headers=h, json=snowflake_body(ws, url))
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == SNOWFLAKE_REFUSED
        assert r.json()["errors"][0]["loc"] == ["body", "address"]
        assert url not in r.text
    assert counts(owner_db, world.a.tenant) == (1, 1)
    assert counts(owner_db, world.b.tenant) == (0, 0)


def test_archiving_the_last_snowflake_target_releases_the_claim(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    url = snowflake_address()
    ha = as_user(client, people.owner_a)
    made = register(client, ha, world.a.slug, snowflake_body(ws_slug(world.a), url))
    assert client.post(f"{targets(world.a.slug)}/{made['id']}/archive", headers=ha).is_success
    assert counts(owner_db, world.a.tenant) == (1, 0)
    hb = as_user(client, people.owner_b)
    taken = register(client, hb, world.b.slug, snowflake_body(ws_slug(world.b), url))
    assert taken["address"] == url
    assert counts(owner_db, world.b.tenant) == (1, 1)


def test_a_databricks_and_a_snowflake_claim_do_not_collide(app_db: Database, world: World) -> None:
    key = address_key(address())
    with app_session(app_db, world.a.tenant, uuid7()) as s:
        databricks = s.execute(CLAIM, {"k": key}).scalar_one()
        s.commit()
    with app_session(app_db, world.b.tenant, uuid7()) as s:
        snowflake = s.execute(SNOWFLAKE_CLAIM, {"k": key}).scalar_one()
        s.commit()
    assert isinstance(databricks, UUID) and isinstance(snowflake, UUID)
    assert databricks != snowflake
    with app_session(app_db, world.a.tenant, uuid7()) as s:
        assert s.execute(SNOWFLAKE_CLAIM, {"k": key}).scalar_one() is None  # B holds it
        s.rollback()


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def test_the_widened_checks_accept_snowflake_and_refuse_another_kind(
    app_db: Database, world: World
) -> None:
    principal = uuid7()
    target = (
        "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, name, platform_kind, "
        "address, claim_id, environment, bindings, credential_ref, status, created_by) VALUES "
        "(:i, :t, :w, 'x', :k, 'https://x', NULL, 'dev', '{}', 'ref', 'archived', :i)"
    )
    artifact = (
        "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, size, "
        "content, created_by) VALUES (:t, :i, :k, :d, 1, :c, :i)"
    )
    with app_session(app_db, world.a.tenant, principal) as s:
        assert isinstance(s.execute(SNOWFLAKE_CLAIM, {"k": "a" * 64}).scalar_one(), UUID)
        s.execute(
            text(target), {"i": uuid7(), "t": world.a.tenant, "w": world.a.ws, "k": "snowflake"}
        )
        s.execute(
            text(artifact),
            {
                "i": uuid7(),
                "t": world.a.tenant,
                "k": "snowflake.native-app/v1",
                "d": "e" * 64,
                "c": b"\x00",
            },
        )
        s.commit()
    for sql, params in (
        ("SELECT shifde.delivery_claim_platform('oracle', :k)", {"k": "b" * 64}),
        (target, {"i": uuid7(), "t": world.a.tenant, "w": world.a.ws, "k": "oracle"}),
        (
            artifact,
            {"i": uuid7(), "t": world.a.tenant, "k": "oracle.app/v1", "d": "e" * 64, "c": b"\x00"},
        ),
    ):
        with app_session(app_db, world.a.tenant, principal) as s:
            with pytest.raises(DBAPIError) as raised:
                s.execute(text(sql), params)
            assert _sqlstate(raised.value) == "23514"
            s.rollback()


def test_bs_snowflake_target_and_claim_are_invisible_to_a(
    client: TestClient, app_db: Database, world: World, people: People
) -> None:
    hb = as_user(client, people.owner_b)
    made = register(client, hb, world.b.slug, snowflake_body(ws_slug(world.b), snowflake_address()))
    with app_session(app_db, world.a.tenant, uuid7()) as s:
        assert (
            s.execute(
                text("SELECT id FROM shifde.delivery_targets WHERE id = :i"), {"i": made["id"]}
            ).all()
            == []
        )
        assert (
            s.execute(
                text(
                    "SELECT id FROM shifde.delivery_platform_claims WHERE platform_kind = 'snowflake'"
                )
            ).all()
            == []
        )
        s.rollback()
    with app_session(app_db, world.b.tenant, uuid7()) as s:
        assert (
            len(
                s.execute(
                    text(
                        "SELECT id FROM shifde.delivery_platform_claims "
                        "WHERE platform_kind = 'snowflake'"
                    )
                ).all()
            )
            == 1
        )
        s.rollback()


# ------------------------------------------------------------------ Fabric, Palantir, Docker (SP8-1)
def _forms(kind: str, url: str) -> list[str]:
    """Every accepted form of the normalized address `url` of `kind` (the Docker daemon id is
    never case-folded, so a Docker address has one form)."""
    if kind == "fabric":
        guid = url.rsplit("/", 1)[1].upper()
        return [url, guid, f"https://app.fabric.microsoft.com/groups/{guid}/lakehouses"]
    if kind == "palantir":
        return [url, url.upper() + ":443/", url + "/"]
    return [url]


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_sp8_targets_of_one_organization_share_one_claim(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    h = as_user(client, people.owner_a)
    url = sp8_address(kind)
    forms = _forms(kind, url)
    spaces = (ws_slug(world.a), ws2_slug(world.a), ws_slug(world.a))
    made = [
        register(client, h, world.a.slug, sp8_body(kind, ws, form))
        for ws, form in zip(spaces, forms, strict=False)
    ]
    second = register(client, h, world.a.slug, sp8_body(kind, ws2_slug(world.a), url))
    assert {m["address"] for m in [*made, second]} == {url}
    assert all(m["platform_kind"] == kind for m in made)
    assert counts(owner_db, world.a.tenant) == (len(made) + 1, 1)
    with owner_db.session() as o:
        claim = o.execute(
            text(
                "SELECT platform_kind, address_key FROM shifde.delivery_platform_claims "
                "WHERE tenant_id = :t"
            ),
            {"t": world.a.tenant},
        ).one()
    assert tuple(claim) == (kind, address_key(url))


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_another_organization_registering_an_sp8_address_gets_the_fixed_409(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    url = sp8_address(kind)
    register(
        client, as_user(client, people.owner_a), world.a.slug, sp8_body(kind, ws_slug(world.a), url)
    )
    for form in _forms(kind, url):
        hb = as_user(client, people.owner_b)
        r = client.post(
            targets(world.b.slug), headers=hb, json=sp8_body(kind, ws_slug(world.b), form)
        )
        _assert_fixed_409(r, world, url)
        assert address_key(url) not in r.text and url not in r.text
    assert counts(owner_db, world.b.tenant) == (0, 0)
    assert counts(owner_db, world.a.tenant) == (1, 1)


def test_another_organization_cannot_register_a_sub_host_of_a_palantir_stack(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """B's sub-host form of A's stack is refused and writes nothing (it would be a second claim
    key of one stack)."""
    url = sp8_address("palantir")
    register(
        client,
        as_user(client, people.owner_a),
        world.a.slug,
        sp8_body("palantir", ws_slug(world.a), url),
    )
    sub_host = url.replace("https://", "https://eu.")
    hb = as_user(client, people.owner_b)
    r = client.post(
        targets(world.b.slug), headers=hb, json=sp8_body("palantir", ws_slug(world.b), sub_host)
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"
    assert sub_host not in r.text and url not in r.text
    assert counts(owner_db, world.b.tenant) == (0, 0)
    assert counts(owner_db, world.a.tenant) == (1, 1)


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_archiving_the_last_sp8_target_releases_the_claim(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    url = sp8_address(kind)
    ha = as_user(client, people.owner_a)
    made = register(client, ha, world.a.slug, sp8_body(kind, ws_slug(world.a), url))
    hb = as_user(client, people.owner_b)
    blocked = client.post(
        targets(world.b.slug), headers=hb, json=sp8_body(kind, ws_slug(world.b), url)
    )
    _assert_fixed_409(blocked, world, url)
    ha = as_user(client, people.owner_a)
    assert client.post(f"{targets(world.a.slug)}/{made['id']}/archive", headers=ha).is_success
    assert counts(owner_db, world.a.tenant) == (1, 0)
    hb = as_user(client, people.owner_b)
    taken = register(client, hb, world.b.slug, sp8_body(kind, ws_slug(world.b), url))
    assert taken["address"] == url
    assert counts(owner_db, world.b.tenant) == (1, 1)


def test_the_widened_checks_accept_the_sp8_kinds(app_db: Database, world: World) -> None:
    """delivery_0008: each new platform kind in claims and targets, and each new target kind in
    artifacts, as `shifde_app` in a client context; any other value is still 23514."""
    principal = uuid7()
    target = (
        "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, name, platform_kind, "
        "address, claim_id, environment, bindings, credential_ref, status, created_by) VALUES "
        "(:i, :t, :w, 'x', :k, 'https://x', NULL, 'dev', '{}', 'ref', 'archived', :i)"
    )
    artifact = (
        "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, size, "
        "content, created_by) VALUES (:t, :i, :k, :d, 1, :c, :i)"
    )
    kinds = {
        "fabric": "fabric.solution/v1",
        "palantir": "palantir.marketplace/v1",
        "docker": "docker.compose/v1",
    }
    for n, (kind, target_kind) in enumerate(kinds.items()):
        with app_session(app_db, world.a.tenant, principal) as s:
            claimed = s.execute(
                text("SELECT shifde.delivery_claim_platform(:p, :k)"),
                {"p": kind, "k": str(n) * 64},
            ).scalar_one()
            assert isinstance(claimed, UUID)
            s.execute(text(target), {"i": uuid7(), "t": world.a.tenant, "w": world.a.ws, "k": kind})
            s.execute(
                text(artifact),
                {"i": uuid7(), "t": world.a.tenant, "k": target_kind, "d": "e" * 64, "c": b"\x00"},
            )
            s.commit()
    for sql, params in (
        ("SELECT shifde.delivery_claim_platform('fabric.iq', :k)", {"k": "f" * 64}),
        (target, {"i": uuid7(), "t": world.a.tenant, "w": world.a.ws, "k": "Docker"}),
        (target, {"i": uuid7(), "t": world.a.tenant, "w": world.a.ws, "k": "foundry"}),
        (
            artifact,
            {
                "i": uuid7(),
                "t": world.a.tenant,
                "k": "palantir.superrepo/v1",
                "d": "e" * 64,
                "c": b"\x00",
            },
        ),
        (
            artifact,
            {"i": uuid7(), "t": world.a.tenant, "k": "fabric.iq/v1", "d": "e" * 64, "c": b"\x00"},
        ),
    ):
        with app_session(app_db, world.a.tenant, principal) as s:
            with pytest.raises(DBAPIError) as raised:
                s.execute(text(sql), params)
            assert _sqlstate(raised.value) == "23514"
            s.rollback()
