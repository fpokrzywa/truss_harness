# Skeleton: isolation review, round 2

Branch `skeleton` at `d7c7b82` (fix round 1). Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: BLOCKED** (4 blocking, 7 follow-up). Schema and core tests pass (95, `-n 8`). Each blocking leak was demonstrated with throwaway objects in rolled-back transactions on the local cluster.

Closed from round 1: 2 (views, materialized views, foreign tables), 3 (REPLICATION, CREATEROLE, incl. members), 4 (preset context settings; checkout `RESET ALL` + check), 1 for `true`/`1 = 1`/missing WITH CHECK/look-alike helper, 5, 11, 13, 7 except follow-up 6 below. `base_0001` downgrade correct. No existing test weakened.

## Blocking

1. `backend/tests/schema/test_rls_schema.py:98-99,180-181`: any `EXISTS (...)` mentioning a helper passes. `USING (EXISTS (SELECT 1 WHERE shifde.current_tenant_id() = shifde.current_tenant_id()))` passed and returned tenant B's row to tenant A as `shifde_app`; `... OR true` inside, and a subquery over another table ignoring the outer row, also pass. Fix: subquery must reference the outer row, WHERE AND-only, one conjunct `<inner>.tenant_id = helper`; or a reviewed list of EXISTS policies (table, policy, exact text) that fails on any change. Probe tests for both bypasses.
2. `test_rls_schema.py:95,223,417`: `tenant_id IS NULL` accepted in WITH CHECK (probe at 417 approves `tenant_id IS NULL OR tenant_id = helper` as a write rule): any client can write rows every client sees. Fix: in WITH CHECK accept `tenant_id IS NULL` only ANDed with `shifde.is_platform_admin_ctx()`; probe test.
3. `test_rls_schema.py:59-69`: rewrite rules not checked. `CREATE RULE ... ON INSERT TO t DO ALSO INSERT INTO sink SELECT ... FROM data` leaked tenant B's rows to tenant A (rule actions run as the owner, which has BYPASSRLS). Fix: fail any `pg_rewrite` row in a non-system schema other than a view's `_RETURN`; probe test.
4. `backend/tests/schema/test_function_schema.py:77` (`nspname = 'shifde'`): SECURITY DEFINER functions outside `shifde` unchecked; `public.zz_dump()` returning all clients' rows passed every gate. Fix: same non-system-schema filter as `test_rls_schema.py:66-67`; probe test.

## Follow-up

5. Round-1 items 9, 10, 12 are not recorded on the branch (only 6, in B-114). Add a DECISIONS entry naming each item and its owner.
6. `base_0001_baseline.py:54-58`: `NULLIF` resolves `=` through the caller's `search_path`. Use `CASE WHEN x OPERATOR(pg_catalog.=) '' THEN NULL ELSE x END` or a `RETURN` body.
7. `test_function_schema.py:102-106`: require `pg_temp` listed last in a pinned SECURITY DEFINER `search_path`.
8. `core/db.py:134`: `RESET ALL` leaves temporary objects across checkouts; `dev_db.py:275` grants TEMPORARY to `shifde_app`. Use `DISCARD ALL` (or `DISCARD TEMP` + `RESET ALL`), or revoke TEMPORARY.
9. `core/dbrole.py:79-84`: CREATE on the database not refused (new schemas, e.g. one named `shifde_app` first on `"$user"` path; trusted extensions). Refuse `has_database_privilege(r.oid, current_database(), 'CREATE')`.
10. `dbrole.py:30-38`: `pg_read_all_data`, `pg_write_all_data`, `pg_maintain` not refused.
11. `test_rls_schema.py:91-92`: any column may be compared with either helper (`user_id = current_tenant_id()` passes). Tie `current_tenant_id()` to `tenant_id` (or a reviewed column list).
