# 097 (CM-2, #160, PR #207) isolation review

Reviewer: isolation-reviewer (opus), lead session after 7d438bdc, 2026-11-22. Verdict: **APPROVED**. No migration in the diff.

1. `PUT .../credential` (connections/service.py:339): `_get_row` filters `id` and `tenant_id` and re-checks the tenant; then `connection:manage` (hidden unless `connection:read`), then tenant-scoped `secret:manage`. Another organization's id gives the unknown-id 404 before any secret lookup.
2. Rotate branch: `get_secret(..., tenant_id=self.sc.tenant_id)` filters by tenant (secrets/repository.py:20).
3. Owned-secret create: tenant from the session scope, server-generated name `connection-<uuid7>`, unique per tenant (`uq_secrets__tenant_name`).
4. Archive: secret lookup, name match and `count_live_bindings` all filter `tenant_id`; RLS applies on top.
5. Inline create: tenant from scope; `credential` and `secret_id` together refused; `secret:manage` on the scope ref.
6. Admin prefix guards (admin/providers.py:513, 555, 576) run after the tenant-level check and tenant-filtered 404.
- Security note 3 (PATCH `secret_id` binding another connection's owned secret) cannot cross organizations: tenant-filtered `_check_secret`, forced RLS on `shifde.secrets`, composite FK `fk_connections__secret (tenant_id, secret_ref_id)` + `trg_connections_guard`, and `_check_secret_scope`.
- Tests: new route in `ROUTE_TESTS` of tests/connections/test_routes_cross_client.py (coverage enforced); cross-project and cross-workspace cases with secret snapshots.
- Optional (filed as a follow-up): a cross-client case where B PATCHes its own connection with `secret_id` = A's owned `connection-<id>` secret and gets the unknown-secret 404.

Lead (acting owner approval) accepts the escalation points: `secrets/service.py` gains only a public `check_value` wrapper; credential rotation at connection-scope `secret:manage` is the ruling of B-2749.
