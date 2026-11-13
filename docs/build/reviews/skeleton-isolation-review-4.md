# Skeleton: isolation review, round 4

Branch `skeleton` at `2830270` (fix round 3). Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: BLOCKED** (1 blocking, 3 follow-up). `tests/schema tests/core -n 8`: 174 passed. Probes: throwaway objects as `shifde_owner`, read/write as `shifde_app` with tenant and principal set, rolled back.

Closed from round 3: 1, 2, 4, 5, 6 (context-write scan over functions, views, policies incl. restrictive, defaults, constraints, indexes, triggers, statistics; bare `tenant_id IS NULL` rule for WITH CHECK and USING of ALL/UPDATE/DELETE; helpers by oid, overloads refused; `REVIEWED_EXPRESSIONS` keyed by command/permissive/roles). Item 3 closed only for the literal `tenant_id IS NULL` (see blocking 1). No test weakened: `test_rls_schema.py:614` USING change is covered by `test_shared_rows_writable_by_a_client_are_reported` (820).

## Blocking

1. `backend/tests/schema/test_rls_schema.py:236-237`, `_admits_shared_rows` 250-262: **a user-keyed check lets one client write rows every client, or another client, sees.** Probe: `FOR SELECT USING (tenant_id IS NULL OR tenant_id = current_tenant_id())` + `FOR INSERT WITH CHECK (user_id = current_principal_id())` (SETH's `own_row`, copied) passed every gate; as client A the user inserted rows with `tenant_id` NULL and = B, and B read both. `USING (user_id = current_principal_id())` also showed a user acting for A their row from B; `NOT (tenant_id IS NOT NULL) AND user_id = ...` evades the text match. Cause: in an AND any recognised conjunct carries it, and the user key counts. Fix: on any relation with a `tenant_id` column, only tenant atoms count for USING and WITH CHECK (`tenant_id = current_tenant_id()`, its NOT IS DISTINCT FROM form, `is_platform_admin_ctx()`, bare `tenant_id IS NULL` only in USING of FOR SELECT); the user key may narrow but not satisfy. Checked against every SETH 3ba0e06 policy on a `tenant_id` table: rejects none beyond `audit_events.write_own` (already rejected); SETH's `own_row` tables have no `tenant_id`. Probe tests for the four forms; update B-112, B-116.

## Follow-up

2. `backend/tests/schema/catalog_scan.py:83-96`: an aggregate with `SFUNC = pg_catalog.set_config`, `INITCOND = 'app.platform_admin'` in a `security_invoker` view switched the admin flag on and passed every gate (aggregate's `pg_proc` row is `aggregate_dummy`; `pg_aggregate` not read). Hostile-author only. Fix: refuse `pg_aggregate` support functions in system schemas / `set_config`; scan `agginitval`/`aggminitval`. Owner: skeleton schema test.
3. B-116 items 7, 9, and 10 (no admin-flag OR in client-content policies): tracked. Owner: tenancy.
4. B-117 `rate_card_versions_guard` `set_config('shifde.ratecard_close', …)`: allowlist or other signal at port. Owner: governance.
