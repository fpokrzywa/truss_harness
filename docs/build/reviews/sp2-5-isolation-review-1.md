APPROVED (isolation-reviewer, opus) — PR #67, origin/factory/023-sp2-5-delivery-artifacts at 846faf9 vs origin/main.

No way found for one organization to read, approve, reject, seal or reference another organization's plan, approval, artifact or job.

Verified:
1. RLS enabled+forced, one tenant_isolation policy (USING+WITH CHECK) on delivery_artifacts, delivery_plans, delivery_approvals (delivery_0004_plans.py:124-132,151,193,217); covered by tests/schema/test_rls_schema.py.
2. Composite (tenant_id, ...) FKs plan->target, plan->artifact, approval->plan; fk_delivery_jobs__plan (tenant_id, plan_id) per B-2673 (delivery_0004_plans.py:149,184-188,213-215,224-225); cross-client test test_routes_cross_client.py::test_bs_job_cannot_name_as_plan.
3. Grants: shifde_app SELECT/INSERT on artifacts+approvals, SELECT + column-limited INSERT/UPDATE on plans, no DELETE, owns nothing.
4. Both trigger functions SECURITY INVOKER, pinned search_path, EXECUTE revoked from PUBLIC; no SECURITY DEFINER added.
5. Every repository query/update filters tenant_id = :t; settle_plans scoped to caller org, lost-job EXISTS joins on tenant; plans._get_row re-checks tenant_id.
6. In-org scope: unreadable plan = same 404 as unknown; list filtered by visible workspaces/projects; request via TargetService.plannable.
7. Executor artifact route proves lease (tenant, executor_id, lease_id, fence, unexpired) then reads artifact by the job's plan_id in executor tenant; test_bs_executor_cannot_fetch_as_plan_artifact.
8. on_plan_result/start_planning act only on the leased job's tenant_id/plan_id.
9. ROUTE_TESTS covers all five new plan routes; test_workspace_routes.py adds workspace-scope cases.

Non-blocking:
A. test_a_cannot_reach_bs_plan tries approve/reject only on B's unsealed plan; a sealed-B-plan case would make protection explicit.
B. Nothing enforces plan job target_id == plan target_id (same-org consistency gap, not isolation).
