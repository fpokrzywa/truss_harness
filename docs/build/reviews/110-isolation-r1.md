# Isolation review: job 110 (PR #222), round 1 head 9ae3c00

Reviewer: isolation-reviewer (lead session 6975aeb6, 2026-11-22). Verdict: APPROVED, 0 blocking.

Checked: connections_0003 table forced RLS + tenant policy, app role SELECT/INSERT/limited UPDATE, no DELETE, composite FK; enqueue checks connection lookup + `connection:use` (+`connection:write`), queries keyed on tenant; idempotency key per connection and caller, replay by another requester 404; worker rebuilds requester and rechecks permissions under the job's organization; status route filtered by organization, connection and requester with one 404; `test_a_cannot_read_bs_invocation` in ROUTE_TESTS; no protected test or core change.

Non-blocking, sent to round 2 by lead decision B-2999: (1) worker should use the claimed invocation row's connection_id and refuse a mismatch; (2) add a cross-client worker test for `connection.invoke`.

## Rounds 2-3 (b7c0690, 149f288, 5473f15)

Verdict: APPROVED, 0 findings. Credential-header refusal runs before any write; use-only requester read keeps the tenant/connection/requester filter and one 404; worker refuses a payload whose connection or invocation id differs from the claimed row (`test_worker.py` forged-id test as app role and owner); `connection.invoke_queued` audit holds ids only; no RLS schema, startup-role or `tests/schema` change. Query/body values may still sit in `jobs.payload` (same organization): follow-up #224 (B-3000).

## Round 4 (1508dab)

Verdict: APPROVED, 0 findings. `test_adapter_hooks.py` now uses the queued invoke and keeps every assertion (result, 422 on DROP, `normalize_config` never called, no token in any answer, B's owner gets 404 on test, discover, invoke, GET and PATCH). httpx and httpcore are held at WARNING by the logging setup and the executor, so no request URL is logged. The merges of origin/main conflict only in the `connections/api.py` import and the DECISIONS row order. No cross-client, RLS schema or startup role check test removed or weakened.
