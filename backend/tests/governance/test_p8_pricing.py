"""Rate cards through the API, moved from SETH 3ba0e06 (`test_p8_cost.py`, `test_p8_scoping.py`,
`test_p8_review_r1.py`, `test_p8_review_r2.py`); assertions unchanged. SETH's `login` /
`user_session` become `as_user` on the tenancy route-test people; `priced_stack` (a model run
through a worker) is replaced by the rate card it created (`chat-a`, one open-ended version since
2000). The run-driven and usage tests move with governance parts 2 and 3 and runtime
(docs/build/carry_forward.md)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, platform_admin_context, set_tenant
from shifde.core.ids import uuid7
from shifde.governance import service as gov_service
from tests.connections.routes import as_user, limited_user, org, post
from tests.governance.routes import PLATFORM, rate_card, rate_version
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _iso(delta: timedelta) -> str:
    return (utcnow() + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_rate_card_versions_reject_overlaps_and_resolve_by_time(
    client: TestClient, world: World, people: People
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    card = rate_card(client, h, base, "m-1", "openai_compatible")
    dup = client.post(
        f"{base}/rate-cards",
        json={"provider_key": "openai_compatible", "model_key": "m-1", "name": "again"},
        headers=h,
    )
    assert dup.status_code == 409
    v1 = rate_version(client, h, base, card["id"], effective_to="2025-01-01T00:00:00Z")
    assert v1["version_no"] == 1
    clash = rate_version(
        client, h, base, card["id"], effective_from="2024-06-01T00:00:00Z", status=409
    )
    assert "overlaps" in clash["detail"]
    v2 = rate_version(
        client,
        h,
        base,
        card["id"],
        effective_from="2025-01-01T00:00:00Z",
        input_rate=3.0,
        output_rate=4.0,
        cached_read_rate=0.5,
    )
    assert v2["version_no"] == 2 and v2["cached_read_rate"] == 0.5
    bad = client.post(
        f"{base}/rate-cards/{card['id']}/versions",
        json={
            **{k: v2[k] for k in ("currency", "unit", "input_rate", "output_rate")},
            "effective_from": "2030-01-01T00:00:00Z",
            "effective_to": "2029-01-01T00:00:00Z",
            "source": "x",
            "revision": "r",
        },
        headers=h,
    )
    assert bad.status_code == 422
    q = "provider_key=openai_compatible&model_key=m-1"
    old = client.get(f"{base}/rate-cards/resolve?{q}&at=2020-01-01T00:00:00Z", headers=h).json()
    new = client.get(f"{base}/rate-cards/resolve?{q}&at=2026-01-01T00:00:00Z", headers=h).json()
    assert (old["version"]["version_no"], new["version"]["version_no"]) == (1, 2)
    assert (
        client.get(f"{base}/rate-cards/resolve?{q}&at=1999-01-01T00:00:00Z", headers=h).status_code
        == 404
    )
    listed = client.get(f"{base}/rate-cards/{card['id']}/versions", headers=h).json()["items"]
    assert [v["version_no"] for v in listed] == [2, 1]


def test_pricing_administration_needs_pricing_manage_and_platform_cards_need_platform_admin(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    card = rate_card(client, h, base, "m-1", "fake")
    vh = as_user(client, people.viewer_a)  # usage:read only
    assert client.get(f"{base}/rate-cards", headers=vh).status_code == 403
    body = {"provider_key": "fake", "model_key": "m-2", "name": "x"}
    assert client.post(f"{base}/rate-cards", json=body, headers=vh).status_code == 403
    email, _ = limited_user(owner_db, world.a.tenant, ["pricing:read"])
    rh = as_user(client, email)
    assert client.get(f"{base}/rate-cards", headers=rh).status_code == 200
    assert client.get(f"{base}/rate-cards/{card['id']}/versions", headers=rh).status_code == 200
    assert client.post(f"{base}/rate-cards", json=body, headers=rh).status_code == 403
    version = {
        "effective_from": "2000-01-01T00:00:00Z", "currency": "XTS", "unit": "per_call",
        "input_rate": 1, "output_rate": 0, "source": "s", "revision": "r",
    }  # fmt: skip
    assert (
        client.post(
            f"{base}/rate-cards/{card['id']}/versions", json=version, headers=rh
        ).status_code
        == 403
    )
    # a tenant administrator cannot create or version GLOBAL cards
    h = as_user(client, people.owner_a)
    assert client.get(f"{PLATFORM}/rate-cards", headers=h).status_code == 403
    assert client.post(f"{PLATFORM}/rate-cards", json=body, headers=h).status_code == 403
    ph = as_user(client, people.platform)
    gcard = post(client, f"{PLATFORM}/rate-cards", ph, body)
    assert gcard["scope"] == "global"
    h = as_user(client, people.owner_a)
    org_view = client.get(f"{base}/rate-cards", headers=h).json()["items"]
    assert {c["scope"] for c in org_view} == {"tenant", "global"}  # global cards are readable
    assert (
        client.post(
            f"{base}/rate-cards/{gcard['id']}/versions", json=version, headers=h
        ).status_code
        == 403
    )  # ... but not editable from a tenant
    ph = as_user(client, people.platform)
    listed = client.get(f"{PLATFORM}/rate-cards", headers=ph).json()["items"]
    assert [c["id"] for c in listed] == [gcard["id"]]  # the platform view never lists tenant cards
    # the platform admin has no tenant data-plane access
    assert client.get(f"{base}/rate-cards", headers=ph).status_code in (403, 404)


def test_only_the_guard_may_close_a_version_and_a_version_belongs_to_its_cards_tenant(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    card = rate_card(client, h, base, "m-guard", "fake")
    v1 = rate_version(client, h, base, card["id"])
    # outside the guard even the database owner cannot close (or otherwise change) a version
    for stmt in (
        "UPDATE shifde.rate_card_versions SET effective_to = '2030-01-01'",
        "UPDATE shifde.rate_card_versions SET input_rate = 1",
        "DELETE FROM shifde.rate_card_versions",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            owner_session.execute(text(stmt))
        owner_session.rollback()
    assert v1["effective_to"] is None
    tid = world.a.tenant
    other = world.b.tenant
    global_card = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.rate_cards (id, tenant_id, provider_key, model_key, name) "
            "VALUES (:i, NULL, 'fake', 'm-global', 'g')"
        ),
        {"i": global_card},
    )
    owner_session.commit()
    insert = text(
        "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, version_no, "
        "effective_from, currency, unit, input_rate, output_rate, source, revision) "
        "VALUES (:i, :c, :t, 1, '2000-01-01', 'XTS', 'per_1m_tokens', 1, 1, 's', 'r')"
    )
    # In the wrong tenant's own context (as tests.ledger.util._seed_context): since ledger_0002
    # the guard refuses a client row outside the caller's context before this check, owner
    # sessions included (governance isolation review 1, item 1; B-611)
    for card_id, wrong_tenant in ((UUID(card["id"]), other), (global_card, tid)):
        set_tenant(owner_session, wrong_tenant)
        with pytest.raises(DBAPIError, match="does not match its rate card"):
            owner_session.execute(insert, {"i": uuid7(), "c": card_id, "t": wrong_tenant})
        owner_session.rollback()
    set_tenant(owner_session, None)
    # NULL-safe: a global card's version without a tenant is accepted (since ledger isolation
    # review 1, item 1, in the platform administrator context: the guard refuses a row outside
    # the caller's context, owner sessions included, B-601, B-611)
    with platform_admin_context(owner_session):
        owner_session.execute(insert, {"i": uuid7(), "c": global_card, "t": None})
        owner_session.commit()


def test_a_version_that_would_close_an_open_ended_one_in_the_past_is_refused(
    client: TestClient, world: World, people: People
) -> None:
    base = org(world.a.slug)
    h = as_user(client, people.owner_a)
    card = rate_card(client, h, base, "chat-a", "fake")
    stack = rate_version(client, h, base, card["id"])  # v1 open-ended since 2000
    q = "provider_key=fake&model_key=chat-a&at=2010-01-01T00:00:00Z"
    before = client.get(f"{base}/rate-cards/resolve?{q}", headers=h).json()
    assert before["version"]["version_no"] == 1 and stack
    past = rate_version(
        client, h, base, card["id"], effective_from="2020-01-01T00:00:00Z", status=409
    )
    assert "past" in past["detail"]
    versions = client.get(f"{base}/rate-cards/{card['id']}/versions", headers=h).json()["items"]
    assert [v["version_no"] for v in versions] == [1] and versions[0]["effective_to"] is None
    # history resolves exactly as before, and a version starting later still supersedes
    after = client.get(f"{base}/rate-cards/resolve?{q}", headers=h).json()
    assert after["version"]["version_no"] == 1
    ok = rate_version(client, h, base, card["id"], effective_from=_iso(timedelta(days=1)))
    assert ok["version_no"] == 2


def test_csv_cells_neutralise_formulas_but_keep_numbers_numeric() -> None:
    cell = gov_service._csv_cell
    assert cell(Decimal("-0.0012")) == "-0.0012" and cell(-5) == "-5" and cell(-1.5) == "-1.5"
    for hostile in ("=1+1", "+cmd", "-cmd|x", "@SUM(A1)", "\tx", "-1+2"):
        assert cell(hostile) == "'" + hostile
    assert cell(None) == "" and cell("plain") == "plain" and cell(True) == "True"
    out = gov_service._csv([[Decimal("-3"), "=x"]], ["a", "b"])
    assert out.splitlines()[1] == "-3,'=x"
