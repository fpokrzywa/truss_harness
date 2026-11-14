# Admin part 1 (IAM routes): isolation review 1

Diff `dcb0f9c..0071471` plus merge `9043783` (branch `admin`). Reviewer: isolation-reviewer (opus), 2026-11-14. Saved by the lead from the reviewer's report.

**Verdict: BLOCKED** (1 blocking, 3 follow-up). Detached worktree at `9043783`: `tests/admin tests/authz tests/tenancy tests/audit -n 6` 162 passed; no authz or tenancy test changed by the diff.

## What holds

- `clients_binding_group` (tenancy/service.py:1063) refuses a caller without the admin flag and clears the client context in `finally`; per-client rows carry only group id, group name, change and actor; the test checks no other client's id or slug appears.
- `/platform/groups*`: `require_platform_admin` runs before any lookup; the client owner gets the same 403 for A's, B's and a random group id.
- `POST /orgs/{org}/users`: one 403 for A's slug, B's slug and an unknown slug; another client's role or a platform-only role gets 422 `role not found`; the membership and audit row are written in that client only; an existing e-mail is a Conflict.
- `/orgs/{org}/groups[/{id}]` returns only `{id, name, role_bindings}`; a group bound only in B gets the missing-id 404.
- PATCH users: B's member gets the missing-id 404; a person in both clients is suspended in A only; reactivation counts every group bound in the client (fails closed).
- B-1405: a direct binding needs an active membership, read under RLS; the new test fails without the change; group grants unchanged.
- Roles: platform-only keys 422 before insert; the `platform_administrator` template is left out of lists, 404 on read, 422 on bind; naming an owner goes through `may_name_owner`.
- Cross-client tests: `ROUTE_TESTS` equals `api.router.routes` (19); each gets 200/201 on the own object first, then compares the refusal to the missing-id/org body.

## Findings

1. **BLOCKING** (owner: admin + tenancy). A platform administrator reaches a client through a group with no self-add record. `admin/iam.py:1038` computes `self_added` only for clients bound at that moment; `tenancy/service.py:929` (`bind_group`) records a self-add only when the binding owner is a platform administrator. Probe: the administrator creates group "Support" and joins it while it is bound nowhere (installation row only); owner_a binds "Support" as viewer in A (the owner cannot see members, B-1401); the administrator's `GET /orgs/A/me/permissions` returns 200 with workspace:read, project:read, run:read; A's audit log has 0 `tenancy.platform_admin.self_added` rows. The existing `lonely` group test pins this gap. Making an existing group member a platform administrator, and IdP sync, are the same hole. Fix: when a group binding takes effect (`bind_group`, `bind_group_as_platform_admin`), write `self_added` in that client for every platform administrator who is a member, read through a definer that returns only administrator ids for a group bound in the current client (migration + allowlist); until then refuse a platform administrator as a manual group member; test the order "administrator joins, then owner binds".
2. **Follow-up** (owner: admin). `iam.py:1035-1036`: each client's `identity.group.members_changed` row lists added/removed user ids, including people who may belong only to other clients; conflicts with B-1401. Fix: per client record counts plus the self-add only, or amend B-1401/B-1403.
3. **Follow-up** (owner: audit). `audit/service.py:245` (vendors review 1 follow-up 2, now also `identity.group.*`): with `platform_admin_context` on and tenant A set, a NULL-tenant `identity.group.updated` row was written. Fix: also require `_has_no_client_context(session)` (from the database, B-703) for `ADMIN_INSTALLATION_ACTIONS`. No current caller does this.
4. **Follow-up** (owner: tenancy). `tenancy/service.py:1085`: on an aborted transaction `set_tenant(None)` in `finally` raises `InFailedSqlTransaction` and hides the original error (no leak). Fix: if `not session.is_active`, only `forget`/`replace`, as `platform_admin_context` does.
