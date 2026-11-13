# Skeleton: isolation review, round 3

Branch `skeleton` at `1c4c215` (fix round 2). Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: BLOCKED** (4 blocking, 4 follow-up). Schema and core tests pass (129, `-n 8`). Probes: throwaway objects created as `shifde_owner`, read as `shifde_app` with tenant A set, in rolled-back transactions.

Closed from round 2: blocking 1–4 (EXISTS exact-text list with stale-entry check; `tenant_id IS NULL` alone in WITH CHECK refused; rewrite rules refused; SECURITY DEFINER in every non-system schema) and follow-ups 5–11. No test weakened.

Probed, no finding: function `SET app.* = ...` clause (PostgreSQL refuses it to `shifde_owner`); implicit cast to uuid (`(tenant_id)::uuid` rejected); triggers calling definer code (definer gate); event triggers (superuser only); publications (subscriber needs REPLICATION, refused); policies for other roles (every permissive policy checked).

## Blocking

1. `backend/tests/schema/test_function_schema.py:100-104`; nothing checks view text. **Context-setting code is unchecked.** A plain plpgsql `public.zz_peek2()` running `PERFORM pg_catalog.set_config('app.platform_admin','on',true)`, a SQL `shifde.zz_all()` doing the same, and a `security_invoker` view selecting `FROM pg_catalog.set_config('app.platform_admin','on',true)` (flag stays on for the rest of the transaction) all passed every gate and returned tenant B's rows to A under a SETH-style `tenant_id = current_tenant_id() OR is_platform_admin_ctx()` policy. Fix: in every non-system schema, fail any function body (`prosrc`), view definition, policy expression, column default/generated column, CHECK constraint, index expression or trigger WHEN that mentions `set_config` or `app.tenant_id|principal_id|platform_admin` (also `SET [LOCAL]`); exempt the pinned helpers; anything else on an allowlist with a reason. Probe tests.
2. `backend/tests/schema/test_rls_schema.py:272-273,217-219`: **RESTRICTIVE policies are not parsed, and in an AND only one conjunct is checked.** Restrictive `USING (pg_catalog.set_config('app.platform_admin','on',true) IS NOT NULL)` passed and returned every row; `tenant_id = current_tenant_id() AND set_config('app.tenant_id', <B>, true) IS NOT NULL` passed and returned B's rows. Fix: run the item-1 scan over every policy expression, restrictive included.
3. `test_rls_schema.py:217-222`: **round-2 item 2 only partly closed.** WITH CHECK `tenant_id = current_tenant_id() OR (tenant_id IS NULL AND user_id = shifde.current_principal_id())` passes because any recognised conjunct carries the AND; A inserted a tenant-less row that B read. Fix: in WITH CHECK, an AND containing `tenant_id IS NULL` passes only if `shifde.is_platform_admin_ctx()` is one of its conjuncts. Probe.
4. `test_rls_schema.py:222-223`: **`tenant_id IS NULL` accepted in USING of UPDATE/DELETE/ALL.** `FOR ALL USING (tenant_id IS NULL OR tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id())` passed; A deleted the shared row (UPDATE could take it over). Fix: bare `tenant_id IS NULL` only in USING of FOR SELECT policies; elsewhere ANDed with the admin flag. SETH's policies already meet this.

## Follow-up

5. `test_function_schema.py:124`: helper check keys rows by name, so an overload hides a replaced helper (`CREATE OR REPLACE shifde.current_tenant_id()` returning B's uuid + overload with the pinned body passed). Fix: key by oid; fail if any of the five helper names has more than one row. Owner: skeleton.
6. `test_rls_schema.py:120,284`: `REVIEWED_EXPRESSIONS` key ignores command, permissive/restrictive and roles. Fix: add them to the key. Owner: identity port (first user of the list).
7. B-116, round-1 item 9 (plain FKs checked as owner without RLS: existence check and cascades into another client's rows; TRUNCATE ignores RLS): tracked; must land with tenancy's first client table. Owner: tenancy.
8. Optional: require `tenant_id`/`user_id` columns on client tables to be `uuid`. Owner: tenancy.
