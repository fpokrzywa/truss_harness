# Isolation review 1, job 176 (#351, PR #356) at 489bf9a

APPROVED (isolation-reviewer, 2026-11-24; diff, touched files and migration text comparison read, no tests run; saved by the lead from the reviewer's report, shortened). No blocking finding.

1. `assist_0002` is CREATE OR REPLACE, SECURITY INVOKER, `search_path = pg_catalog, pg_temp`; owner, PUBLIC revoke and both triggers kept; no definer, grant, policy or table added.
2. Floor, hold and retention statements identical to runtime_0009's text; PREVIOUS_GUARD identical to assist_0001's.
3. RLS is enabled and forced on the assist tables and `retention_policies`, subselects filtered by `OLD.tenant_id`; no refusal text can reveal another client's hold or retention.
4. Bypasses read (future, infinity, -infinity, empty, unparsable cutoff, column updates, TRUNCATE, cascade): all refused.
5. Residual shared with runtime_0009: a raw-SQL `shifde_app` session can change `app.tenant_id` inside the DELETE, so the trigger reads no policy rows. No gain on other clients' rows; no application path. Possible follow-up for both guards: refuse when `shifde.current_tenant_id()` differs from `OLD.tenant_id`.
6.-9. `TargetService.readable`, `_payload_readable`, the lost-accept audit event and the maintenance docstring: verified, tenant-filtered, nothing revealed across clients.
10. `tests/assist/test_routes_cross_client.py`: only the two row ages, two cutoffs and the `_seed` docstring change; every assertion, table and case unchanged, and the B-row delete stays meaningful.
11.-14. No cross-client or RLS test weakened; `backend/tests/schema` untouched; `identity/test_schema.py` only moves the head.
