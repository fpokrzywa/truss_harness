# Isolation review 1: job 030 (SP3-5 ledger/runtime reservations, PR #88, head 79b62e32)

Reviewer: `isolation-reviewer` (opus), 2026-11-20. Filed by the lead from the agent's reply (the agent is read-only).

**APPROVED.** No path from one client to another client's holds, usage rows or runs found. The reviewer ran `tests/schema/test_schema_vs_seth.py`, `test_rls_schema.py`, `test_function_schema.py`, `tests/ledger/test_reservations_cross_client.py` and `tests/runtime/test_orphaned_holds.py` in the worktree: 203 passed.

Checked and correct:
1. `ledger_0003`: RLS stays enabled and forced; `tenant_isolation` untouched; only new grant `UPDATE (provider_request_at)`; no DELETE; both partial unique indexes lead with `tenant_id`; owner CHECK requires exactly one of `run_id` / `invocation_id`.
2. `runtime_0004`: `settle_ended_run` CREATE OR REPLACE from the runtime_0001 body (not redefined since), SECURITY INVOKER, `search_path = pg_catalog, pg_temp`, only edit `provider_request_at IS NULL` in step 3 (still `tenant_id = r.tenant_id`).
3. `ledger/reservations.py`: every statement keyed on `tenant_id` plus hold/run id; `settle_orphaned` joins `st.tenant_id = res.tenant_id`; usage row written in the hold's own client.
4. `runtime/worker.py:436-447`: reaper settle takes `tenant_id`/`run_id` from the reaper function, sets the client before `settle_orphaned`.
5. `models_admin/service.py:584-608`: invocation hold id generated server-side; reserve/mark/settle/release keyed on `self.sc.tenant_id`; no route takes a hold or invocation id.
6. `ledger/budgeting.py:110-127`: direct-invocation held amount filtered on `res.tenant_id = :t` (cross-client test: `used_and_held` returns `(0, 40)`).
7. `deliberate_changes.yaml:702-751` (lead, B-2530): the 10 entries declare exactly the differences ledger_0003 makes; schema comparison reports both undeclared and stale entries.
8. `tests/ledger/test_reservations_cross_client.py`: as app role and schema owner, A's calls with B's ids change nothing in B; B's invocation holds invisible under A; positive control. No routes added.

Non-blocking (within one client):
1. `models_admin/service.py:1230-1320`: a marked direct-invocation hold is never settled if the API process dies or `adapter.generate` raises other than `AdapterFailure`; it stays `held`, counts against the client's own budgets indefinitely and `stale_holds` no longer reports it. Filed as a follow-up issue.
2. `ledger/budgeting.py:105`: a rule without `id` silently counts no direct-invocation holds; assert the id. Filed with item 1.
3. `ledger_0003` downgrade deletes direct-invocation holds and rewrites `settled_unknown` to `settled` (documented destructive; back up first).
