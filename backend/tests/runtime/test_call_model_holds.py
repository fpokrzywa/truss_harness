"""`steps.call_model` and its budget hold (issue #82, design 12.2).

The provider-request marker is set on the attempt's hold in the transaction that inserts its
`running` step row, before `invoke_for_run`: an attempt whose worker dies inside the call leaves
the hold `held`, marked and counting. The next worker's resume of that step (the attempt is
marked failed) charges the hold as `settled_unknown` with its unknown-usage row in the same
transaction; the new attempt is held and settled as usual.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.adapters.base import Usage
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.models_admin import schemas as ms
from shifde.models_admin.service import RunInvoke
from shifde.runtime import repository as repo
from shifde.runtime import steps
from tests.conftest import CLEAN_TABLES
from tests.ledger.util import add_budget
from tests.runtime.conftest import app_session
from tests.runtime.execution import Seed, run_context, run_row
from tests.runtime.test_steps import MESSAGES, Extra, opened, seeds, world

__all__ = ["opened", "seeds", "world"]  # fixtures shared with the RT-6 tests

for _table in ("budgets", "budget_reservations", "usage_events", "usage_rollups_daily"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


class _WorkerDied(BaseException):
    """The process dies inside the provider call: nothing below `call_model` handles it."""


def _holds(owner_db: Database, run: UUID) -> list[tuple[Any, ...]]:  # Any: row values
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT id, step_key, status, provider_request_at IS NOT NULL, amount, "
                "settled_amount FROM shifde.budget_reservations WHERE run_id = :r ORDER BY id"
            ),
            {"r": run},
        ).all()
    return [tuple(r) for r in rows]


def _answer(
    monkeypatch: pytest.MonkeyPatch, ctx: Any, owner_db: Database, seen: list[Any]
) -> None:  # Any: run context
    """`invoke_for_run` that records the hold as committed when the provider is called."""

    def invoke_for_run(ref: object, model_id: UUID, body: ms.InvokeIn, **kw: Any) -> RunInvoke:
        seen.append(_holds(owner_db, ctx.run_id))
        out = ms.InvokeOut(output="ok", usage=ms.UsageOut(input_tokens=2, output_tokens=3),
                           latency_ms=1)  # fmt: skip
        assert body.max_tokens is not None
        return RunInvoke(out, Usage(input_tokens=2, output_tokens=3), body.max_tokens)

    monkeypatch.setattr(ctx.artifacts.models, "invoke_for_run", invoke_for_run)


def _no_calls(_text: str) -> None:
    return None


def test_the_marker_is_committed_with_the_running_step_before_the_provider_call(
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    with owner_db.session() as o:
        add_budget(o, a.tenant, amount="1000000")
        o.commit()
    seen: list[Any] = []
    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running), db=app_db)
        _answer(monkeypatch, ctx, owner_db, seen)
        steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls)
    [[(hold, key, status, marked, _, _)]] = seen
    assert (key, status, marked) == ("m:1", "held", True)
    assert _holds(owner_db, a.running) == [(hold, "m:1", "settled", True, seen[0][0][4], 5)]


def test_a_worker_lost_inside_the_call_is_charged_in_full_when_the_step_resumes(
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b, xa, _ = world
    with owner_db.session() as o:
        add_budget(o, a.tenant, amount="1000000")
        o.commit()

    def die(*_args: Any, **_kw: Any) -> RunInvoke:
        raise _WorkerDied

    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running), db=app_db)
        monkeypatch.setattr(ctx.artifacts.models, "invoke_for_run", die)
        with pytest.raises(_WorkerDied):
            steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls)
    [(lost, _, status, marked, amount, _)] = _holds(owner_db, a.running)
    assert (status, marked) == ("held", True)  # no settle, no release
    with app_session(app_db, a.tenant) as s:  # the next worker resumes the step
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running), db=app_db)
        prior = [
            r
            for r in repo.list_steps(s, a.tenant, a.running)
            if r["kind"] == "model_call" and r["name"] == "m"
        ]
        assert [r["status"] for r in prior] == ["running"]
        _answer(monkeypatch, ctx, owner_db, [])
        steps.call_model(ctx, list(MESSAGES), [xa.model], "m", prior, _no_calls)
    holds = _holds(owner_db, a.running)
    assert [(h[1], h[2], h[5]) for h in holds] == [
        ("m:1", "settled_unknown", amount),
        ("m:2", "settled", 5),
    ]
    with owner_db.session() as o:
        unknown = o.execute(
            text(
                "SELECT input_tokens, output_tokens, model_id, provider_reported_usage "
                "FROM shifde.usage_events WHERE event_key = :k"
            ),
            {"k": f"reservation_unknown:{lost}"},
        ).all()
        b_usage = o.execute(
            text("SELECT count(*) FROM shifde.usage_events WHERE tenant_id = :t"), {"t": b.tenant}
        ).scalar_one()
    assert [tuple(r) for r in unknown] == [
        (amount, 0, xa.model, {"estimated": True, "usage_unknown": True})
    ]
    assert b_usage == 0
