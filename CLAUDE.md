# SETH unified platform

One application (SETH) merging SETH, the FDE SDK and Agent Factory into one codebase and one PostgreSQL database.

## Read first
1. `docs/build/STATUS.md` — where the build is. Read it after every compaction or resume.
2. `docs/build/CHARTER.md` — standing instructions for the build (authority, agents, gates, cost).
3. `docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md` — the approved design (what to build).
4. `docs/superpowers/plans/2026-11-13-subproject-1-foundation-and-seth-port.md` — the sub-project 1 plan.
5. `docs/build/DECISIONS.md` — builder decisions.

## Source repositories (read-only, never modify)
- SETH: `S:\Development_2026\shi_work\shi_enterprise_truss_harness`, commit `3ba0e06`. Read with `git -C <path> show 3ba0e06:<file>`.
- FDE SDK: `%USERPROFILE%\OneDrive - 3C Publish LLC\Documents\ChatGPT\SHIFDE SDK`, commit `1fd902b`.
- Agent Factory: `S:\Development_2026\the_agent_factory`, commit `0fdd09c` (never open its `.env` or `azure_key.txt`).

## Stack
- Backend `backend/`: Python 3.13 (uv), FastAPI, SQLAlchemy 2 sync + psycopg 3, Alembic, Pydantic v2, pytest + pytest-xdist. Package `shifde`, env prefix `SHIFDE_*`, commands `shifde-api`, `shifde-worker`, `shifde-admin`, `shifde-executor`.
- Database: PostgreSQL 17, forced row-level security on every client-owned table. App role `shifde_app` (NOSUPERUSER NOBYPASSRLS, owns nothing); schema owner `shifde_owner`. The app refuses to start under a privileged role.
- Frontend `frontend/`: Next.js App Router, React 19, TypeScript, Tailwind, pnpm, Vitest.

## Windows notes
- `UV_LINK_MODE=copy` for uv; `python`, never `python3`; `git config core.longpaths true`.

## Secrets
- `.env.local` (hosted DB) and `.devdata/` (local cluster, dev env) are gitignored. Never print, log, commit or pass their contents to an agent.

## Local commands
- Local database (port 55450): `python scripts/dev_db.py init` once, then `python scripts/dev_db.py start|stop|status|env`. One cluster for every checkout: data and secrets in the main checkout's `.devdata/` (`pg/`, `cluster.env`); `start`/`env` write this checkout's `.devdata/dev.env` and database (`shifde`, or `shifde_<worktree>` in a worktree).
- Python 3.13 for the backend: `uv python install 3.13` if `uv sync` cannot find one.
- Backend: `cd backend; $env:UV_LINK_MODE='copy'; uv sync` then `uv run pytest -n auto` (all) or `uv run pytest tests/<module>` (one module).
- Frontend: `cd frontend; pnpm install; pnpm dev`.
- Browser end-to-end suite (Playwright, Chromium): cluster running, `cd frontend; pnpm install --frozen-lockfile`, once `pnpm exec playwright install chromium`; then `cd frontend; pnpm e2e`. It prepares this checkout's database (`dev_db.py env`, `db upgrade`, `dev-seed`, keeping existing passwords), then starts the API on 8780 and Next on 3780 and signs in as `admin@demo-a.local`. Local only: not part of `factory/checks.sh` or CI.
- API latency (F-16, cluster running): `cd backend; $env:SHIFDE_PERF_ITERATIONS='200'; uv run pytest tests/perf/test_api_latency.py -q -s` prints p50/p95 per main route and appends them to `.devdata/f16-latency.txt`; method, budgets and measured numbers in `docs/build/perf.md`.
- Executor against the fake platform (API running): set `SHIFDE_EXECUTOR_API_URL` (the API origin), `SHIFDE_EXECUTOR_TOKEN` (an `shx_` credential from the Executors screen), `SHIFDE_EXECUTOR_CREDENTIALS_FILE` (JSON `credential_ref -> {tenant_id, host, token, service_principal}`; the file belongs to one organization and every entry carries that organization's id as `tenant_id`) and `SHIFDE_ENV=development` (optionally `SHIFDE_EXECUTOR_HEALTH_PORT` for `/healthz` and `/readyz`, and `SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS`, default 60), then `cd backend; uv run shifde-executor --fake-platform`.
- Databricks live test (opt-in, owner only): needs `.devdata/databricks.env`, then `cd backend; uv run pytest tests/adapters/native -m live`; skipped otherwise. Steps, env file keys and cleanup in `docs/runbooks/databricks-live-validation.md`.
- Entra live test (opt-in, owner only): needs `.devdata/entra.env` (`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`), then `cd backend; uv run pytest tests/identity/test_entra_live.py -m live`; skipped otherwise. App registration, IdP setup and the manual browser sign-in in `docs/runbooks/entra-live-validation.md`.
- Delivery demo against the fake platform (cluster running): `python scripts/delivery_demo.py --fake` seals a release, shows a plan of it refused (`evaluation_required`), runs a real evaluation of it to `passed`, then runs plan, approval, apply, observe and verify in-process on a scratch database and ends with `DELIVERY DEMO PASSED`.
- Release demo against the fake platform and the fake Entra issuer (cluster running): `python scripts/release_demo.py --fake` seals a release whose plan is refused (`evaluation_required`), shows a second release's failing evaluation still refused, runs a passing evaluation and an accepted plan, a second person's approval, apply and verify on the fake platform, finds a settled budget reservation of an evaluation case run, then signs an Entra user of a group bound only in `demo-a` into `demo-a` (404 on `demo-b`) and refuses a group-overage sign-in, in-process on a scratch database, and ends with `RELEASE DEMO PASSED`.
- Reference process supervisor (cluster running): `python scripts/procctl.py up --env-file F [--with-executor] [--with-web]` starts and supervises the API, worker, executor and web, `python scripts/procctl.py down` stops it and `python scripts/procctl.py status` prints liveness and readiness; exit codes, restart policy and env file in `docs/runbooks/process-control.md`.
- Token usage: `python scripts/usage_report.py`.
- Traceability matrix (design 5.3 -> tests): `python scripts/traceability.py` writes `docs/requirements/TRACEABILITY.md`; `python scripts/traceability.py --check` fails on a missing test file or a stale matrix.
- Go-live preflight: `cd backend; uv run shifde-admin config preflight [--env-file F] [--register PATH]` validates the configuration, then prints one `ok:`/`refused:` line per go-live check (trusted proxies, HSTS, API docs, local login, metrics token, database role, migrations, register) and `pending: <id>` per `pending-owner` row of `docs/build/golive-register.md`, never a value; exit 0 only when no line is `refused:`.
- Retention purge (content scrub of old, ended runs, as the app role): `cd backend; uv run shifde-admin maintenance purge --older-than <days> [--dry-run]`.
- SETH schema snapshot (only if SETH's reference commit changes): `cd backend; uv run python ../scripts/make_seth_schema_snapshot.py` (cluster running) rewrites `backend/tests/schema/seth_3ba0e06.json`.

## Fresh checkout or worktree
- Worktrees live in `.worktrees/<name>` (gitignored). The local cluster runs from the main checkout; a worktree needs its own `.devdata/dev.env`: run `python scripts/dev_db.py env` from the worktree root (the script finds the main checkout through git).
- `cd backend; $env:UV_LINK_MODE='copy'; uv sync --frozen`; `cd frontend; pnpm install --frozen-lockfile`.

## Validation (the gate set)
- `python -I scripts/validate/scan-secrets.py`
- `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports; uv run pytest -n auto`
- SAST: `cd backend; uv run bandit -q -r src --severity-level medium` (bandit pinned in `pyproject.toml`; a reviewed finding gets `# nosec <id>` with its reason, never a blanket skip).
- Coverage: `cd backend; uv run pytest -n 8 --cov --cov-report=term` runs the full suite; the floor is `fail_under` in `[tool.coverage.report]` of `backend/pyproject.toml` (CI runs it with `-n auto`).
- OpenAPI diff: `cd backend; uv run python -m tests._support.openapi_snapshot --check`. Refresh `backend/openapi.json` with `cd backend; uv run python -m tests._support.openapi_snapshot` after any route or schema change, or a version bump. These three run in CI; `gates.py` and `factory/checks.sh` do not run them.
- `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`
- `python -m unittest discover -s scripts/tests`
- All of the above from a clean checkout of a ref, one line per gate: `python scripts/gates.py --ref <branch|commit> [--tests tests/<module> ...] [--no-frontend]` (worktree `.worktrees/verify`, logs in `.worktrees/_gate-logs/`; cluster must be running). `pytest -n 8` is faster than `-n auto` on this 96-core machine.

## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through intake, spec, build, gate, review, approve, one PR per issue (concurrency 1; intake runs on sonnet). State is in `factory/jobs/` (gitignored); `factory/bin/job` is its only writer (`factory/bin/job queue` shows what `/factory run` would do). Checks: `bash factory/checks.sh` from a checkout root (cluster running): the gate set above, with backend pytest limited to a core set (row-level security schema, schema comparison with SETH, startup role check, cross-client tests) plus the backend test files the branch changed vs `origin/main`; `FACTORY_FULL_PYTEST=1` runs the whole suite (`-n 8`). The full suite stays a human step before merging. Each job's own database comes from `python scripts/dev_db.py env` (`factory/bin/jobdb ensure` also migrates and seeds it). Dashboard: `cd factory; python -m http.server`, then open http://localhost:8000/dashboard.html.

Each job works in its own git worktree at `.worktrees/<job-id>` (gitignored); exclude `.worktrees/` in any tool that does not read `.gitignore`. The factory is GPLv3 (see `factory/NOTICE.md`): keep it in `factory/` and `.claude/`, never in `backend/src` or `frontend/src`. `factory/*`, `.claude/*`, Alembic migrations and `backend/tests/schema/*` are protected paths: jobs cannot change them.
