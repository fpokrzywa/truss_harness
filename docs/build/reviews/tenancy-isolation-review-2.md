# Tenancy part 2a (service port, tenancy_0002): isolation review, round 2

Branch `tenancy` `121346b..2c3b4b4`. Reviewer: isolation-reviewer (opus), 2026-11-14. Saved by the lead from the reviewer's report (the reviewer is read-only).

**Verdict: APPROVED** (0 blocking, 7 follow-up). 444 tests passed (`tests/tenancy tests/identity tests/authz tests/schema -n 8`, clean detached worktree at 2c3b4b4). Probes ran as `shifde_app` on scratch databases on 55450.

## What holds

- **Creating a client.** The RESTRICTIVE `insert_by_platform_admin` policy refuses a client context's INSERT on `tenants`, including `ON CONFLICT DO NOTHING` with B's slug, so the slug check reveals nothing. The slug cannot be updated (permission denied). An UPDATE of B's row from A touches 0 rows.
- **`shifde_may_bind_group`.** Owner `shifde_owner`, search_path `pg_catalog, pg_temp`, EXECUTE for `shifde_owner` and `shifde_app` only, allowlisted by signature. It answers false for B from A's context, for A's owner while in B's context, for an unknown group and with no principal.
- **The trigger refuses every cross-client binding.** When A's owner binds a group in A's context, the trigger refuses B's custom role, B's workspace or project as scope, the `platform_administrator` template, a row keyed to B, B's user and B's service account. A group-held `tenant_administrator` cannot bind a group.
- **Visibility (B-1009).** Group-only staff list A, and their pin to A resolves. A disabled group, an expired binding, an archived client or a suspended membership hides A and ends the pin. The role-name join is fixed (review 1 item 3).
- **Organization lists.** `list_orgs` reads only `shifde_my_memberships`. `list_all_orgs` checks the principal and the PDP and returns id, slug, name and status only.
- **Session revocation.** `shifde_revoke_user_sessions` refuses B's member and unknown ids with one generic error, revokes only sessions pinned to A, and refuses a platform-administrator target.
- **Failure paths.** A failed self-add (unknown client) and a failed admin group binding (platform template, B's workspace as scope) both leave tenant NULL and the admin flag off, in the Session and in the database.
- **Downgrade.** Downgrading to `tenancy_0001` restores function bodies, ACLs, proconfig and the `tenants` policies exactly as a fresh `tenancy_0001` database has them. Upgrading again reproduces the head.
- **Changed identity tests.** They only add `bind_user` to setup, and the assertions are unchanged. The org-list tests assert the own client positively (`== {A}`).
- **New tests are not vacuous.** Each refusal has an own-client positive case.
- **No routes are added**, so no new route needs a cross-client test.

## Follow-ups

1. **Group-binding check fails open with no client context.** Owner: tenancy (fix now, cheap).
   - `tenancy_0002_service.py`, `MAY_BIND_GROUP` and `CHECK_ROLE_BINDING`: with `current_tenant_id()` NULL the function returns NULL, not false, and `ELSIF NOT NULL` does not raise.
   - Probe: as `shifde_owner`, with no client context and the principal set to A's owner, a group binding into A was inserted without the admin flag.
   - `shifde_app` is still stopped by RLS on `role_bindings`.
   - Fix: `COALESCE(..., false)` in the function and `IS NOT TRUE` in the trigger.
2. **Anyone holding `role:manage` can create an owner.** Owners: authz and admin, before user-binding routes land.
   - The trigger lets any client-context writer bind a member to `tenant_administrator` at organization scope, which makes that person an owner under B-1010.
   - Probe: a viewer member bound themselves, and `may_bind_group` turned true.
   - `role:manage` sits only on `tenant_administrator`, but a group-held tenant administrator can still name an owner, which goes against B-1010's "group-held never counts".
   - Fix: binding `tenant_administrator` at organization scope requires the owner or the platform administrator, in the service and the trigger.
3. **Platform-administrator self-add through a group.** Owners: admin (existing carry_forward row) and tenancy.
   - `bind_group_as_platform_admin` binding a group the acting admin belongs to grants A content without a `self_added` event.
   - A self-added admin then counts as an owner, and binds through `bind_group` with `by_platform_administrator: false`.
   - Fix: record a self-add when the actor is in the group or is a platform administrator.
4. **Org routes must use the B-1009 rule.** Owner: tenancy part 2b (existing row).
   - `TenantScope` must be resolved from `shifde_my_memberships`. `get_org` has no PDP check.
   - `shifde_active_group_ids` ignores a non-active membership, so the PDP still grants through a group after a membership is suspended.
5. **The failed-create test does not test what it claims.** Owner: tenancy.
   - `tests/tenancy/test_service.py::test_a_failed_create_org_leaves_no_client_context` fails on `admin_email` before `set_tenant`, so it never covers a failure after the client context is set.
   - Fix: add a failure after `set_tenant`, for `create_org`, the self-add and the admin bind.
6. **An owner cannot revoke group-only staff sessions.** Owner: tenancy or admin (document it).
   - `shifde_revoke_user_sessions` refuses group-only staff pinned to A, since they are not members. Removing the group binding does end the pin.
   - Document the owner's off-boarding path.
7. **Group-id oracle.** Owner: tenancy (low).
   - `shifde_may_bind_group` tells an owner whether a group id is an active platform group. Ids are uuid7.
   - Accept, or answer only for groups the owner may see.
