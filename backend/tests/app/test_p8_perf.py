"""SETH 3ba0e06 `tests/test_p8_perf.py` (NFR-PERF-003): cost and usage dashboards answer within
3 s over 1M usage events per tenant. The one test; assertions unchanged.

The test bulk-loads one million events (30 days, 20 agents, 5 models, 5 users, 20 000 runs) as the
database owner, builds the rollups with the production `rebuild`, then times the dashboard queries
through the API. The measured numbers are appended to `.devdata/p8-perf.txt` at the repository
root (SETH's location; this file is one directory deeper, hence `parents[3]`)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.clock import utcnow
from tests._support.fullapp import World
from tests._support.p2util import login, tenant_id
from tests._support.p3util import ADMIN, A

EVENTS = 1_000_000
CHUNK = 250_000
LIMIT_SECONDS = 3.0
REPORT = Path(__file__).resolve().parents[3] / ".devdata" / "p8-perf.txt"

INSERT = """
INSERT INTO shifde.usage_events (id, tenant_id, occurred_at, event_key, event_type, status,
    workspace_id, project_id, principal_id, run_id, root_run_id, attempt, agent_id, model_id,
    provider_key, model_key, is_test, input_tokens, output_tokens, call_count, duration_ms,
    cost_kind, cost_amount, currency)
SELECT gen_random_uuid(), :t, now() - ((g % (30 * 86400)) * interval '1 second'),
    'perf-' || g, 'model_call', 'succeeded', :ws, :pj, md5('user' || (g % 5))::uuid,
    md5('run' || (g / 50))::uuid, md5('run' || (g / 50))::uuid, 1, md5('agent' || (g % 20))::uuid,
    md5('model' || (g % 5))::uuid, 'fake', 'perf-model', false, 100 + g % 50, 20 + g % 30, 1,
    100 + g % 900, 'calculated', ((100 + g % 50) * 0.001 + (20 + g % 30) * 0.002)::numeric(24, 10),
    'XTS'
FROM generate_series(:a, :b) g
"""


def _timed(client: TestClient, h: dict[str, str], url: str) -> tuple[float, dict[str, Any]]:
    started = time.perf_counter()
    r = client.get(f"{A}{url}", headers=h)
    elapsed = time.perf_counter() - started
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return elapsed, body


def test_dashboards_respond_within_three_seconds_over_a_million_events(
    client: TestClient, world: World, owner_session: Session
) -> None:
    tid = tenant_id(owner_session, "demo-a")
    ws, pj = owner_session.execute(
        text(
            "SELECT workspace_id, id FROM shifde.projects WHERE slug = 'sample' AND tenant_id = :t"
        ),
        {"t": tid},
    ).one()
    loaded = time.perf_counter()
    for start in range(1, EVENTS + 1, CHUNK):
        owner_session.execute(
            text(INSERT), {"t": tid, "ws": ws, "pj": pj, "a": start, "b": start + CHUNK - 1}
        )
        owner_session.commit()
    load_s = time.perf_counter() - loaded
    from_day = (utcnow() - __import__("datetime").timedelta(days=31)).date()
    to_day = utcnow().date()
    rebuilt = time.perf_counter()
    rec = ledger.rebuild(owner_session, tid, from_day, to_day)
    owner_session.commit()
    rebuild_s = time.perf_counter() - rebuilt
    assert rec.consistent and rec.events_sum > 0
    owner_session.execute(text("ANALYZE shifde.usage_events"))
    owner_session.execute(text("ANALYZE shifde.usage_rollups_daily"))
    owner_session.commit()

    h = login(client, world.passwords, ADMIN)
    results: dict[str, float] = {}
    colds: dict[str, float] = {}
    for name, url in (
        ("summary by agent", "/usage/summary?group_by=agent"),
        ("summary by model", "/usage/summary?group_by=model"),
        ("summary by user", "/usage/summary?group_by=user"),
        ("summary by project", "/usage/summary?group_by=project"),
        ("summary by run (top 500)", "/usage/summary?group_by=run"),
        (
            "summary by agent, cost_kind filter",
            "/usage/summary?group_by=agent&cost_kind=calculated",
        ),
        ("events page (200)", "/usage/events?limit=200"),
        ("events of one run", f"/usage/events?run_id={_run_id(owner_session, tid)}"),
        ("governance overview", "/governance/overview"),
    ):
        # the first call is cold (plans, page cache); the 3 s target is judged on the second
        # (warm) call, the first is recorded as well
        cold, body = _timed(client, h, url)
        warm, _ = _timed(client, h, url)
        results[name] = warm
        colds[name] = cold
        if name.startswith("summary"):
            assert body["reconciliation"]["consistent"] is True, name
    totals = _timed(client, h, "/usage/summary?group_by=model")[1]["totals"]
    assert totals["calls"] == EVENTS and totals["cost"]["calculated"] > 0

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{utcnow().isoformat()} NFR-PERF-003 events={EVENTS} load_s={load_s:.1f} "
        f"rebuild_s={rebuild_s:.2f} limit_s={LIMIT_SECONDS}"
    ] + [f"  {name}: warm {secs:.3f} s, cold {colds[name]:.3f} s" for name, secs in results.items()]
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    slow = {n: s for n, s in results.items() if s >= LIMIT_SECONDS}
    assert not slow, f"slower than {LIMIT_SECONDS}s: {slow}"


def _run_id(owner: Session, tid: Any) -> str:
    return str(
        owner.execute(
            text("SELECT run_id FROM shifde.usage_events WHERE tenant_id = :t LIMIT 1"), {"t": tid}
        ).scalar_one()
    )
