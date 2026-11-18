# ARCH M2 (job 012, #10) isolation review 3 (round-3 delta a583870..c726489): APPROVED

Files: `cli/sample_pipeline.py`, `tests/app/test_p5_reliability.py`, `test_p5_review.py`, `test_p8_budgets.py`, DECISIONS. No RLS schema test, startup role check, migration or `tests/schema` change.

1. `cli/sample_pipeline.py:246-260` `_run_test`: tenant id from `GET /api/v1/orgs` as the signed-in user (only orgs they are bound to), matched to the seeder's own org slug; job id from the same org's 202. `run_test_job` (`connections/service.py:1105-1160`) sets the tenant, loads the job by `tenant_id` + kind under RLS, re-derives the requester via `authz.execution_principal`, re-checks `connection:test`; no admin/owner context; app-role engine (`app.state.db`). Same code as the worker. Dev-only.
2. `:255-259`: result read via the org-scoped status route; `outcome == 'pass'` check kept; job row left queued, later worker ends it done (B-2610).
3. `test_p5_reliability.py:376-379, 464-466`, `test_p8_budgets.py:58-61`: expected values unchanged; narrowing to `run.advance` keeps the meaning; not cross-client; not weakened.
4. `test_p5_review.py:131, 137-142, 588`: UPDATE and `scalar_one` narrowed, still expects "queued" and exactly one run job; claim row `kind` matches `shifde_claim_jobs`. No assertion removed or relaxed.
