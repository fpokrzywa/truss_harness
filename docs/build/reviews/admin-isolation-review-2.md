# Admin part 1 (IAM routes): isolation review 2

Diff `9043783..31f1b0b` (fix round 1, branch `admin`). Reviewer: isolation-reviewer (opus), resumed for round 2, 2026-11-14. Saved by the lead.

**Verdict: APPROVED** (0 blocking, 3 follow-up). Clean detached worktree at `31f1b0b`, `-n 6`: `tests/admin tests/authz tests/tenancy tests/audit tests/schema` 360 passed; `tests/core/test_skeleton.py tests/vendors tests/secrets tests/identity` 230 passed (incl. `test_every_migration_downgrades_and_upgrades_again`).

## What holds

- **Finding 1 closed for manual membership.** Probe order: the administrator creates "Support", adds themselves and B's member; A's owner binds it as viewer: one `tenancy.platform_admin.self_added` row in A (target the administrator, `through_group_id` the group), no rows in B mentioning the group. Remove and re-add while bound: a second self-add in A. Pinned by `test_administrator_joins_first_then_owner_binds_in_a_and_a_shows_the_self_add` (also covers a second administrator added later).
- **`shifde_bound_group_admin_ids(uuid)` (tenancy_0004)** as `shifde_app`: empty with no client, in B for a group bound only in A, in A before A binds, for a random id; after the bind returns only the platform administrator's id. Ids only. SECURITY DEFINER, search_path pinned, EXECUTE revoked from PUBLIC and granted to `shifde_app`, allowlisted by signature; the downgrade drops it.
- `_record_group_self_adds` refuses unless the database's current client is the target (B-703). `bind_group` skips the owner-administrator (no duplicate); `bind_group_as_platform_admin` records every administrator in the group.
- **Follow-up 2 closed:** per-client `members_changed` rows carry `added_count`/`removed_count` only; installation row keeps the ids.
- **Follow-up 3 closed:** installation actions also require no client context read from the database; the changed `tests/audit/test_cross_client.py` test is strictly tighter (adds a refusal with the flag and client A set, keeps the no-flag refusal, success moved to no client); a parametrised `test_service.py` test covers `identity.group.updated`.
- **Follow-up 4 closed:** `core.db.forget_tenant` clears only the session-side context on an aborted transaction.

## Follow-ups

1. **IdP sync is a live path** (owner: identity; fix before `app` mounts the OIDC routes). `identity/oidc.py:472` calls `authz.sync_idp_groups` at sign-in: a platform administrator synced into an IdP-mapped group bound in A reaches A with no self-add. Fix: `shifde_sync_idp_groups` returns the added group ids; for each, write the per-client `members_changed` row (counts) and a self-add per administrator via `shifde_bound_group_admin_ids` in each bound client's context.
2. **Self-add label** (owner: tenancy). A self-add for another administrator has `target_label` None; label it with that administrator's e-mail.
3. **Promotion path** (owner: identity). No code sets `users.is_platform_admin` on an existing person today; a future path must record a self-add in every client where the person's groups are bound.
