# ARCH M2 (job 012, #10) isolation review 1: APPROVED

Reviewer: isolation-reviewer (opus), 2026-11-18, diff `origin/main...factory/012-arch-m2-move-adapter` (4 commits, 24 files).

1. Payload not trusted: worker uses `tenant_id` from `shifde_claim_jobs`, sets that client's context, re-reads the job by `tenant_id` and kind, re-derives the principal with `authz.execution_principal` (active membership/service account, same-client unexpired unrevoked key, `is_platform_admin=False`), loads the connection with `_get_row` (tenant filter + RLS) and re-checks `connection:test` (`connections/service.py` `run_test_job`). Payload written only by the server.
2. Claim/dispatch by kind (`runtime/worker.py`); unknown kind ends `dead`; settle/finish/heartbeat filter by `tenant_id` + `lease_owner`; no migration or grant changed.
3. Status route checks `connection:read`; job query filters by id, `tenant_id`, kind and the path connection; same 404 as unknown id; `error` only exposes `[a-z][a-z_]*` codes, else `worker_lost`.
4. Dedupe conflict target `(tenant_id, kind, dedupe_key)`; read-back filtered by `tenant_id`.
5. Secrets resolved in `execute_test` under the job's client and re-derived principal; the API no longer calls the adapter.
6. Stale leases closed by the reaper's existing branch (job id + `tenant_id`); a late abandoned thread writes only its pre-allocated `test_id` in its own client.
7. Tests: new route in `ROUTE_TESTS` with `test_a_cannot_read_bs_test_job`; cross-workspace hidden-connection case; `_b_state` compares B's job queue. Nothing under `tests/schema`, startup role tests or migrations changed; no cross-client assertion removed (only 200 -> 202 and the job helper).
8. Non-blocking (within one client): a second user posting during an active job receives the first requester's job, run as the first requester; already authorized for `connection:test` and can read results through `GET .../tests`. Record in DECISIONS.
9. Non-blocking, pre-existing: neither the old nor the new path checks `_active_tenant` (archived org); not an isolation issue.
