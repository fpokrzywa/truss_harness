# app-2 isolation review 2 (parts 2b-2e src and test infra, 3a9ac10..471d257)

Verdict: APPROVED (opus isolation-reviewer, 2026-11-16).

- `RunContext.control()` (B-2440) reads run, parent and pipeline-parent rows by `tenant_id` and id; a foreign id raises LeaseLost like an unknown id; finalize goes through the fenced `ctx.txn()` with the same client and lease token.
- The new `context.control` cross-client case is non-vacuous (B's id answers like an unknown id, A's own id answers differently, B's rows unchanged).
- `installation_idp_count` counts `tenant_id IS NULL AND status = 'active'` only; test proves a client IdP is not counted.
- Pinned-session and API-key 403 cases on platform status are real.
- Dev commands refuse outside development; the superuser URL is used only for the separate sample database, whose reader role is NOSUPERUSER NOBYPASSRLS read-only; seeding goes through the public API as one organization's administrator under the app role.
- Owner sessions in `conftest.py`, `p5util`, `p6util`, `p7util` are for setup and assertions only.

Non-blocking notes (open): 9 a database error inside `control()` in the Shutdown branch leaves the run to lease expiry (robustness); 10 IdP count dedups by (display name, issuer), the sign-in list by (name, issuer host) (cosmetic).
