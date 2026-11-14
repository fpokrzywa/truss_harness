"""Rate cards: resolution by time and tenant, the insert guard, immutability (SETH 3ba0e06 p08)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.ledger import pricing
from tests._support.tenants import add_tenants
from tests.ledger.util import PAST, add_rate, add_rate_card, add_version


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def test_a_tenant_card_overrides_the_global_one_and_time_selects_the_version(
    owner_session: Session,
) -> None:
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    add_rate(s, None, input_rate="1")
    add_rate(s, t, input_rate="5")
    now = utcnow()
    own = pricing.resolve(s, t, "openai", "m1", now)
    assert own is not None and own.scope == "tenant" and own.input_rate == Decimal(5)
    other = pricing.resolve(s, uuid7(), "openai", "m1", now)
    assert other is not None and other.scope == "global" and other.input_rate == Decimal(1)
    assert pricing.resolve(s, t, "openai", "no-such-model", now) is None
    assert pricing.resolve(s, t, "openai", "m1", PAST - timedelta(days=1)) is None


def test_a_new_version_supersedes_the_open_ended_one_from_its_start(
    owner_session: Session,
) -> None:
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    card = add_rate_card(s, t)
    v1 = add_version(s, card, t, 1, input_rate="1")
    start = utcnow() + timedelta(hours=1)
    v2 = add_version(s, card, t, 2, frm=start, input_rate="9")
    closed = s.execute(
        text("SELECT effective_to FROM shifde.rate_card_versions WHERE id = :i"), {"i": v1}
    ).scalar_one()
    assert closed == start
    now_version = pricing.resolve(s, t, "openai", "m1", utcnow())
    later = pricing.resolve(s, t, "openai", "m1", start + timedelta(minutes=1))
    assert now_version is not None and now_version.id == v1
    assert later is not None and later.id == v2 and later.input_rate == Decimal(9)


def test_a_version_closing_an_open_ended_one_in_the_past_is_refused(
    owner_session: Session,
) -> None:
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    card = add_rate_card(s, t)
    add_version(s, card, t, 1)
    with pytest.raises(DBAPIError) as err, s.begin_nested():
        add_version(s, card, t, 2, frm=PAST + timedelta(days=1))
    assert _sqlstate(err.value) == "23P01"


def test_an_overlapping_version_is_refused(owner_session: Session) -> None:
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    card = add_rate_card(s, t)
    add_version(s, card, t, 1, frm=PAST, to=PAST + timedelta(days=10))
    with pytest.raises(DBAPIError) as err, s.begin_nested():
        add_version(s, card, t, 2, frm=PAST + timedelta(days=5), to=PAST + timedelta(days=20))
    assert _sqlstate(err.value) == "23P01"
    # inside free history and overlapping nothing it is allowed
    add_version(s, card, t, 3, frm=PAST + timedelta(days=10), to=PAST + timedelta(days=12))


def test_a_version_belongs_to_the_tenant_of_its_card(owner_session: Session) -> None:
    s, a, b = owner_session, uuid7(), uuid7()
    add_tenants(s, a, b)
    card = add_rate_card(s, a)
    with pytest.raises(DBAPIError) as err, s.begin_nested():
        add_version(s, card, b, 1)
    assert _sqlstate(err.value) == "23514"
    global_card = add_rate_card(s, None)
    with pytest.raises(DBAPIError) as err2, s.begin_nested():
        add_version(s, global_card, a, 1)
    assert _sqlstate(err2.value) == "23514"


def test_only_the_guard_may_close_a_version_and_versions_are_never_deleted(
    owner_session: Session,
) -> None:
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    card = add_rate_card(s, t)
    v1 = add_version(s, card, t, 1)
    # the guard closes a predecessor from inside its own trigger (depth 2) ...
    add_version(s, card, t, 2, frm=utcnow() + timedelta(hours=1))
    # ... nobody else can: not the close, not another column, not a delete (owner included)
    statements = (
        "UPDATE shifde.rate_card_versions SET effective_to = now() + interval '2 hours'"
        " WHERE id = :i",
        "UPDATE shifde.rate_card_versions SET input_rate = 99 WHERE id = :i",
        "DELETE FROM shifde.rate_card_versions WHERE id = :i",
    )
    for sql in statements:
        with pytest.raises(DBAPIError) as err, s.begin_nested():
            s.execute(text(sql), {"i": v1})
        assert _sqlstate(err.value) == "42501", sql
    open_one = add_version(s, add_rate_card(s, t, model_key="open"), t, 1)
    with pytest.raises(DBAPIError) as err3, s.begin_nested():
        s.execute(
            text("UPDATE shifde.rate_card_versions SET effective_to = now() WHERE id = :i"),
            {"i": open_one},
        )
    assert _sqlstate(err3.value) == "42501"


def test_the_app_role_supersedes_through_the_guard_and_cannot_update_directly(
    app_db: Database, owner_db: Database
) -> None:
    t = uuid7()
    with owner_db.session() as owner:
        add_tenants(owner, t)
        owner.commit()
    with app_db.session() as s:
        set_tenant(s, t)
        assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        card = add_rate_card(s, t)
        v1 = add_version(s, card, t, 1)
        add_version(s, card, t, 2, frm=utcnow() + timedelta(hours=1))
        s.commit()
    with app_db.session() as s:
        set_tenant(s, t)
        assert s.execute(
            text("SELECT effective_to IS NOT NULL FROM shifde.rate_card_versions WHERE id = :i"),
            {"i": v1},
        ).scalar_one()
        with pytest.raises(DBAPIError) as err:
            s.execute(
                text("UPDATE shifde.rate_card_versions SET input_rate = 0 WHERE id = :i"),
                {"i": v1},
            )
        assert _sqlstate(err.value) == "42501"  # no UPDATE grant
        s.rollback()


def test_rate_columns_reject_bad_values(owner_session: Session) -> None:
    s, t = owner_session, uuid7()
    add_tenants(s, t)
    card = add_rate_card(s, t)
    with pytest.raises(IntegrityError), s.begin_nested():
        add_version(s, card, t, 1, input_rate="-1")
    with pytest.raises(IntegrityError), s.begin_nested():
        add_version(s, card, t, 1, currency="usd")


def _version(unit: str, cached_read_rate: Decimal | None = None) -> pricing.RateVersion:
    return pricing.RateVersion(
        id=uuid7(),
        rate_card_id=uuid7(),
        scope="tenant",
        provider_key="p",
        model_key="m",
        version_no=1,
        effective_from=PAST,
        effective_to=None,
        currency="USD",
        unit=unit,
        input_rate=Decimal(3),
        output_rate=Decimal(15),
        cached_read_rate=cached_read_rate,
        cached_write_rate=None,
        other_rates={},
        source="test",
        revision="r",
    )


def test_calculation_per_unit_and_the_cached_fallback() -> None:
    usage = pricing.Usage(
        input_tokens=1_000_000, output_tokens=2_000_000, cached_read_tokens=1_000_000, calls=4
    )
    # cached reads fall back to the input rate when the card has none
    assert pricing.calculate(_version("per_1m_tokens"), usage) == Decimal(3 + 30 + 3)
    assert pricing.calculate(_version("per_1m_tokens", Decimal("0.3")), usage) == Decimal("33.3")
    assert pricing.calculate(_version("per_call"), usage) == Decimal(12)
    assert pricing.calculate(_version("per_second"), pricing.Usage(duration_ms=1500)) == Decimal(
        "4.5"
    )
    assert pricing.price(None, usage) is pricing.UNMEASURED
