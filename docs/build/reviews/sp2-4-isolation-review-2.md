APPROVED

# SP2-4 isolation review 2: PR #65, job 022, `939c2ec..origin/factory/022-sp2-4-delivery-job` (round 2: ae2a5f3, 73069a2, e3be89e) plus all of `delivery_0003_queue.py` at e3be89e

(Saved by the lead from the isolation-reviewer's report; the reviewer runs read-only. Condensed.)

Checks:
1. Sweep (`delivery_0003_queue.py:120-129`): any journal row for the job's own `(tenant_id, deployment_id)` gives `unknown_outcome`; "failed / no platform write was attempted" only with none; SECURITY INVOKER under forced RLS, tenant-filtered. Correct, no cross-client signal.
2. Lease guard `shifde.delivery_jobs_lease_guard()` (lines 135-146, 207-210): SECURITY INVOKER, pinned search_path, reads only OLD/NEW, writes no `app.*`. Refuses queued/lost/done/cancelled to leased and extending an expired unswept lease unless `fence` changes (app has no grant on `fence`). Definer (fence+1), sweep, heartbeat (same transaction timestamp), checkpoint and finish unaffected. Apply-once still held by `attempts < max_attempts`.
3. New tests prove their claims (complete-only journal distinguishes old and new rule; revival refused with 42501 specifically); the replay test is correct but weak (finding 1).
4. Nothing weakened; no protected or schema file touched in round 2.
5. `LEASE_JOB` text unchanged; the B-2671 allow-list entry is still exactly correct, add it verbatim. The guard function (not a definer) needs no entry.

Findings (none block isolation):
1. `tests/delivery/test_gateway_cross_client.py:189-202`: B's job is not due, so B gets 204 either way and the test cannot detect an organization-unscoped request-id lookup. Make B's job due (`_due_now`), assert B gets 200 with its own job and A's row unchanged.
2. `delivery_0003_queue.py:138-140`: while a lease is unexpired, `shifde_app` can push `lease_until` arbitrarily far ahead (same organization only, pre-existing). Optional: with `fence` unchanged, refuse `NEW.lease_until > now() + interval '45 seconds'`.
3. Design section 4 still states the old unknown-outcome rule; align it with B-2672.
4. Binding carried forward (B-2673): SP2-5/SP2-6 add composite `(tenant_id, plan_id)` and `(tenant_id, deployment_id)` keys with cross-client tests.
