APPROVED

Isolation review 2, job 050 (#47, PR #122), round 2 commit `29719be`, diff of `backend/src/shifde/audit/`, `models_admin/`, `runtime/` against origin/main. Reviewer: isolation-reviewer (read-only; text saved by the lead). Required by S-020 before the hand merge.

1. Result lookup (`models_admin/service.py:196-206` `_recorded`, `audit/repository.py:94-112` `find_event`) filters on the job's `tenant_id`, action, provider `target_id`, `correlation_id = "<prefix>-<job_id>"` (server-generated uuid7) and `occurred_at >= created_at - 5 min`, under forced RLS. The skew window only admits older rows with the same tenant, provider and job-unique correlation id.
2. Advisory lock key `hashtextextended('model-job:<job_id>', 0)` (`models_admin/repository.py:582-587`) has no tenant; a cross-organization clash needs a 64-bit collision of unique UUIDs and would only make two transactions briefly wait. No data crosses.
3. `IntegrityError` guard (`service.py:1769-1780`): after rollback the job's tenant is re-applied by the `after_begin` hook (`core/db.py:67`); an unexplained error is re-raised.
4. `execute_health_check` / `execute_discovery` (`service.py:546-604`) take the provider tenant-scoped with `write=True`; another organization's provider gives 404 and the shared one Forbidden; the requester is re-derived from the database.
5. Health detail scrubbed before the 500-character cut (`service.py:579`).
6. Low, non-blocking: a caller-supplied X-Request-ID could equal `model-health-<job_id>`, but only the worker writes these actions and the lookup is tenant-limited (`observability/middleware.py:35-36`).
7. The worker dispatches provider jobs with the job row's own `tenant_id` (`runtime/worker.py:339-341`).
