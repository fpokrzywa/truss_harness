# Go-live register

The audit findings that were still open or narrowed at go-live planning (design
`docs/superpowers/specs/2026-11-21-subproject-4-go-live-design.md` section 7), one row each.
`shifde-admin config preflight` reads the table below. A row's `state` is one of:
`closed` (the evidence names the issue, PR or test that closed it); `accepted` (the evidence
quotes the owner's own words, the date and where they were given; the supervising session's
delegated approval does not count); `pending-owner` (the evidence says what the owner must
decide; preflight prints `pending: <id>`); `open` (the evidence names the issue that will close
it; preflight refuses).

| id | state | evidence |
|---|---|---|
| Item 5a | closed | F-14 and ARCH m8, no security headers: closed by SP4-2 #124 (PR #169) and SP4-3 #125 (PR #172). |
| Item 20 delete | closed | ARCH M4 / F-11 retention, deleting old ended runs: closed by SP4-8 #126 (PR #173); the content-purge notes were closed by #46. |
| Item 20 audit-retention | accepted | Owner, 2026-11-23 10:17 EST, owner's own message to the supervising session (Claude Code session 69dbbb7d): "I accept Item 20 (audit events kept for life, not partitioned)". The audit hash chain is kept for life; `docs/build/perf.md` shows no need to partition at test scale. |
| Item 21 | closed | ARCH M2, adapters inline in the API: closed by #47. |
| Item 23 | accepted | Owner, 2026-11-23 10:17 EST, owner's own message to the supervising session (Claude Code session 69dbbb7d): "I accept ... Item 23 (SETH PRD ids not mapped)". The design 5.3 matrix stays enforced by `scripts/traceability.py --check`. |
| ARCH M11 | closed | `data:export`: closed by #56. |
| ARCH M5 | closed | F-18 settings: the security and limits settings are `SHIFDE_*` settings refused at startup and in preflight, closed by #123 (SP4-1). |
| REQ F-08 | accepted | Owner, 2026-11-23 10:17 EST, owner's own message to the supervising session (Claude Code session 69dbbb7d): "I accept ... REQ F-08 (workspace isolation inside a client relies on the service layer)". Residual risk as recorded in B-2655; RLS stays tenant-only, with the run-time refusal checks of #58. |
| REQ F-22 | pending-owner | No real IdP sign-in: the owner must accept going live on local login with `SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION=true`, unless `tests/identity/test_entra_live.py -m live` passes with `.devdata/entra.env` (then this row is `closed`). Owner, 2026-11-23 10:17 EST, owner's own message to the supervising session (Claude Code session 69dbbb7d): "For F-22 I'll run the Entra live test." Stays pending until `tests/identity/test_entra_live.py -m live` passes. |
| Hosting target | accepted | Owner, 2026-11-23 10:17 EST, owner's own message to the supervising session (Claude Code session 69dbbb7d): "Hosting: Ubuntu 24.04, ... domain 3cpublish.com", and, asked where the production database runs, chose "Hosted PG17 (Tailscale)" (the PostgreSQL 17 that `scripts/hosted_db.py` sets up). Host layout: SP4-10 #157 (PR #195), `docs/runbooks/ubuntu-host.md`. Known gap, not accepted here: the hosted server has no server TLS and its port/`pg_hba` trust need the owner's firewall fix (open since 2026-11-19). |
