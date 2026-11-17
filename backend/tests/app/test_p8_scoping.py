"""P8 hostile tests over worker runs: cross-tenant usage visibility and workspace-scoped usage
reports, moved from SETH 3ba0e06 `tests/test_p8_scoping.py`; assertions unchanged, imports
rewritten to `tests._support.*`.

Not here: `test_pricing_administration_needs_pricing_manage_and_platform_cards_need_platform_admin`
was moved to `tests/governance/test_p8_pricing.py` (B-1805).
"""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login, post, tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import limited_user, start_run, thread_worker, user_session, wait_run
from tests._support.p8util import events_of, priced_stack


def test_another_tenant_sees_none_of_this_tenants_usage_runs_or_rate_cards(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        assert wait_run(client, h, run["id"])["status"] == "completed"
    bh = user_session(client, world.passwords, ADMIN_B)
    mine = bh.client.get(f"{B}/usage/events", headers=bh).json()
    assert mine["items"] == []
    s = bh.client.get(f"{B}/usage/summary?group_by=project", headers=bh).json()
    assert s["groups"] == [] and s["totals"]["calls"] == 0 and s["reconciliation"]["consistent"]
    assert bh.client.get(f"{B}/usage/runs/{run['id']}", headers=bh).status_code == 404
    assert bh.client.get(f"{B}/usage/events?run_id={run['id']}", headers=bh).json()["items"] == []
    assert bh.client.get(f"{B}/rate-cards", headers=bh).json()["items"] == []  # A's tenant card
    cards = client.get(f"{A}/rate-cards", headers=h).json()["items"]
    assert bh.client.get(f"{B}/rate-cards/{cards[0]['id']}/versions", headers=bh).status_code == 404
    assert bh.client.post(f"{B}/usage/rollups/rebuild", headers=bh).status_code in (200, 403)
    # B's rebuild touched only B: A's rollups still reconcile with A's events
    assert client.get(f"{A}/usage/summary?group_by=agent", headers=h).json()["reconciliation"][
        "consistent"
    ]
    # exports never mix tenants
    assert run["id"] not in bh.client.get(f"{B}/usage/export", headers=bh).text


def test_usage_reports_are_limited_to_the_scopes_where_usage_read_is_held(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        wait_run(client, h, run["id"])
    tid = tenant_id(owner_session, "demo-a")
    main = owner_session.execute(
        text("SELECT id FROM shifde.workspaces WHERE slug = 'main' AND tenant_id = :t"), {"t": tid}
    ).scalar_one()
    other = UUID(post(client, f"{A}/workspaces", h, {"slug": "other", "name": "Other"})["id"])
    ledger.record(  # usage in a workspace the user cannot see
        owner_session,
        {"tenant_id": tid, "event_key": "o-1", "event_type": "tool_call", "workspace_id": other,
         "input_tokens": 777, "call_count": 1},
    )  # fmt: skip
    owner_session.commit()
    role = post(
        client, f"{A}/roles", h,
        {"key": "ws-usage", "name": "WS usage", "permissions": ["workspace:read", "usage:read"]},
    )  # fmt: skip
    uid, _ = limited_user(
        client, owner_session, h, world.passwords, "u-ws@demo-a.local", ["workspace:read"]
    )
    h = login(client, world.passwords, ADMIN)
    post(
        client, f"{A}/role-bindings", h,
        {"subject_type": "user", "subject_id": uid, "role_id": role["id"],
         "scope": {"type": "workspace", "id": str(main)}},
    )  # fmt: skip
    uh = user_session(client, world.passwords, "u-ws@demo-a.local")
    events = uh.client.get(f"{A}/usage/events?limit=200", headers=uh).json()["items"]
    assert events and {e["workspace_id"] for e in events} == {str(main)}
    summary = uh.client.get(f"{A}/usage/summary?group_by=workspace", headers=uh).json()
    assert [g["key"] for g in summary["groups"]] == [str(main)]
    assert summary["totals"]["input_tokens"] == sum(e["input_tokens"] for e in events)
    assert summary["reconciliation"]["consistent"] is True
    assert uh.client.get(f"{A}/usage/runs/{run['id']}", headers=uh).status_code == 200
    # the admin sees both workspaces
    both = client.get(
        f"{A}/usage/summary?group_by=workspace", headers=login(client, world.passwords, ADMIN)
    )
    assert {g["key"] for g in both.json()["groups"]} == {str(main), str(other)}
    # a scoped user may neither rebuild rollups nor export
    assert uh.client.post(f"{A}/usage/rollups/rebuild", headers=uh).status_code == 403
    assert uh.client.get(f"{A}/usage/export", headers=uh).status_code == 403
    assert events_of(owner_session, run["id"])  # sanity: the ledger itself still holds the run
