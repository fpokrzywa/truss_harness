"""Direct model invocations hold their budget (issue #82, design 12.1 and 12.2): an invoke through
the route, against the fake provider, under a covering budget places one hold keyed by a new
invocation (no run) in the transaction of the budget check, marks it with the limiter commit that
precedes the provider call, and settles it with the recorded usage (an answer) or the failed
usage row (0 tokens, `AdapterFailure`). A rate-limit refusal rolls the hold back with the
counters. A marked direct-invocation hold left by a crash keeps counting after its TTL. Any other
error after the provider request settles the hold in process (issue #91): `settled_unknown` with
one unknown-usage row of the call's scope, or at the recorded usage once that row is committed."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.model_providers import FakeProvider
from shifde.core.ids import uuid7
from shifde.ledger import budgeting
from shifde.models_admin import limiter
from tests.conftest import CLEAN_TABLES
from tests.connections.routes import as_user, org, post
from tests.ledger.util import add_budget
from tests.models_admin.conftest import start_of_window
from tests.models_admin.routes import fake_model_stack, invoke_body
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

for _table in ("budgets", "budget_reservations", "quotas"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


def _holds(o: Session, tenant: UUID) -> list[tuple[Any, ...]]:  # Any: row values
    o.rollback()
    rows = o.execute(
        text(
            "SELECT run_id, invocation_id IS NOT NULL, status, settled_amount, budget_ids, "
            "provider_request_at IS NOT NULL FROM shifde.budget_reservations "
            "WHERE tenant_id = :t ORDER BY id"
        ),
        {"t": tenant},
    ).all()
    return [tuple(r) for r in rows]


def _budget(o: Session, tenant: UUID, amount: str = "100000") -> UUID:
    budget = add_budget(o, tenant, amount=amount)
    o.commit()
    return budget


def test_an_answered_invoke_settles_its_hold_at_the_recorded_usage(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    budget = _budget(owner_session, world.a.tenant)
    out = post(client, f"{A}/models/{ids['model']}/invoke", h, invoke_body("hello there"), 200)
    tokens = owner_session.execute(
        text(
            "SELECT input_tokens + output_tokens FROM shifde.usage_events "
            "WHERE tenant_id = :t AND event_type = 'model_call'"
        ),
        {"t": world.a.tenant},
    ).scalar_one()
    assert tokens == out["usage"]["input_tokens"] + out["usage"]["output_tokens"]
    assert _holds(owner_session, world.a.tenant) == [
        (None, True, "settled", tokens, [budget], True)
    ]
    assert _holds(owner_session, world.b.tenant) == []


def test_without_a_budget_or_quota_an_invoke_places_no_hold(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    post(client, f"{A}/models/{ids['model']}/invoke", h, invoke_body(), 200)
    assert _holds(owner_session, world.a.tenant) == []


def test_a_failed_provider_call_settles_its_hold_at_zero(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    budget = _budget(owner_session, world.a.tenant)
    r = client.post(f"{A}/models/{ids['model']}/invoke", json=invoke_body("__fail__"), headers=h)
    assert r.status_code >= 500, r.text
    assert _holds(owner_session, world.a.tenant) == [(None, True, "settled", 0, [budget], True)]


def test_a_rate_limited_invoke_leaves_no_hold(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A, rate_limits={"rpm": 1})
    budget = _budget(owner_session, world.a.tenant)
    url = f"{A}/models/{ids['model']}/invoke"
    start_of_window(owner_session)  # both calls must share one per-minute window
    assert client.post(url, json=invoke_body(), headers=h).status_code == 200
    limited = client.post(url, json=invoke_body(), headers=h)
    assert limited.status_code == 429 and limited.json()["code"] == "model_rate_limited"
    assert [r[2] for r in _holds(owner_session, world.a.tenant)] == ["settled"]  # the first only
    assert _holds(owner_session, world.a.tenant)[0][4] == [budget]


def test_a_marked_direct_hold_left_by_a_crash_keeps_counting_after_its_ttl(
    client: TestClient, world: World, people: People, owner_session: Session
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    budget = _budget(owner_session, world.a.tenant, amount="100")
    stale = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.budget_reservations (id, tenant_id, invocation_id, step_key, "
            "budget_ids, amount, expires_at, provider_request_at) VALUES (:i, :t, :inv, "
            "'invoke', :b, 95, now() - interval '1 minute', now() - interval '11 minutes')"
        ),
        {"i": stale, "t": world.a.tenant, "inv": uuid7(), "b": [budget]},
    )
    owner_session.commit()
    url = f"{A}/models/{ids['model']}/invoke"
    denied = client.post(url, json=invoke_body("hi", 8), headers=h)
    assert denied.status_code == 403 and denied.json()["code"] == "budget_denied", denied.text
    assert _holds(owner_session, world.a.tenant)[0][2] == "held"  # not expired by the sweep
    owner_session.execute(
        text("UPDATE shifde.budget_reservations SET status = 'released' WHERE id = :i"),
        {"i": stale},
    )
    owner_session.commit()
    assert client.post(url, json=invoke_body("hi", 8), headers=h).status_code == 200


def _workspace_slug(o: Session, ws: UUID) -> str:
    return str(
        o.execute(text("SELECT slug FROM shifde.workspaces WHERE id = :w"), {"w": ws}).scalar_one()
    )


def _invoke_raising(client: TestClient, url: str, h: dict[str, str], ws: str) -> None:
    """The invoke does not succeed: the unexpected error reaches the client (or a 500)."""
    try:
        r = client.post(url, params={"workspace": ws}, json=invoke_body("hello"), headers=h)
    except RuntimeError:
        return
    assert r.status_code >= 500, r.text


def test_an_unexpected_provider_error_settles_the_hold_as_unknown_usage_of_the_call(
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    o = owner_session
    budget = add_budget(o, world.a.tenant, scope_type="workspace", scope_id=world.a.ws)
    o.commit()

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("the provider client broke")

    monkeypatch.setattr(FakeProvider, "generate", boom)
    _invoke_raising(client, f"{A}/models/{ids['model']}/invoke", h, _workspace_slug(o, world.a.ws))
    o.rollback()
    hold, amount, status, settled = o.execute(
        text(
            "SELECT id, amount, status, settled_amount FROM shifde.budget_reservations "
            "WHERE tenant_id = :t"
        ),
        {"t": world.a.tenant},
    ).one()
    assert (status, settled) == ("settled_unknown", amount)
    rows = o.execute(
        text(
            "SELECT event_key, status, input_tokens, output_tokens, provider_reported_usage, "
            "workspace_id, project_id, model_id FROM shifde.usage_events WHERE tenant_id = :t"
        ),
        {"t": world.a.tenant},
    ).all()
    assert [tuple(r) for r in rows] == [
        (
            f"reservation_unknown:{hold}",
            "failed",
            amount,
            0,
            {"estimated": True, "usage_unknown": True},
            world.a.ws,
            None,
            UUID(ids["model"]),
        )
    ]
    rule = {"id": budget, "scope_type": "workspace", "scope_id": world.a.ws, "period": "daily"}
    since = budgeting.period_start("daily")
    assert budgeting.used_and_held(o, world.a.tenant, None, rule, metric="tokens", since=since) == (
        amount,
        0,
    )
    o.rollback()


def test_an_error_after_the_durable_usage_row_settles_the_hold_at_that_usage_once(
    client: TestClient,
    world: World,
    people: People,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    ids = fake_model_stack(client, h, A)
    budget = _budget(owner_session, world.a.tenant)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("the limiter broke after the answer")

    monkeypatch.setattr(limiter, "add_tokens", boom)
    _invoke_raising(
        client, f"{A}/models/{ids['model']}/invoke", h, _workspace_slug(owner_session, world.a.ws)
    )
    owner_session.rollback()
    rows = owner_session.execute(
        text(
            "SELECT event_key, status, input_tokens + output_tokens FROM shifde.usage_events "
            "WHERE tenant_id = :t"
        ),
        {"t": world.a.tenant},
    ).all()
    assert len(rows) == 1 and rows[0][0].startswith("invoke:") and rows[0][1] == "succeeded"
    assert _holds(owner_session, world.a.tenant) == [
        (None, True, "settled", rows[0][2], [budget], True)
    ]
