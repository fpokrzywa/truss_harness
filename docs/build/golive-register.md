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
| Item 5a | open | F-14 and ARCH m8, no security headers: closes when SP4-2 #124 and SP4-3 #125 merge. |
| Item 20 delete | open | ARCH M4 / F-11 retention, deleting old ended runs: closes when SP4-8 #126 merges (the content-purge notes were closed by #46). |
| Item 20 audit-retention | pending-owner | The owner must accept in writing that audit events are never deleted and not partitioned: the audit hash chain is kept for life, and `docs/build/perf.md` shows no need to partition at test scale. |
| Item 21 | closed | ARCH M2, adapters inline in the API: closed by #47. |
| Item 23 | pending-owner | F-23 traceability: the owner must accept not mapping SETH's PRD ids, which belong to a document the unified design replaced; mapping them adds no assurance, and the design 5.3 matrix stays enforced by `scripts/traceability.py --check`. |
| ARCH M11 | closed | `data:export`: closed by #56. |
| ARCH M5 | closed | F-18 settings: the security and limits settings are `SHIFDE_*` settings refused at startup and in preflight, closed by #123 (SP4-1). |
| REQ F-08 | pending-owner | RLS tenant-only: the owner must accept B-2655's residual risk; the run-time refusal checks of #58 are done. |
| REQ F-22 | pending-owner | No real IdP sign-in: the owner must accept going live on local login with `SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION=true`, unless `tests/identity/test_entra_live.py -m live` passes with `.devdata/entra.env` (then this row is `closed`). |
