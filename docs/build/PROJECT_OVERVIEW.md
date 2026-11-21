# SETH unified platform: what is built and what is left

Snapshot for the owner, 2026-11-21 07:40 EST. The live sources are `docs/build/STATUS.md` (row by row, with verification), `docs/build/DECISIONS.md` (every call made) and the GitHub issues labelled `factory`. This page summarises them; where they differ, they win.

## What SETH is

One application, SETH, used by SHI staff, that merges three earlier codebases (SETH, the FDE SDK and Agent Factory) into one repository and one PostgreSQL database. A forward deployed engineer signs in, works inside one client's space, builds and tests an AI solution against that client's platform, and deploys the finished solution onto the client's own infrastructure. SETH itself is never installed at a client. Design: `docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md`.

Stack: Python 3.13 and FastAPI backend (`backend/`, package `shifde`), Next.js and React 19 front end (`frontend/`), PostgreSQL 17 with forced row-level security on every client-owned table, an app role that cannot bypass it, and a startup check that refuses a privileged role.

## Progress by sub-project

| # | Sub-project | State | Notes |
|---|---|---|---|
| 1 | Foundation and SETH port | **Done** (2026-11-17) | Every SETH module ported in layer order (core, adapters, audit, authz, observability, ledger, secrets, identity, tenancy, installation, vendors, connections, admin, models_admin, governance, build, ontology, runtime, app, cli), each with its migration, tests and cross-client tests. Hosted database set up. Front end ported. |
| 2 | Native delivery, Databricks | **Done against the fake platform** (10 of 10 issues) | Client targets, sealed plans, second-person approval, executor, deployment journal and verification. The live run waits on your Databricks workspace (B-2697). |
| 3 | Releases, evaluation, cost reservation, Entra sign-in | **Done against the fake platform and a fake Entra issuer** (9 of 9 issues, #78 to #86) | A release is sealed by digest, the compatibility check names every unsupported feature, a plan must name a release that passed its evaluation, and cost is reserved before each model call. The live Entra sign-in waits on your app registration. |
| 4 | Go-live | **Not started** | Close or accept every remaining major audit finding; production mode, trusted proxy, headers, backup and restore, process control. No real client data may enter SETH before this is done. |
| 5 | Solution generator for Databricks | Not started | Generates the agent service, React front end and platform files from a solution. |
| 6 | Snowflake | Not started | Same flow, producing a Snowflake Native App. |
| 7 | Supervisor and specialists; project and release board | Not started | |
| 8 | MCP access; Fabric; Palantir; standalone Docker target | Not started | Scope decided when reached. |

Sub-projects 4 to 8 have no design or issues yet. The lead writes each one's short design, cuts it into issues and runs them through the factory (charter section 7).

## Open work in the factory queue (23 issues)

- **Hardening follow-ups from reviews** of sub-projects 2 and 3 (#64, #66, #68, #69, #71, #72, #74, #91, #93, #108): extra cross-client tests, executor limits, lease caps, release-manifest redaction.
- **Known defects from the SETH audit** (#33, #39, #41, #44, #46, #47, #55, #56, #58, #60, #62): capture-off handling, tenant settings grants, the worker cap on wedged runs, the data export permission, approval-rule bypasses, OIDC key expiry.
- **Flaky tests** (#51, #109).

`gh issue list --label factory --state open` shows the current list.

## How the build runs

- **One lead, many agents (charter section 12).** The supervisor (`scripts/supervisor/supervise.py 2`) runs one headless Opus lead at a time. The lead runs `/factory run` with up to 10 jobs at once (`factory/config.json` `concurrency`). Each job has its own worktree, database, builder, reviewers and approver.
- **Every job passes the same gates.** These are the secret scan, lint, types, import contracts, the full backend suite, the front end, the scripts suite and an isolation review for client-data code. Merges go one at a time under a merge lock. No gate is skipped or weakened.
- **Models.** Opus for the lead, builders, spec writers, approvers and the security and isolation reviewers. Opus or Sonnet for the code reviewer, depending on risk. Opus for UX review and Sonnet for UI review. Sonnet for intake. Changed on 2026-11-20 to cut rework (S-013).
- **Handoffs.** A lead hands off at 200k tokens of context. The supervisor starts the next one within seconds, and that lead resumes every job from the factory's job state.
- **Failure handling.** The watchdog (`scripts/supervisor/watchdog.py --minutes 30`) alerts with a Windows notification and `.devdata/supervisor/logs/ALERTS.log`. A lead that crashes is salvaged and restarted, backing off for up to 16 minutes.
- **Reboots.** `scripts/supervisor/autostart.ps1` restarts the cluster, the supervisor and the watchdog. It runs at logon once the scheduled task is registered (see below).

## Cost and pace

- Total spend: about **$1,097** at list price (estimate, not a bill), from `python scripts/usage_report.py`.
- Factory issues: **44 of 67 closed**, at about **$8.22 per issue**.
- Pace: overnight on 2026-11-20/21, about 1.1 issues per hour. Charter section 12 should raise that by keeping all 10 slots busy.
- The 23 open issues project to about $190 and under a day. Sub-projects 4 to 8 come on top of that and are not yet sized.

## What only you can do

1. **Register the reboot task.** Run the `Register-ScheduledTask` command in `new_session_handoff.md`. The permission check blocked the session from doing it.
2. **A Databricks development workspace and credential**, for sub-project 2's live run: `docs/runbooks/databricks-live-validation.md`.
3. **An Entra app registration**, for sub-project 3's live sign-in: `docs/runbooks/entra-live-validation.md`.
4. **The hosted database server.** Close the public port and the `trust` rules in `pg_hba`, and turn on TLS on the server.
5. **Where SETH will be hosted**, needed for sub-project 4 (go-live).
6. **A Snowflake development account**, needed for sub-project 6.
7. **The dashboard title.** It reads "Truss Hardness" and may be meant to say "Harness".
