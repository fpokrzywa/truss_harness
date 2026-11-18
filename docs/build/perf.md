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
