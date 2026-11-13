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
- FDE SDK: `C:\Users\FreddiePokrzywa\OneDrive - 3C Publish LLC\Documents\ChatGPT\SHIFDE SDK`, commit `1fd902b`.
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
- Token usage: `python scripts/usage_report.py`.
- SETH schema snapshot (only if SETH's reference commit changes): `cd backend; uv run python ../scripts/make_seth_schema_snapshot.py` (cluster running) rewrites `backend/tests/schema/seth_3ba0e06.json`.

## Fresh checkout or worktree
- Worktrees live in `.worktrees/<name>` (gitignored). The local cluster runs from the main checkout; a worktree needs its own `.devdata/dev.env`: run `python scripts/dev_db.py env` from the worktree root (the script finds the main checkout through git).
- `cd backend; $env:UV_LINK_MODE='copy'; uv sync --frozen`; `cd frontend; pnpm install --frozen-lockfile`.

## Validation (the gate set)
- `python -I scripts/validate/scan-secrets.py`
- `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports; uv run pytest -n auto`
- `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`
- `python -m unittest discover -s scripts/tests`
- All of the above from a clean checkout of a ref, one line per gate: `python scripts/gates.py --ref <branch|commit> [--tests tests/<module> ...] [--no-frontend]` (worktree `.worktrees/verify`, logs in `.worktrees/_gate-logs/`; cluster must be running). `pytest -n 8` is faster than `-n auto` on this 96-core machine.
