# Isolation review 1: job 035 (#81 SP3-4 delivery plans, PR #98)

**Verdict: APPROVED, 0 blocking findings, 1 note.** Reviewer: `isolation-reviewer`, 2026-11-20. Scope: `git diff origin/main...469cb05`. Code read; tests not run by the reviewer.

1. `delivery_0006_releases.py`: both new foreign keys include `tenant_id` (`(tenant_id, release_id)` -> `releases`, `(tenant_id, evaluation_run_id)` -> `evaluation_runs`), backed by existing unique keys; `shifde_app` gets INSERT only on the new columns; forced RLS, policy and seal trigger on `delivery_plans` unchanged; no SECURITY DEFINER, nothing owned by `shifde_app`.
2. `delivery/plans.py` request: release resolved by `scoped_release` (tenant lookup, then `hide_unless=READ`); unknown, foreign and unreadable all give the same 404; the 409s only follow that check; `require_passing_evaluation` is tenant-filtered.
3. Approval re-check: `release_record(..., tenant_id=...)` plus tenant-scoped evaluation lookup after the row lock; a failure rolls back.
4. `executor/runner.py` `ReleaseArtifactStore`: delegates to `InMemoryArtifactStore.read` (tenant match and digest check) before dropping `release.json`; a fresh store per job.
5. `tests/delivery/test_routes_cross_client.py`: A naming B's sealed, passed release gets the same 404 bytes as an unknown id, nothing written. No new routes.

## Note (same organization only, not blocking)

- N1 `plans.py:192-195`: a workspace-wide target accepts a release from any project in its workspace, and the plan artifact carries the release manifest; a workspace-level plan reader without that project's release read could see the manifest. Filed as a follow-up.
