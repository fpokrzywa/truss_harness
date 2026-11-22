# lead/migrations-1 (PR #214) isolation review

Reviewer: isolation-reviewer (opus), 2026-11-22. Verdict: **APPROVED**. Commits 97dfab8 (audit_0002, #183), 6b43439 (packaging_0003, #158), a230e5b (connections_0003, #190).

1. connections_0003: RLS enabled and forced; one FOR ALL `tenant_isolation` policy on `current_tenant_id()` with USING and WITH CHECK; `shifde_app` SELECT, INSERT, column UPDATE (status, result, error_code, finished_at) only; composite FK `(tenant_id, connection_id)`; `(tenant_id, job_id)` unique; no definer function.
2. packaging_0003: invoker rights, pinned search_path, EXECUTE revoked from PUBLIC; `current_tenant_id() IS DISTINCT FROM NEW.tenant_id` refuses unset and other-client contexts; trigger untouched.
3. audit_0002: index only, led by tenant_id.
4. deliberate_changes.yaml: one entry added (audit index); retire-guard entry rewritten to the stricter rule; nothing removed or widened.
5. No test or assertion removed or loosened.
6. New DB-level tests as `shifde_app` assert cross-client read/insert/update/delete refusals.

Notes for the #190 factory job (posted on the issue): route must filter `requested_by = caller` plus tenant and scope; worker opens the invocation under the job's own tenant context; status transitions owned by queue code (B-2997); route needs a cross-client 404 test. Downgrades of packaging_0003 and connections_0003 weaken/drop and must not run in production.
