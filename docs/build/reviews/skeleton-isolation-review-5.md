# Skeleton: isolation review, round 5

Branch `skeleton` at `e2120c0` (fix round 4). Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: BLOCKED** (1 blocking, 3 follow-up). `tests/schema tests/core -n 8`: 181 passed. Probes as `shifde_owner` → `shifde_app` (`current_user` checked), rolled back.

Closed from round 4: item 1 (four review-4 forms plus six variants rejected on `tenant_id` tables, partitioned parent caught; `TENANT AND user key` still passes) and item 2 (aggregate SFUNC/INITCOND, quoted and spaced spellings; wrapper SFUNC reported via the function). Re-scoping did not weaken any test (PRINCIPAL_FORMS at 571; table-specific test at 691 still asserts pass-on-own/fail-on-other; new test at 841).

## Blocking

1. `backend/tests/schema/test_rls_schema.py:53-54`, `_recognised` 242-258: **a user-keyed table without `tenant_id` may hold client content and crosses clients.** `zz_p(id, user_id, organization_id, body)` with SETH's `own_row` (`USING/WITH CHECK (user_id = current_principal_id())`) passes every gate; staff user U in client B wrote 'client B secret', then in client A read it back, and could tag any organization id. Under design §5.3 (SHI-wide staff in several clients; agent memory and run history never cross organizations) an agent-memory/notes/draft table copied from `own_row` moves B content into A. The gate pushes authors there: `organization_id = current_tenant_id()` is rejected (`KEY_COLUMNS` has only `tenant_id`). SETH's own-row tables (`local_credentials`, `sessions`, `user_identities`) hold per-person identity state, not client content. Fix: principal keys count only on relations in a reviewed `PRINCIPAL_TABLES` (reason per entry; stale entries fail; additions need isolation review), seeded with `shifde.sessions`, `shifde.local_credentials`, `shifde.user_identities` (`shifde.users` keeps its table-specific key). Tests: unlisted principal-keyed table reported; PRINCIPAL_FORMS pass only on a listed table. Update B-112.

## Follow-up

2. `test_rls_schema.py:664-666`: `tenant_id = current_principal_id()` / `ref = current_principal_id()` on a `tenant_id` table are now rejected by the tenant-only rule first; the principal column check is covered only at 691. Add a principal wrong-column case on a table without `tenant_id`. Owner: skeleton schema test.
3. `sessions.active_tenant_id` is a client pointer outside the `tenant_id` rule; under `own_row` U can write any value, and SETH 3ba0e06 `identity/service.py:333` turns it into `Principal.tenant_id`. The identity/tenancy port must check the pin against U's current bindings on every request before setting `app.tenant_id`, with a cross-client test for a revoked binding. Owner: identity, tenancy.
4. Carried: B-116 items 7, 9, 10 (tenancy); B-117 `rate_card_versions_guard` (governance).
