APPROVED

# Job 096 (CM-1, #159, PR #203): lead isolation review, 2026-11-22

Reviewer: isolation-reviewer (opus), read-only, diff `origin/main...factory/096-cm-1-connections-models`.

No way found for one organization to reach another's data, or one project another's.

1. `connections/repository.py:46` `include_shared`: replaces only the exact workspace/project filter with "this project's rows, the workspace's own rows, client-wide rows". `c.tenant_id = :t`, forced RLS, type/status filters, cursor and the grant visibility check (line 159) still apply. Client-wide rows show only to callers with an organization-wide grant. Slugs resolve inside the caller's organization; `include_shared` without `workspace` is 422. Covered by `test_scope_isolation.py:279`.
2. `repository.py:36` `last_test_outcome` lateral join matches `t.tenant_id = c.tenant_id AND t.connection_id = c.id`; `connection_tests` has forced RLS and a (tenant, connection) foreign key.
3. `connections/service.py:336` secret scope check runs after `_check_secret` (org-scoped lookup: 404 missing/hidden, 403 without `secret:use`), so the 422 concerns only a secret the caller can already see. PATCH checks `connection:manage` first. `models_admin/service.py:361`: 422 after the org-scoped lookup and `connection:read`.
4. `service.py:89-99`, `models_admin/service.py:386` SQLSTATE mapping: fixed guard text only, no ids or values; database detail never used; unknown errors re-raised.
   - Non-blocking: every 23514 maps to code `secret_scope_mismatch`, including the egress-policy and vendor-visibility guards. Mislabel, no leak.
5. RLS schema test, startup role check, migrations and `backend/tests/schema` unchanged. `test_cross_client.py` only adds `include_shared=False`; `test_routes_cross_client.py` only a docstring. `test_routes_cross_project.py` asserts cross-project 404 on every `connection_id` route.
   - Non-blocking gap: no cross-client test that `include_shared=true` never returns organization B's rows, or that B's workspace slug is 404 under A; same for `last_test_outcome`. Follow-up issue filed.
