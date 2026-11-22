# Job 087 (#144, PR #200) lead isolation review: APPROVED

Reviewer: isolation-reviewer, 2026-11-22, `git diff origin/main...HEAD` of factory/087-arch-m2-rest-2. Recorded by the lead.

1. GET discovery-jobs (connections/service.py:858-872): connection must be in the caller's org (`_get_row`), needs `connection:read`; job lookup filters tenant, kind `connection.discover` and connection id, so another client's job or connection id gets the same 404 as an unknown id; result row read by tenant.
2. Worker `run_discover_job` (service.py:1248-1281): sets the job's tenant before reading it, re-checks requester and client (`_job_scope`) and `_testable` (org + `connection:test`); result row written with `tenant_id`; no cache keys.
3. Credentials: payload holds ids and requester only; `last_error` exposed only as a short code, else `worker_lost`; POST no longer resolves the secret; discovery payloads still scrubbed (service.py:946).
4. No migration, RLS, `backend/tests/schema` or startup role check change.
5. Cross-client tests: tests/connections/test_routes_cross_client.py:306-340 and :183, test_routes_cross_workspace.py:145, tests/authz/scoping/test_workspace_routes.py:457 and :1258.
6. Hardening note (not blocking): job matched to connection by `payload->>'connection_id'` (repository.py:357), sound only while payloads are written by `enqueue_job` alone.
