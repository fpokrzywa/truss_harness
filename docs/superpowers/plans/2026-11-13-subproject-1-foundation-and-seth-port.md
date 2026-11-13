# Sub-project 1: foundation and SETH port — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Today's SETH behaviour runs from this repository as one shared instance on one PostgreSQL database, with the client-isolation rules of design §5.3 proven by tests.

**Architecture:** A clean skeleton (gates, test harness, baseline migration, database roles, startup role check) is built first. SETH's modules are then moved in from `3ba0e06` bottom layer first, each with its tests (regrouped by module), its own Alembic branch, its cross-client tests and its audit fixes. Two new pieces of work land with the modules that own them: SHI-wide groups and the per-client switch for SHI's model provider.

**Tech Stack:** Python 3.13 (uv), FastAPI, SQLAlchemy 2 sync + psycopg 3, Alembic, Pydantic v2, pytest + pytest-xdist; Next.js App Router, React 19, TypeScript, Tailwind, pnpm, Vitest; PostgreSQL 17.

**Spec:** `docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md` (approved). Working rules: `docs/build/CHARTER.md`.

## Global Constraints

- Source of every ported file: SETH at `S:\Development_2026\shi_work\shi_enterprise_truss_harness`, commit `3ba0e06`. Read it with `git -C <seth> show 3ba0e06:<path>` or `git -C <seth> archive 3ba0e06`. Never write to it, nor to the SDK or Agent Factory repositories.
- Package `shifde`, environment prefix `SHIFDE_*`, commands `shifde-api`, `shifde-worker`, `shifde-admin`.
- Python `>=3.13,<3.14`; `UV_LINK_MODE=copy`; scripts call `python`, never `python3`; `git config core.longpaths true`.
- Gates (all must exit 0): `python -I scripts/validate/scan-secrets.py`; in `backend/`: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src` (strict), `uv run lint-imports`, `uv run pytest`; in `frontend/`: `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build`.
- Ruff, mypy and import-linter configuration start as SETH's `backend/pyproject.toml` at `3ba0e06` and are never loosened. New modules may only add contracts.
- Local PostgreSQL 17 cluster on port **55450** (SETH's is 55440), data in `.devdata/pg/`. Tests never touch the hosted database.
- Roles: `shifde_owner` owns the schema and runs migrations; `shifde_app` is `NOSUPERUSER NOBYPASSRLS`, owns nothing, and is what the API and worker connect as.
- Every client-owned table has `ENABLE` + `FORCE ROW LEVEL SECURITY` and a policy keyed on the per-transaction organization setting. The global-table allowlist lives in one file, `backend/tests/schema/global_tables.txt`, one table per line with a reason.
- Every migration has a working `downgrade()`.
- Never print or commit `.env.local` or `.devdata/*`.
- Each agent description starts with its unit in brackets, e.g. `[tenancy] build`.

## Review Focus

1. A user bound to client A by group requests a client-B object by id (any route, any verb) → 404 or 403, never the object and never a hint it exists. Pinned by each module's `test_cross_client.py`.
2. The app connects with a role that is superuser, owns any table in `public`, or has `BYPASSRLS` → process exits non-zero before serving. Pinned in Task 2 (`tests/core/test_startup_role_check.py`).
3. A new table is added with no RLS policy and not in `global_tables.txt` → `tests/schema/test_rls_schema.py` fails naming the table. Pinned in Task 2.
4. A worker dies mid approved write → run ends `unknown_outcome`/needs decision, never `cancelled` (C1). A connection's host is changed by someone without `secret:use` → the secret binding is not used against the new host (C2). Pinned in Tasks 13 and 17.
5. A run in a client where SHI's shared model provider is switched off tries to call it → refused before any network call, and the refusal is audited. Pinned in Task 14.

---

### Task 0: Build control files, agents and usage report (lead)

**Files:**
- Create: `CLAUDE.md`, `docs/build/STATUS.md`, `docs/build/DECISIONS.md`, `docs/build/prices.json`, `.claude/agents/{scout,module-builder,module-builder-critical,frontend-builder,verifier,isolation-reviewer}.md`, `scripts/usage_report.py`, `scripts/tests/test_usage_report.py`

**Interfaces:**
- Produces: `python scripts/usage_report.py [--projects-dir DIR] [--out-dir DIR]` → exit 0, prints tables "By model", "By agent type", "By unit", "By sub-project", "By day"; writes `docs/build/usage.json` and `docs/build/usage.html`.

- [ ] Write `scripts/tests/test_usage_report.py` (stdlib `unittest`): a fixture project dir with one session jsonl (two lines sharing one `requestId`, one distinct) and one subagent jsonl + `meta.json` (`agentType: module-builder`, `description: "[core] build"`). Assert: the shared `requestId` counts once; lead vs `module-builder` split; unit `core`; cost uses `prices.json`.
- [ ] Run `python -m unittest scripts/tests/test_usage_report.py` → FAIL (no module).
- [ ] Implement `scripts/usage_report.py` (stdlib only). Project folders: every folder in `~/.claude/projects/` whose name starts with `S--Development-2026-shi-work-agt-trus-harness`. Unit = bracket prefix of description, else `lead`/`unassigned`. Sub-project = `sp1` until `STATUS.md` says otherwise (unit→sub-project map read from `docs/build/units.json` if present). Rounds per unit = count of agents per unit and description verb (`build`, `verify`, `fix`).
- [ ] Run the tests → PASS; run `python scripts/usage_report.py` → exit 0.
- [ ] Commit.

### Task 1: Backend skeleton and gates

**Files:**
- Create: `backend/pyproject.toml` (SETH's, plus `pytest-xdist` and `python-dotenv` in dev), `backend/uv.lock`, `backend/src/shifde/{__init__,app,main}.py`, `backend/src/shifde/core/{__init__,settings,db,migrations,dbrole}.py`, `scripts/validate/scan-secrets.py` (from SETH), `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `shifde.app.create_app(settings: Settings | None = None) -> FastAPI` serving `GET /healthz → {"status":"ok"}`; `shifde.core.dbrole.check_app_role(engine: Engine) -> None` raising `UnsafeDatabaseRole(reason: str)` when the connected role is superuser, has `rolbypassrls`, or owns any relation in schema `public`; `shifde.main.run()` calls `check_app_role` before `uvicorn.run` and exits with code 3 on `UnsafeDatabaseRole`.
- import-linter: SETH's contracts verbatim; contracts naming modules that do not exist yet are kept and are satisfied vacuously (`lint-imports` must still exit 0; if it errors on missing modules, add empty `__init__.py` packages for every SETH module name — record in DECISIONS).

- [ ] Copy SETH `pyproject.toml`, `scripts/validate/scan-secrets.py`; `uv lock`; `uv sync`.
- [ ] Write `tests/core/test_startup_role_check.py`: three cases (superuser, owner-of-a-table, BYPASSRLS role) each `pytest.raises(UnsafeDatabaseRole)`; a safe `shifde_app` passes; and a subprocess test that `shifde-api` with an owner URL exits 3 without binding the port.
- [ ] Implement; tests PASS; all backend gates exit 0.
- [ ] CI workflow: Postgres 17 service, runs the gate set. Commit.

### Task 2: Local database, test harness, baseline migration, schema tests

**Files:**
- Create: `scripts/dev_db.py` (`init|start|stop|status|env`), `backend/migrations/{env.py,script.py.mako}`, `backend/migrations/versions/base_0001_baseline.py`, `backend/tests/conftest.py`, `backend/tests/schema/{global_tables.txt,deliberate_changes.yaml,seth_3ba0e06.json,test_rls_schema.py,test_schema_vs_seth.py,snapshot.py}`, `scripts/make_seth_schema_snapshot.py`

**Interfaces:**
- `dev_db.py init` creates the cluster on 55450, superuser `postgres` (password in `.devdata/dev.env`), roles `shifde_owner` (LOGIN, owns database `shifde`) and `shifde_app` (LOGIN NOSUPERUSER NOBYPASSRLS), writes `.devdata/dev.env` with `SHIFDE_DATABASE_URL` (app), `SHIFDE_MIGRATION_DATABASE_URL` (owner), `SHIFDE_TEST_ADMIN_DATABASE_URL` (superuser).
- Alembic: one `versions/` dir, `version_locations` per module not used; each module's revisions carry `branch_labels=("<module>",)`; root revision `base_0001` (branch `base`). Upgrades run `alembic upgrade heads`.
- Baseline creates: `shifde_app` default privileges, helper functions `shifde_current_org() returns uuid` (reads `current_setting('app.org_id', true)`), nothing client-owned.
- `tests/schema/snapshot.py: snapshot(conn, tables: set[str] | None) -> dict` capturing per table columns (name, type, nullable, default), constraints (pk/fk/unique/check by definition), indexes (definition), RLS enabled/forced, policies (cmd, roles, qual, with_check), grants to `shifde_app`.
- `conftest.py`: session fixture migrates a template database `shifde_tmpl_<run>` once (guarded by file lock under xdist), each xdist worker gets `CREATE DATABASE ... TEMPLATE`; fixtures `app_engine`, `owner_engine`, `client` (TestClient), per-test cleanup by truncation as owner. Port SETH's fixtures as modules arrive.
- `test_rls_schema.py`: every table in `public` except `alembic_version` and those in `global_tables.txt` has RLS enabled+forced and ≥1 policy.
- `test_schema_vs_seth.py`: for each table present in both the new DB and `seth_3ba0e06.json`, snapshots are equal after applying `deliberate_changes.yaml` (entries: `table`, `path`, `reason`, `migration`). A table listed in `docs/build/ported_tables.txt` but missing from the new DB fails.
- `make_seth_schema_snapshot.py`: `git archive 3ba0e06 backend` from SETH into a scratch dir, `uv sync` there, migrate a scratch DB on 55450 with SETH's migrations, write `seth_3ba0e06.json`, drop the scratch DB.

- [ ] Write the two schema tests and a test that a table with no policy makes `test_rls_schema` fail (create in a throwaway transaction).
- [ ] Implement; generate the SETH snapshot; all gates exit 0. Commit.

### Task 3: Front-end skeleton

**Files:** `frontend/` with SETH's `package.json`, `eslint.config.js`, `tsconfig.json`, `vitest.config.ts`, `next.config.ts`, `postcss.config.mjs`, `src/app/layout.tsx`, `src/app/page.tsx` (health), `src/test/setup.ts`, one Vitest test.

- [ ] `pnpm install`; `pnpm lint && pnpm typecheck && pnpm test && pnpm build` exit 0. Commit.

### Task 4: Hosted database setup (lead, no agent)

- [ ] Using `DATABASE_URL` from `.env.local` (never printed), list databases and the tables in the named database. If it holds tables this project did not create, create database `seth_unified`; create roles `shifde_owner`/`shifde_app` with generated passwords stored only in `.env.local` as `SHIFDE_HOSTED_DATABASE_URL` / `SHIFDE_HOSTED_MIGRATION_DATABASE_URL`; confirm RLS role setup works (design §9 last bullet). Record result in STATUS.md. On failure, record "hosted steps waiting" with evidence.

### Per-module recipe (Tasks 5–19)

Each module task is owned end-to-end by one builder in its own worktree (`.worktrees/<module>`) with its own local database. Steps:

1. `scout` writes `docs/build/inventory/<module>.md`: source files, SETH tests that exercise it (by test function), tables, routes, findings from `docs/build/findings.md` that belong to it, front-end routes.
2. Copy `backend/src/shifde/<module>/` from `3ba0e06`; adapt imports to modules already present only.
3. Write `backend/migrations/versions/<module>_0001_*.py` (`branch_labels=("<module>",)`, `depends_on` the modules it references) reproducing SETH's final schema for the module's tables; add FKs to tables owned by later modules in that later module's migration. Add the module's tables to `docs/build/ported_tables.txt`. `test_schema_vs_seth` passes.
4. Move the module's SETH tests into `backend/tests/<module>/`, unchanged in assertion; shared helpers (`p2util` etc.) go to `backend/tests/_support/`. A SETH test that needs a later module moves with that later module.
5. Add `backend/tests/<module>/test_cross_client.py`: for every route the module adds, a user of org A asks for org B's object → refused, and B's data is absent from A's lists.
6. Fix the module's findings from `docs/build/findings.md`, each with a regression test that fails first.
7. All gates exit 0; `pytest tests/<module>` and tests of every module that imports it pass.
8. Verifier runs the gate set from a clean checkout of the branch; isolation-reviewer reads critical modules and every migration. Merge to `main`, push, update STATUS.md and usage report.
9. Front-end area for the module (frontend-builder) after the API merge, same verify/merge loop.

| Task | Module(s) | Builder | Extra |
|---|---|---|---|
| 5 | `core` | sonnet | settings, db (GUC per transaction), migrations helper replace skeleton versions; keep `dbrole` |
| 6 | `adapters` | sonnet | incl. `testservers` |
| 7 | `audit`, `authz`, `observability`, `ledger` | authz → opus; others sonnet; ≤3 parallel | ledger: R3 budget overshoot, `max_cost`, usage honesty |
| 8 | `secrets` | opus | |
| 9 | `identity` | opus | local login; SHI-wide groups part 1 (groups become platform-level) |
| 10 | `tenancy` | opus | org = client; group→role bindings at org/workspace/project; who may grant; platform-admin self-add audited and visible to owner; no binding ⇒ org invisible |
| 11 | `installation` (minimal) | sonnet | only what later modules import; profile, branding and install validator not ported |
| 12 | `vendors` | sonnet | |
| 13 | `connections` | opus | C2 confirmed/fixed by test; read-only credential confirmation |
| 14 | `admin`, `models_admin` | opus | platform-level SHI provider, off per client by default, audited switch shown to owner, gate at call time |
| 15 | `governance` | sonnet | |
| 16 | `build` | sonnet | |
| 17 | `ontology` | sonnet | |
| 18 | `runtime` | opus | C1 confirmed/fixed by test; agent memory/run history per org |
| 19 | `app`, `cli`, `main` | sonnet | full app factory, seed command (two test orgs, separate groups) |

### Task 20: Side-by-side proof and completion

- [ ] `tests/e2e/test_two_orgs.py`: seed orgs `test-alpha`, `test-beta` with groups `alpha-fdes`, `beta-fdes`; each user lists organizations, workspaces, connections, runs, audit → sees only own org.
- [ ] Run the same check against the hosted database via `shifde-admin` seed + a smoke script (no tests on hosted), if Task 4 succeeded.
- [ ] Full backend suite with `pytest -n auto`, wall-clock recorded vs 20-minute target; STATUS.md lists every open major finding as a known defect.
- [ ] Push `main`. Then install the factory (charter §6).
