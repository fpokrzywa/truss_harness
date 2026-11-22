APPROVED

# Job 060 (#69, PR #146) isolation review 1

Reviewer: isolation-reviewer (read-only; verdict saved by the lead). Scope: `git diff origin/main...HEAD` at `3e0240b`: `delivery/plans.py`, `delivery/repository.py`, `tests/delivery/test_approvals.py`, `docs/build/DECISIONS.md`. No routes, migrations or grants changed. Code read only.

1. `service_account_owner` (`delivery/repository.py:597-607`) filters `id = :p AND tenant_id = :t` with `:t` = the request scope's tenant; `shifde.service_accounts` has forced RLS with `tenant_isolation` USING + WITH CHECK (`identity_0001_identity.py:272-282`). Another organization's account returns None (no refusal fires, nothing leaks). Not SECURITY DEFINER, no owner engine; same pattern as `runtime/repository.py:295-307` `initiator_controllers`. Both ids are server-side (caller principal, plan row `requested_by` read under the caller's tenant).
2. Denial audit (`plans.py:158-170, 409-413`): `tenant_id=self.sc.tenant_id`, on a row `_get_row` already proved is the caller's tenant; only reached after `delivery:plan:approve` passed on an own-tenant plan; details are `decision` and `reason` only; the commit before the 403 holds only that row.
3. Permission before settle (`plans.py:403`): `_require` on the tenant-scoped `_get_row` with `hide_unless="delivery:plan:read"`; a foreign or unreadable id gets the same 404 as unknown and now causes no settle commit (fewer cross-client side effects). `_settle` acts on `self.sc.tenant_id` only; the locked re-read is tenant scoped, and tenant/workspace/project do not change in a settle.
4. Cross-client coverage: approve/reject map to `test_a_cannot_reach_bs_plan` (`tests/delivery/test_routes_cross_client.py:129-130, 442-458`); new tests cover the related-principal, audit and ordering cases. No same-id foreign service-account test; covered by the tenant filter plus forced RLS, not a blocker.

Findings: none.
