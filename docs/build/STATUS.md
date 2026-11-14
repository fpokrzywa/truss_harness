# Build status

Read this first after any compaction or resume. Standing instructions: `docs/build/CHARTER.md`. Plan: `docs/superpowers/plans/2026-11-13-subproject-1-foundation-and-seth-port.md`.

States: `todo`, `in progress`, `done`, `blocked` (with evidence), `waiting on owner input`.
Tokens per model: see `python scripts/usage_report.py` (by unit); the figure below is copied at acceptance.

## Sub-project 1

| Unit | State | Commit | Verification summary | Tokens (by model, at acceptance) |
|---|---|---|---|---|
| plan + control files + agents + usage report | done | (this commit) | `python -m unittest discover -s scripts/tests`: 4 passed | lead only |
| skeleton: backend, gates, CI, local DB, harness, baseline migration, schema tests, startup role check | done | `77f5d07` on `skeleton`, merged to main | `python scripts/gates.py --ref origin/skeleton` (clean checkout): scan-secrets 0 findings; uv sync; ruff format/check 0; mypy 0 (119 files); lint-imports 5 kept; pytest `-n 8` 200 passed (36.7 s); scripts unittest OK. Isolation review: 5 BLOCKED rounds, round 6 APPROVED (`docs/build/reviews/skeleton-isolation-review-1..6.md`). Fix rounds closed: RLS test accepts only tenant-keyed policy forms (exact-text list for EXISTS, tenant atoms only on `tenant_id` tables, reviewed `PRINCIPAL_TABLES`), WITH CHECK required, views/matviews/foreign tables/rewrite rules covered, catalog scan refuses any code writing `app.*`, SECURITY DEFINER allowlist in every schema with pinned `search_path`, helpers pinned by oid; role check refuses superuser, BYPASSRLS, owner, REPLICATION, CREATEROLE, CREATE on db/schema, data-wide predefined roles, preset `app.*`; pool checkout `DISCARD ALL` + context check; check runs in `Database()`. Follow-ups for later modules: B-116, B-117, review 6 item 4 (identity). | unit `skeleton` per usage report: opus (module-builder-critical, isolation-reviewer) + sonnet verify; 366 requests, output 504k, cache write 1.70M, cache read 42.4M, est. $26.61 at list price |
| skeleton: frontend shell | done | `cbeb976`, merged `6f60b00` | verifier PASS from clean checkout: scan-secrets 0 findings; pnpm lint/typecheck 0; pnpm test 157 passed; pnpm build 0; config byte-identical to SETH. File-to-area map: `docs/build/frontend-areas.md`. CI job pending in `docs/build/frontend-ci-job.yml` until ci.yml exists | sonnet (build + verify) |
| hosted database setup | waiting on owner input | | Server is PostgreSQL 16.13 and refuses TLS (`sslmode=require`: "server does not support SSL"); design requires TLS. Database `truss_harness` is empty. Nothing created. See B-009. | lead only |
| core | done | `4bf4563`, merged `a814a53` | `gates.py --ref main --no-frontend` on the merge: scan 0 findings; ruff format/check 0; mypy 0 (135 files); lint-imports 5 kept; pytest `-n 8` 245 passed (28.7 s); scripts OK. All 26 SETH core files; skeleton safeguards kept. Fixed: Item 3 (client IP via trusted proxies, B-201), Item 5b core part (body cap, B-202), Item 18 core part (connect/statement timeout, B-203), ARCH m1 (settings repr, B-204). Isolation review APPROVED (`reviews/core-isolation-review-1.md`); follow-ups 1, 3, 5 fixed before merge, rest in `carry_forward.md`. 14/15 of `test_core.py` moved; deferred SETH tests listed in `carry_forward.md` | sonnet build (2 rounds), opus review: 51 requests, est. $1.67 (unit `core`; review counted under its agent) |
| adapters | done | `82cf9cb`, merged `0446a0c` | `gates.py --ref main --no-frontend` on the merge: scan 0 findings; ruff format/check 0; mypy 0 (141 files); lint-imports 5 kept; pytest `-n 8` 413 passed (39.9 s); scripts OK. 16 files; `adapters_0001` (`adapter_conformance_runs`, global with reason B-301; schema comparison passes); 150 SETH tests moved; Items 9 and 11 adapters part (max_tokens validated and sent; omitted usage stays None) and ARCH m13 fixed (B-304); Item 16 known defect. Isolation review APPROVED (`reviews/adapters-isolation-review-1.md`); deferred tests and follow-ups in `carry_forward.md` | sonnet build, opus review: 83 requests, est. $2.08 (unit `adapters`) |
| audit | todo | | | |
| authz | done | `0ccc34c`, merged `74e6a83` | `gates.py --ref main --no-frontend` on the merge: scan 0 findings; ruff format/check 0; mypy 0 (145 files); lint-imports 5 kept; pytest `-n 8` 452 passed (49.7 s); scripts OK. `authz_0001`: 7 tables (permissions global with reason B-401; others SETH tenant-keyed policies), seeds 92 permissions / 8 system roles / 339 grants; 7 FKs to tenancy/identity deferred; binding trigger fails closed until `memberships` exists. Group-binding seam for SHI-wide groups (B-403). Core fix: `prepare_threshold=None` (B-404). 8 SETH tests moved, rest deferred (`carry_forward.md`); cross-client tests as `shifde_app`. Isolation review APPROVED (`reviews/authz-isolation-review-1.md`); follow-up 1 (`platform:admin` in a client role) closed on branch `authz-fix` `a8da7f8` (fast-forwarded to main): `authz_0002` CHECK + PDP filter (B-406), 7 tests; `gates.py --ref authz-fix --no-frontend` 8/8, 536 passed; review 2 APPROVED (`reviews/authz-isolation-review-2.md`) | opus build ~290k agent tokens (over the 200k sizing), opus review |
| observability | done | `0f76432`, merged `82b3b66` | `gates.py --ref main --no-frontend` on the merge: scan 0 findings; ruff format/check 0; mypy 0 (144 files); lint-imports 5 kept; pytest `-n 8` 419 passed (52.4 s); scripts OK. 6 files copied unchanged; no tables; readiness uses `core.health.migration_state`; 6 SETH tests moved, 7 deferred (app, governance; `carry_forward.md`). Router mounting is owed by `app` (B-501). No migration and not critical: no isolation review. Haiku trial passed in one round (B-011) | haiku build: 25 requests, est. $0.04 (unit `observability`) |
| ledger | done | `3e9f6d5`, merged `36bd4db` | `gates.py --ref main --no-frontend` on the merge: scan 0 findings; ruff format/check 0; mypy 0 (146 files); lint-imports 5 kept; pytest `-n 8` 529 passed (63.9 s); scripts OK. `ledger_0001` (8 tables; tenant FKs and runs FK deferred to tenancy/runtime); Item 9 ledger part (hold = max(estimate, ceiling)), Item 10 primitive (`estimate.call_cost`), Item 11 ledger part (estimated not 0, durable record) (B-601..B-605). Isolation review 1 BLOCKED (definer guard oracle; budget_alerts keys), fix round 1 (B-606..B-610), review 2 APPROVED (`reviews/ledger-isolation-review-1..2.md`). 0 SETH tests moved (all need the app World; deferred in `carry_forward.md`), 71+ direct tests | sonnet build ~253k + fresh sonnet fix ~115k agent tokens, opus reviews x2 (unit `ledger` per usage report) |
| secrets | todo | | | |
| identity | todo | | | |
| tenancy | todo | | | |
| installation (minimal) | todo | | | |
| vendors | todo | | | |
| connections | todo | | | |
| admin | todo | | | |
| models_admin | todo | | | |
| governance | todo | | | |
| build | todo | | | |
| ontology | todo | | | |
| runtime | todo | | | |
| app, cli, main | todo | | | |
| two-org side-by-side proof | todo | | | |
| software factory install | todo | | | |

## Audit findings

In scope for sub-project 1: C1, C2, batch R2 (ported code), R3 cost items (budget overshoot, `max_cost`, usage honesty). Mapping of every finding to a module: `docs/build/findings.md`.

### Known defects (major findings left open after sub-project 1)

(filled in as modules land)

## Waiting on owner input

- **Hosted PostgreSQL**: enable TLS on the server (and ideally PostgreSQL 17, per design §3). Until then the two-organization proof and all other checks run on the local cluster; the hosted role setup, migration and side-by-side check wait (B-009).

- Databricks development workspace (`.devdata/databricks.env`): not needed in sub-project 1.
- Entra application registration: not needed in sub-project 1.
- Snowflake development account: not needed in sub-project 1.
- Where SETH will be hosted: not needed in sub-project 1.
