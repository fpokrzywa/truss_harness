APPROVED

# SP2-4 isolation review 1: PR #65, job 022, `origin/main...origin/factory/022-sp2-4-delivery-job`

(Saved by the lead from the isolation-reviewer's report; the reviewer runs read-only. Condensed to its verdict, checks and findings.)

Allow-list entry: the exact text in DECISIONS row B-2671 is correct to add verbatim to `SECURITY_DEFINER_ALLOWLIST` (owner `shifde_owner`, pinned `search_path`, qualified objects, EXECUTE to `shifde_app` only, no `app.*` write, only writer of the lease columns, organization only from `current_tenant_id()`, 42501 without one or for an inactive executor, at most one lease).

No protected path touched; no cross-client test, RLS schema test or startup role check weakened (only `tests/identity/test_schema.py` head moves to `delivery_0003`, nothing removed).

Checked and correct: forced RLS and one `tenant_isolation` policy on `delivery_jobs` and `delivery_journal`; composite FKs `(tenant_id, target_id)`, `(tenant_id, executor_id)`, journal `(tenant_id, job_id)`; app has no grant on `executor_id`, `lease_id`, `lease_request_id`, `fence`, `attempts`, no DELETE; definer filters every statement on the organization, another organization's executor id raises 42501, replay scoped to organization and executor, apply-once enforced in the definer; sweep is SECURITY INVOKER and organization-filtered; every gateway per-job route resolves the job under the executor's organization and answers one 404 for missing or foreign jobs before any lease check; credential grant reads the job's own target and returns no secret; `GATEWAY_ROUTE_TESTS` equals the gateway router (7 routes, #64 item 1 met); B's token with A's real lease and fence gets the random-id 404 on all five per-job routes and A's rows stay identical.

Findings (none blocking for isolation):
1. `delivery_0003_queue.py:112-122` sweep result: an apply whose journal holds only `complete` steps (e.g. `bundle-deploy` done, lease lost before the `bundle-run` intent) is labelled `failed / no platform write was attempted` though a write happened. Set `unknown_outcome` whenever any journal row exists for the deployment; "no platform write" only when none. Add a test.
2. `delivery_0003_queue.py:56`: the app can UPDATE `status` and `lease_until`, and the sweep keeps `executor_id`/`lease_id`/`fence` on re-queued or lost jobs, so app code could revive an old lease (same organization only). Harden: a BEFORE UPDATE trigger refusing `status = 'leased'` unless `fence` changes, or null `lease_id` on re-queue/loss.
3. No cross-organization test for request-id replay: B calls lease with A's live `request_id`, assert 204 and A's row unchanged.
4. `plan_id`/`deployment_id` have no keys yet (deferred to 0004/0005, B-2670). Binding on SP2-5/SP2-6: composite `(tenant_id, plan_id)` and `(tenant_id, deployment_id)` FKs and a cross-client test where B's job names A's plan or deployment id.
5. Design section 3 lists `delivery_lease_job(executor_id, tenant_id, ...)`; the branch takes the organization from `current_tenant_id()` (stricter, preferred). Update the design text.
