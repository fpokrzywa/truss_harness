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
- Backend `backend/`: Python 3.13 (uv), FastAPI, SQLAlchemy 2 sync + psycopg 3, Alembic, Pydantic v2, pytest + pytest-xdist. Package `shifde`, env prefix `SHIFDE_*`, commands `shifde-api`, `shifde-worker`, `shifde-admin`.
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
- Token usage: `python scripts/usage_report.py`.
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
