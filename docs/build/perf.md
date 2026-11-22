# API latency (F-16)

Issue #13, findings Item 22 (REQ F-16). Decisions B-2633..B-2638.

## Method

- The harness is `backend/tests/perf/test_api_latency.py::test_main_routes_p95_within_budget`, with the measurement helper in `backend/tests/perf/latency.py`.
- Requests go through the real app in-process (`TestClient` over `create_app`) as `shifde_app`, signed in as `admin@demo-a.local`.
- Each run gets a fresh database cloned from the migrated template and seeded with the demo world (the `tests/app` fixtures, imported by `backend/tests/perf/conftest.py`).
- Setup builds a fake-model `Stack` (scripted fake model connection, prompt, builtin tool, published agent) and starts one run, so the single-run read has an id.
- Every route is called 3 times untimed (warm-up), then timed `N` times with `time.perf_counter()`. Each call must answer 200 (reads) or 202 (run start).
- p50 and p95 use the nearest-rank method: the sample at rank `ceil(p/100 * N)` of the sorted samples.
- No worker runs, so started runs stay `pending` and no model or vendor is called.
- `N` is 20 by default. `SHIFDE_PERF_ITERATIONS` (a positive integer) overrides it; any other value fails the test.
- Each run prints its table (visible with `-s`) and appends a timestamped block to `.devdata/f16-latency.txt` at the repository root.

## Routes and budgets

All routes are under `/api/v1/orgs/demo-a`.

| Route | Expected status | p95 budget |
|---|---|---|
| `GET /runs?limit=50` | 200 | 2000 ms |
| `GET /runs/{run_id}` | 200 | 2000 ms |
| `GET /agents` | 200 | 2000 ms |
| `GET /agents/{agent_id}` | 200 | 2000 ms |
| `GET /prompts` | 200 | 2000 ms |
| `GET /connections` | 200 | 2000 ms |
| `POST /runs` (run start, `agent` kind, no `Idempotency-Key`) | 202 | 3000 ms |

The test fails if any route's p95 exceeds its budget and names every such route with its p95. The budgets are deliberately generous so the test does not flake beside the full suite under `-n 8`.

## Measurement command

Cluster running:

```powershell
cd backend; $env:SHIFDE_PERF_ITERATIONS='200'; uv run pytest tests/perf/test_api_latency.py -q -s
```

```bash
cd backend && SHIFDE_PERF_ITERATIONS=200 uv run pytest tests/perf/test_api_latency.py -q -s
```

Without the variable, `uv run pytest tests/perf -q` runs the same test with 20 iterations (as `factory/checks.sh` does when the file changes).

## Measured

2026-11-18, base commit `dae6953`, local cluster (PostgreSQL 17, port 55450), Windows 11, 200 iterations, warm-up 3:

| Route | Iterations | p50 ms | p95 ms | Budget ms |
|---|---|---|---|---|
| `GET /runs?limit=50` | 200 | 43.7 | 60.5 | 2000 |
| `GET /runs/{run_id}` | 200 | 38.6 | 55.8 | 2000 |
| `GET /agents` | 200 | 36.9 | 50.5 | 2000 |
| `GET /agents/{agent_id}` | 200 | 36.4 | 52.2 | 2000 |
| `GET /prompts` | 200 | 37.3 | 51.8 | 2000 |
| `GET /connections` | 200 | 32.6 | 47.6 | 2000 |
| `POST /runs` | 200 | 64.0 | 89.4 | 3000 |

## Not measured

- Pipeline or worker throughput, run completion time and model-call time (no worker runs).
- Concurrency, load and multiple clients (calls are sequential from one client).
- Percentiles other than p50 and p95, and network overhead (the client is in-process).

# Dashboards over a million events (NFR-PERF-003)

Issue #109. Decision B-2770.

## Method

- The test is `backend/tests/app/test_p8_perf.py::test_dashboards_respond_within_three_seconds_over_a_million_events`, ported from SETH 3ba0e06.
- It bulk-loads 1,000,000 `usage_events` for `demo-a` (30 days, 20 agents, 5 models, 5 users, 20,000 runs), builds the rollups with the production `ledger.rebuild`, and runs `ANALYZE` on the events and rollup tables.
- Each of 9 dashboard URLs (6 `usage/summary` groupings, 2 `usage/events` pages, `governance/overview`) gets one cold call, recorded but not judged, then one warm call. Each warm call must finish in under 3 s (`LIMIT_SECONDS`). The test also checks status 200, reconciliation of every summary, and the model totals (`calls == 1,000,000`, calculated cost above 0).
- Each run appends a timestamped block to `.devdata/p8-perf.txt` at the repository root, with the warm and cold time of every dashboard.

## Running alone under xdist

- The timed calls and the totals check run inside `running_alone` from `backend/tests/conftest.py`.
- Every test marks its xdist worker busy while it runs. `running_alone` takes the run's lock, so no other worker can start a test, and it waits until the tests other workers are already running have finished. Only then does it time the calls. The bulk load and the rebuild still run next to the other workers.
- Why: a wall-clock budget measured next to seven other workers on the shared cluster measured their load, not the dashboards. In the `041-merge` full suite the warm calls took about 1.3 s on the server side, but one warm `summary by project` measured 4.89 s and failed the test. The budget, the data volume and what the test asserts are the same as before. A dashboard that really is slow still fails.
- Cost: the other workers sit idle while the test waits and times its calls (about 30 s, plus the rest of the longest test that is running at that moment). Without xdist, `running_alone` does nothing.

## Measurement command

Cluster running:

```bash
cd backend && uv run pytest tests/app/test_p8_perf.py -q
```

## Measured

2026-11-22, `-n 8 tests/adapters tests/app` on the job 067 branch: every warm call took between 0.035 s and 1.6 s, the summaries about 1.25-1.6 s.
