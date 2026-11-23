# Isolation review: runtime_0007 (jobs.payload scrub guard, issue #224)

Reviewer: isolation-reviewer (opus), 2026-11-23, diff origin/main...lead/jobs-payload-scrub at 09291cd.
Verdict: APPROVED, no blocking findings.

1. Only new privilege is `UPDATE (payload)`; `kind` and `tenant_id` stay non-updatable, so no row can move organization or kind. Forced RLS and `tenant_isolation` (USING and WITH CHECK) unchanged; cross-client UPDATE filtered to 0 rows before the trigger.
2. Guard is plpgsql invoker, reads no table, fixed `search_path = pg_catalog, pg_temp`, PUBLIC execute revoked.
3. NULL payloads: column is NOT NULL; NULL would also fail the object/equality checks.
4. Non-object payloads refused by `jsonb_typeof(...) = 'object'`.
5. Kind/status changes in the same UPDATE: OLD/NEW kind both checked; scrub with a non-terminal status refused; queued/leased -> done with scrub stays in-organization and the status grant already allowed it.
6. No other BEFORE UPDATE trigger on jobs; claim/reap definer functions never write payload.
7. `tests/identity/test_schema.py` head assertion tightened (runtime_0007 head, runtime_0006 not a head); nothing loosened.
8. `deliberate_changes.yaml` entries scoped to runtime_0007; none covers a policy or RLS flag.
9. Cross-client test (A against B's ended invoke job) touches 0 rows, B's payload byte-for-byte unchanged, A's own scrub succeeds; no-context case 0 rows; guard also tested as schema owner.
10. Note for job 119: the service code must scrub inside the worker's per-job organization context, never through an owner engine.
