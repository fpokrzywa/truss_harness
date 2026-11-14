# Tenancy part 2b (routes, dependencies, tenancy_0003): isolation review, round 3

Diff `906e719..b291be3` (branch `tenancy`; head `280474f` adds only fixtures and docs from main). Reviewer: isolation-reviewer (opus), 2026-11-14. Saved by the lead from the reviewer's report.

**Verdict: APPROVED** (0 blocking, 4 follow-up). In a clean detached worktree at `280474f`, `tests/tenancy tests/identity tests/authz tests/schema -n 8` gave 469 passed. Four extra probes ran as `shifde_app` on scratch databases on 55450 and passed; no scratch database was left.

## What holds

- **`resolve_org`.** A person's client comes only from `shifde_my_memberships`; a pinned session or personal key reaches only its pinned client; a service-account key gets its key's client only when the slug matches, else the client is cleared. `set_tenant` never takes a value from the path or a header. Probes:
  - a personal key of a person in A and B, minted in A, gets 200 on A's workspaces and the missing-client 404 for B, and the org list is {A};
  - group-only staff reach A but not B, and after the membership is suspended A and A's dashboard give 404;
  - an unpinned platform administrator gets 404 on `/orgs/A` and A's workspaces.
- **Identical 404s.** An unknown client, another client, and `get_org` with no grant all return `organization not found`; `_same_404` compares the whole body except `instance` and `request_id`. B's workspace and project slugs under A's org answer as missing ones.
- **PDP checks.** The PDP is built only for the resolved client, so denial audits are written there only. `get_org` checks for some grant (`_require_some_grant`); every other route goes through `require`, `visible_scopes` or the hiding helpers; lists filter by `scope.tenant_id` plus visible ids.
- **Who may name an owner (`tenancy_0003`).** `shifde_is_client_owner` is false, never NULL, for a NULL argument, no client context, no principal, the other client's context, and A's id asked from B. The trigger refused naming an owner by: an expired owner binding, a suspended owner membership, a workspace-scoped `tenant_administrator`, a service account holding it, group-held staff, a viewer, B's person, and the schema owner with no context; all were refused group binds too. `shifde_app` has no UPDATE on `role_bindings`. `authz.grant_role` checks the same rule first (`may_name_owner`).
- **Suspended membership.** `shifde_active_group_ids` returns nothing once the membership in the current client is not active, and nothing for B's staff asked from A; test checks the positive case first.
- **Platform-admin self-add through a group.** `bind_group_as_platform_admin` records `self_added` in that client only when the administrator is in the group; `bind_group` records it when the acting owner is a platform administrator (reads the person's own `users` row, so it works on a pinned session). An administrator later joining a group already bound in a client is tracked in carry_forward (admin row).
- **SECURITY DEFINER hygiene.** The four tenancy definers are owned by `shifde_owner`, search_path `pg_catalog, pg_temp`, EXECUTE only for `shifde_owner` and `shifde_app`, allowlisted by signature; `check_role_binding` stays invoker.
- **Downgrade.** Function bodies, ACLs, proconfig, security mode and owner after downgrading to `tenancy_0002` match a fresh 0002 exactly; re-upgrading matches the original head.
- **Failure paths.** `test_failures_after_the_client_context_is_set_clear_it` injects a failure after `set_tenant` in `create_org`, the self-add and the admin group binding; tenant NULL and admin flag off in Session and database (review 2 item 5 closed).
- **Changed existing tests** are as strict as before (`bind_user`/`_bind` raise the admin flag only for the `tenant_administrator` insert; `test_schema` adds a "not a head" assertion; allowlist only gains entries).
- **Route tests are not vacuous.** `test_every_tenancy_route_has_a_cross_client_test` pins all 14 routes; each gets the own object 200/201 first, then B's under B's org and under A's, then reads B's rows directly to confirm they are unchanged.

## Follow-ups

1. **PDP counts direct user bindings when the membership is suspended.** Owner: authz. `authz/repository.py:60-90` (`load_grants`), accepted in B-1018. A future route or worker building `new_pdp` without `resolve_org` (admin, runtime) would grant a suspended member their direct bindings; nothing leaks today. Fix: require an active membership for `subject_type='user'` in `load_grants` and `bound_roles`.
2. **Route cases covered only by probes.** Owner: tenancy. Add to `tests/tenancy/test_cross_client.py`: a personal API key asking for B; group-only staff on a route and 404 after suspension; an unpinned platform administrator on `/orgs/{a}` and `/orgs/{a}/workspaces`.
3. **Uneven hiding of a client with no grant** (same client, not cross-client; low). Owner: tenancy. `tenancy/service.py:591` (`dashboard`) gives 200 with slug and zero counts where `get_org` gives 404; `:287`, `:312` give 403. Fix: `_require_some_grant` once in `resolve_org`.
4. **Contradicting audit details** (cosmetic). Owner: tenancy. `tenancy/service.py:941-950`: `authz.group.bound` says `by_platform_administrator: true` while the paired `self_added` says `false`.
