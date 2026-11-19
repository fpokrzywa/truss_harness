APPROVED (isolation-reviewer, opus) — PR #70, origin/factory/024-sp2-6-delivery-deployments at 6fdc259 vs origin/main.

Nothing blocking. Verified:
1. delivery_deployments, delivery_native_resources: RLS enabled+forced, one tenant_isolation policy USING+WITH CHECK (delivery_0005_deployments.py:96-104,142,171); grants column-limited, no DELETE, app owns nothing; status-guard trigger SECURITY INVOKER, pinned search_path, EXECUTE revoked from PUBLIC; no SECURITY DEFINER. Covered by tests/schema/test_rls_schema.py.
2. All FKs to plans/targets/deployments composite with tenant_id; fk_delivery_jobs__deployment and fk_delivery_journal__deployment (B-2673) tested by test_routes_cross_client.py::test_bs_job_and_journal_cannot_name_as_deployment.
3. Settle step (repository.py:154-212) scoped to tenant_id = :t / u.tenant_id = d.tenant_id; callers pass scope's or executor's tenant.
4. Result hooks (service.py:565-775) use leased job's tenant; on_apply_result refuses receipts with mismatched tenant/target/artifact (422, no change).
5. Native-resource ownership keyed on (tenant_id, target_id, ...); platform claims exclusive across orgs (delivery_0001:106-110).
6. Routes: unknown / other-org / unreadable deployment all 404; permission before settle; list filtered by tenant + visible workspaces/projects.
7. All five routes in ROUTE_TESTS, F-08 classified SCOPED with cases, listed in tests/app/test_routes.py.
8. Edits to test_gateway.py, test_gateway_cross_client.py, test_journal.py only replace random deployment ids with same-tenant real rows; no assertion removed or loosened.

Optional (not leaks):
9. No cross-org test for fk_delivery_native_resources__deployment / __target.
10. No test that settling A's deployments leaves B's untouched.
