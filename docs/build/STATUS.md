# Build status

Read this first after any compaction or resume. Standing instructions: `docs/build/CHARTER.md`. Plan: `docs/superpowers/plans/2026-11-13-subproject-1-foundation-and-seth-port.md`.

States: `todo`, `in progress`, `done`, `blocked` (with evidence), `waiting on owner input`.
Tokens per model: see `python scripts/usage_report.py` (by unit); the figure below is copied at acceptance.

## Sub-project 1

| Unit | State | Commit | Verification summary | Tokens (by model, at acceptance) |
|---|---|---|---|---|
| plan + control files + agents + usage report | done | (this commit) | `python -m unittest discover -s scripts/tests`: 4 passed | lead only |
| skeleton: backend, gates, CI | todo | | | |
| skeleton: local DB, harness, baseline migration, schema tests, startup role check | todo | | | |
| skeleton: frontend shell | done | cbeb976 (merge on main) | verifier PASS from clean checkout: scan-secrets 0 findings; pnpm lint/typecheck 0; pnpm test 157 passed; pnpm build 0; config byte-identical to SETH. File-to-area map: `docs/build/frontend-areas.md`. CI job pending in `docs/build/frontend-ci-job.yml` until ci.yml exists | sonnet (build + verify) |
| hosted database setup | waiting on owner input | | Server is PostgreSQL 16.13 and refuses TLS (`sslmode=require`: "server does not support SSL"); design requires TLS. Database `truss_harness` is empty. Nothing created. See B-009. | lead only |
| core | todo | | | |
| adapters | todo | | | |
| audit | todo | | | |
| authz | todo | | | |
| observability | todo | | | |
| ledger | todo | | | |
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
