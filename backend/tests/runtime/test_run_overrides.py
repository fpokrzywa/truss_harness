"""Run overrides (issue #80, lead decisions B-2706 and B-2707): a run started by
`RunService.create(..., model_overrides=, max_cost=)` (an evaluation's case run) stores them in
its input payload (`overrides`); `steps.call_model` calls the override of every overridden model
of the chain, for the run and for its child runs (read through `root_run_id`), and refuses a
call that would take the run family past the run's `max_cost` (`budget_denied`). A run without
overrides calls its spec's models and has no family cap, as before.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.adapters.base import Usage
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime import state, steps
from tests.conftest import CLEAN_TABLES
from tests.runtime.conftest import app_session
from tests.runtime.execution import Seed, run_context, run_row
from tests.runtime.test_steps import MESSAGES, Extra, _fake_model, opened, seeds, world

__all__ = ["opened", "seeds", "world"]  # fixtures shared with the RT-6 tests

for _table in ("budgets", "budget_reservations", "usage_events", "usage_rollups_daily"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


def _no_calls(_text: str) -> None:
    return None


def _override(owner_db: Database, tenant: UUID, run: UUID, overrides: dict[str, Any]) -> None:
    """Give `run` an input payload with `overrides`, as `RunService.create` stores it."""
    with owner_db.session() as o:
        ref = repo.insert_artifact(o, tenant, run, "input", {"input": "x", "overrides": overrides})
        o.execute(text("UPDATE shifde.runs SET input_ref = :i WHERE id = :r"), {"i": ref, "r": run})
        o.commit()


def test_an_overridden_model_is_called_by_the_run_and_its_children(
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    _override(
        owner_db,
        a.tenant,
        a.running,
        {"models": {str(xa.unpriced): str(xa.model)}, "max_cost": None},
    )
    with owner_db.session() as o:  # the child of `running` executes under the same lease
        o.execute(
            text(
                "UPDATE shifde.runs SET status = 'running', lease_token = :k, started_at = now(), "
                "state_version = state_version + 1, initiated_by_principal_id = :u "
                "WHERE id = :r"
            ),
            {"k": a.token, "r": a.child, "u": a.user},
        )
        o.commit()
    # the root run, and its child, which reads its root's overrides
    for run in (a.running, a.child):
        calls: list[dict[str, Any]] = []
        with app_session(app_db, a.tenant) as s:
            ctx = run_context(s, settings, run_row(s, a.tenant, run), db=app_db)
            _fake_model(monkeypatch, ctx, calls, Usage(input_tokens=1, output_tokens=1))
            assert steps._run_overrides(ctx).models == {xa.unpriced: xa.model}
            steps.call_model(ctx, list(MESSAGES), [xa.unpriced], "m", [], _no_calls)
        assert [c["model_id"] for c in calls] == [xa.model], run


def test_a_run_without_overrides_calls_its_own_models(
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    calls: list[dict[str, Any]] = []
    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running), db=app_db)
        _fake_model(monkeypatch, ctx, calls, Usage(input_tokens=1, output_tokens=1))
        assert steps._run_overrides(ctx) == steps.NO_OVERRIDES
        steps.call_model(ctx, list(MESSAGES), [xa.unpriced], "m", [], _no_calls)
    assert [c["model_id"] for c in calls] == [xa.unpriced]


def test_the_run_cost_cap_refuses_a_call_over_it(
    world: tuple[Seed, Seed, Extra, Extra],
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The priced model's worst case (1000 per million tokens, the whole output ceiling) is far
    above a cap of 0.0001: the call is refused before the provider is reached."""
    a, _, xa, _ = world
    _override(owner_db, a.tenant, a.running, {"models": {}, "max_cost": "0.0001"})
    calls: list[dict[str, Any]] = []
    with app_session(app_db, a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running), db=app_db)
        _fake_model(monkeypatch, ctx, calls, Usage(input_tokens=1, output_tokens=1))
        with pytest.raises(state.RunFailed) as refused:
            steps.call_model(ctx, list(MESSAGES), [xa.model], "m", [], _no_calls)
    assert refused.value.error_class == state.BUDGET_DENIED
    assert "max_cost (0.0001)" in str(refused.value)
    assert calls == []
