# Skeleton: isolation review, round 6

Branch `skeleton` at `77f5d07` (fix round 5). Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: APPROVED** (0 blocking, 3 follow-up). `tests/schema tests/core -n 8`: 200 passed. Probes as `shifde_owner` → `shifde_app` (`current_user` checked), rolled back.

Closed from round 5:

1. Blocking 1: `PRINCIPAL_TABLES` (`backend/tests/schema/test_rls_schema.py:162`) and the listed-only principal key (`:290`). On an unlisted `zz_p` the gate reported `own_row`, `NOT (user_id IS DISTINCT FROM principal)`, `principal = user_id AND id IS NOT NULL`, `own_row OR is_platform_admin_ctx()`, `id = principal`, and split SELECT/INSERT policies on a partitioned parent and partition.
2. Follow-up 2: wrong-column principal keys on a listed table (`:1125`).
3. The re-scoped test (`:702`) is not weaker: forms must pass on a listed table, fail on an unlisted one, and both fail under the real list.

The builder's deviation (`:490`, missing listed tables tolerated until ported) is sound: the exemption only covers SETH 3ba0e06 `shifde` tables without `tenant_id` that have a permissive principal policy (exactly sessions, local_credentials, user_identities, users), `test_reviewed_principal_tables` pins the list, and a missing table fails once listed in `ported_tables.txt`.

## Follow-up

4. `test_rls_schema.py:503`: an existing `PRINCIPAL_TABLES` entry is matched by name only, not tied to SETH's shape: a non-SETH `shifde.sessions(id, user_id, organization_id, body)` with `own_row` passed and leaked. Not blocking (no port before identity creates `sessions`). Fix: an existing `shifde` entry must also be in `ported_tables.txt` (so `test_schema_vs_seth` compares it); test that the probe fails. Owner: identity (or skeleton schema test).
5. Still open: B-116 items 7, 9, 10 (tenancy); `sessions.active_tenant_id` pin (identity, tenancy); B-117 `rate_card_versions_guard` (governance).
