"""F-16: API latency of the main routes, measured in-process against a fresh seeded database.

Six read routes and the run-start route are timed through the real app (`TestClient`); p95 must
stay within the budgets below. Run start uses the fake-model `Stack` and no worker runs, so the
run stays pending and no model is called. `SHIFDE_PERF_ITERATIONS` sets the timed calls per route
(default 20; 200 is the measurement in `docs/build/perf.md`). Each run prints its table and
appends it to `.devdata/f16-latency.txt` at the repository root."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A
from tests._support.p5util import Stack, start_run
from tests.perf.latency import Latency, measure, percentile

READ_BUDGET_MS = 2000.0
RUN_START_BUDGET_MS = 3000.0
DEFAULT_ITERATIONS = 20
ITERATIONS_VAR = "SHIFDE_PERF_ITERATIONS"
REPORT = Path(__file__).resolve().parents[3] / ".devdata" / "f16-latency.txt"


def _iterations() -> int:
    raw = os.environ.get(ITERATIONS_VAR)
    if raw is None:
        return DEFAULT_ITERATIONS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        pytest.fail(f"{ITERATIONS_VAR} must be a positive integer, got {raw!r}")
    return value


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(20, 0, -1)]
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0
    assert percentile(values, 100) == 20.0
    assert percentile([7.5], 50) == 7.5
    assert percentile([7.5], 95) == 7.5
    with pytest.raises(ValueError):
        percentile([], 50)


def test_main_routes_p95_within_budget(
    client: TestClient, world: World, owner_session: Session
) -> None:
    iterations = _iterations()
    h = login(client, world.passwords, ADMIN)
    stack = Stack(client, h, owner_session)
    agent_id = stack.agent["id"]
    run_id = start_run(client, h, agent_id)["id"]
    run_body: dict[str, Any] = {
        "kind": "agent",
        "agent_id": agent_id,
        "workspace": "main",
        "project": "sample",
        "input": "hello",
    }

    def get(url: str) -> Callable[[], httpx.Response]:
        return lambda: client.get(f"{A}{url}", headers=h)

    routes: list[tuple[str, Callable[[], httpx.Response], int, float]] = [
        ("GET /runs?limit=50", get("/runs?limit=50"), 200, READ_BUDGET_MS),
        ("GET /runs/{run_id}", get(f"/runs/{run_id}"), 200, READ_BUDGET_MS),
        ("GET /agents", get("/agents"), 200, READ_BUDGET_MS),
        ("GET /agents/{agent_id}", get(f"/agents/{agent_id}"), 200, READ_BUDGET_MS),
        ("GET /prompts", get("/prompts"), 200, READ_BUDGET_MS),
        ("GET /connections", get("/connections"), 200, READ_BUDGET_MS),
        (
            "POST /runs",
            lambda: client.post(f"{A}/runs", json=run_body, headers=h),
            202,
            RUN_START_BUDGET_MS,
        ),
    ]
    results: list[tuple[str, Latency, float]] = [
        (name, measure(call, status, iterations), budget) for name, call, status, budget in routes
    ]

    lines = [f"{'route':<24} {'iterations':>10} {'p50 ms':>9} {'p95 ms':>9} {'budget ms':>10}"] + [
        f"{name:<24} {lat.iterations:>10} {lat.p50_ms:>9.1f} {lat.p95_ms:>9.1f} {budget:>10.0f}"
        for name, lat, budget in results
    ]
    print("\n" + "\n".join(lines))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(f"{utcnow().isoformat()} F-16 API latency, warm-up 3\n")
        fh.write("".join(f"  {line}\n" for line in lines))

    over = {name: round(lat.p95_ms, 1) for name, lat, budget in results if lat.p95_ms > budget}
    assert not over, f"p95 over budget (ms): {over}"


def test_board_routes_p95_within_budget(client: TestClient, world: World) -> None:
    """The organization board and one project's board (sub-project 7 design 7.2)."""
    iterations = _iterations()
    h = login(client, world.passwords, ADMIN)

    def get(url: str) -> Callable[[], httpx.Response]:
        return lambda: client.get(f"{A}{url}", headers=h)

    routes = [
        ("GET /board", get("/board")),
        (
            "GET /workspaces/main/projects/sample/board",
            get("/workspaces/main/projects/sample/board"),
        ),
    ]
    results = [(name, measure(call, 200, iterations)) for name, call in routes]

    lines = [f"{'route':<42} {'iterations':>10} {'p50 ms':>9} {'p95 ms':>9} {'budget ms':>10}"] + [
        f"{name:<42} {lat.iterations:>10} {lat.p50_ms:>9.1f} {lat.p95_ms:>9.1f} "
        f"{READ_BUDGET_MS:>10.0f}"
        for name, lat in results
    ]
    print("\n" + "\n".join(lines))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(f"{utcnow().isoformat()} F-16 API latency (board), warm-up 3\n")
        fh.write("".join(f"  {line}\n" for line in lines))

    over = {name: round(lat.p95_ms, 1) for name, lat in results if lat.p95_ms > READ_BUDGET_MS}
    assert not over, f"p95 over budget (ms): {over}"
