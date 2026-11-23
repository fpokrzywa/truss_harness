# Job 133 (PR #298, SP5-2 appgen skeleton) isolation review 1

Reviewer: isolation-reviewer (opus), 2026-11-23, head `ad1f3ef`. Written by the lead from the reviewer's reply.

**BLOCKED** (no live leak path; two missing tests).

## Holds
- Both tables RLS enabled and forced, one `tenant_isolation` policy (USING and WITH CHECK on `current_tenant_id()`); every FK composite `(tenant_id, ...)`.
- Grants: requests SELECT/INSERT and UPDATE on 8 status/accept columns; drafts SELECT/INSERT only. No SECURITY DEFINER; guards SECURITY INVOKER, `search_path = pg_catalog, pg_temp`, EXECUTE revoked from PUBLIC.
- Requests guard (migration 88-98) requires same tenant and same connection; foreign tenant gets the same `check_violation` (no probe).
- Repository keyed on `tenant_id`; one 404 for unknown / other client / other project; create checks WRITE, `connection:read`, `in_scope`.
- Draft job re-derives requester and re-checks permission, connection, discovery; sends only column names and types.

## Findings
1. No test for `appgen_requests_guard`: as `shifde_app` in A, INSERT with B's discovery id and with another A connection's discovery; assert 23514 (as `tests/connections/test_scope_guard_db.py`).
2. `test_routes_cross_client.py:128-210` tests only A->B; parametrize over (A->B, B->A).
3. (non-blocking) DB ties request to connection but not project/workspace scope; trigger should also require connection client-wide or in `NEW.workspace_id`/`NEW.project_id`, and project in `NEW.workspace_id`.
4. (non-blocking) `revise` locks before the permission check (same-client existence oracle); check on an unlocked read first, then lock and re-read.
5. (non-blocking) `test_routes_cross_client.py:241-244`: compare whole 404 body with `_same()`.

## Round 2 (head `08fca4d`): APPROVED
All five findings fixed: guard DB tests with positive controls (`test_requests_guard_db.py:137-168`); cross-client tests parametrized a->b and b->a (`test_routes_cross_client.py:112-117`); trigger now requires project in workspace and connection client-wide or in the request's workspace/project (`appgen_0001:99-113`, matches `ck_connections__scope` and `in_scope`; SECURITY INVOKER, pinned search_path); permission checked before the row lock (`service.py:316-323`, test `:280-298`); whole-body 404 comparisons (`:265-273`).
