# SP7-2 assist skeleton: isolation review 1

Recorded by the lead from the isolation-reviewer's report (the reviewer is read-only), 2026-11-23.

Verdict: APPROVED. assist_settings, assist_specialists, assist_requests and assist_proposals: RLS enabled and forced, one tenant_isolation policy on tenant_id = shifde.current_tenant_id() with USING and WITH CHECK; grants SELECT/INSERT, column-limited UPDATE, DELETE only on requests/proposals behind a SECURITY INVOKER guard with fixed search_path; all client-table foreign keys composite on tenant_id; repository binds tenant_id; project routes resolve scope first (404); cross-client tests cover every assist route (tests/assist/test_routes_cross_client.py), P1-only member refusal and shifde_app refusal; RLS schema test reads the catalog (152 passed with tests/assist).

Advisories for SP7-3 (carried to #279): bare job_id/run_id uuids, proposals without workspace/project columns, worker routing under the organization's context.
