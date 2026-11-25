# Isolation review 1, job 189 (#374, PR #392), head 80aebab4

APPROVED

Isolation review 1, job 189 (#374, PR #392). Head 80aebab4 (equals the PR head), merge base 831711f4. I found no cross-client or cross-project leak in the diff. Nothing that was enforced before stops being enforced. No finding blocks.

Paths are relative to S:/Development_2026/shi_work/agt_trus_harness/.worktrees/189-masked-keys-filter-mask/backend.

## Findings (all non-blocking; 1-3 existed before this diff)

1. LOW, left by the spec. src/shifde/tenancy/service.py:590-600 (`_readable_project`, used by `GET .../workspaces/{ws}/projects/{proj}`).
   - Scenario: a member whose only grant is at another workspace of the same client GETs `/workspaces/<guess>/projects/nope`. A hidden workspace answers `project not found`; an unknown slug answers `workspace not found`. The same workspace-slug oracle that item 3 closes for writes is still open on this read.
   - It stays inside one client. Organization resolution and the `tenant_id` filter come first, so another client's slug is simply unknown.
   - The issue and intake limit item 3 to writes, and B-4093 records the gap openly.
   - Fix (follow-up issue): apply the `_workspace_visible` translation in `_readable_project` too.

2. LOW, left by the spec. src/shifde/tenancy/service.py:708-718 (`update_project` in a visible workspace).
   - Scenario: a P1-only member (`main` is visible as ancestor) PATCHes `main/projects/<guess>`. An existing hidden P2 answers `resource not found`; an unknown slug answers `project not found`. The member can therefore tell that a sibling project exists.
   - The behaviour is the same as before, and B-4093 records it.
   - Fix (same follow-up): for a visible workspace, raise `project not found` for the PDP's `NotFound`.

3. INFO. Timing of the three writes: a hidden workspace costs a few more statements, plus one `policy_decisions` insert at close, than an unknown slug. This was so before, and spec requirement 9 requires the denial row. No order-of-checks oracle: body validation runs before any lookup on both sides. The tenant-active, workspace-active, `_patch_values`, row-version and slug-conflict checks all run after authorization.

4. INFO. src/shifde/tenancy/service.py:805-813: for a non-administrator the dashboard reads every project id of the visible workspaces and passes them as an `IN` list. Query count is bounded and the list is client-filtered. Cost is linear, and an extreme client (tens of thousands of projects) could reach the bind parameter limit. That would fail with an error, not a leak.

## What I checked

**Mask (B-4094)**
- `workspace_ids_of_client` filters `tenant_id` explicitly besides RLS and uses bound parameters. It runs no query for an empty set.
- `_mask_workspace_ids` has two callers: an intersection in `granted_workspace_ids` and a union in `ancestor_workspace_ids`. Both can only shrink.
- `authorize`, `visible_scopes` and `effective_permissions` are untouched.
- Every consumer of `ancestor_workspace_ids` was read. `_require_some_grant` (service.py:217) becomes stricter: a key masked only to a foreign or stale workspace id no longer makes the organization visible.
- The test covers a foreign id, a mixed mask, an unknown uuid, the same principal inside B with and without a grant, and the query under an owner session (no RLS), so the explicit filter itself is proven.

**Dashboard (B-4092)**
- The predicate is in SQL before `ORDER BY`/`LIMIT`. `tenant_id` stays a condition of the statement, with the new `OR` nested inside an ANDed `where`.
- Project rows need both a readable project and a visible active workspace. Rows without a project need `workspace:read` through `visible_scopes`, so ancestor-only and project-mask-only workspaces do not count.
- The visible set `activity_ws_only` is unchanged, so the new rows are a subset of the old ones for every non-administrator.
- Both new arguments default to empty, so calls fail closed. The only caller of `audit.recent_activity` is `dashboard`.
- `pdp.authorize("project:read")` gets the workspace id from the database row and applies key scopes and mask.
- `is_admin` is computed as before; a masked key cannot reach it (`resource_scope`).
- A source scan found two audit writes with a workspace and no project, `tenancy.workspace.created` and `_lifecycle("workspace", ...)`. So project labels do not ride on project-less rows.
- Counts, scope and schema are unchanged.

**404**
- The same `NotFound` class (status, code, title) and the same text are used for all three writes, for an existing or unknown project.
- `Forbidden` is not caught, so a 403 for a visible workspace or readable project stays.
- `pdp.require` still runs first, so the denial is still recorded (tested for `workspace:manage` and `project:manage`).
- The success path of a `project:manage` holder without `workspace:read` is kept and tested.
- `same_404` ignores only `instance` (the caller's own path) and `request_id`.
- `state()` checks prove no write happened.

**The changed assertion** (tests/mcp_access/test_project_key.py, renamed test)
- The member's `/dashboard` and `?workspace=main` go from `== everything` to `== in_p1`. Both sides stay exact equality. `len(everything) > len(in_p1) > 0` directly above keeps it a strict, non-empty subset.
- The administrator assertions and the `project="sample"` assertion are untouched.
- Strictly stricter; no cross-client test was changed, removed or skipped.
- `KEY_SCOPES` gains `project:manage`. That widens the key under test, so the mask rather than a missing scope is what refuses.
- The audit helper `_record` only gains an optional argument.

**Other charter points**
- No migration, RLS policy, grant, SECURITY DEFINER, engine, route, worker, cache, OpenAPI, frontend or protected-path change (11 files).
- No new route, so no new cross-client route test is due; existing cross-client tests pass.

**Tests run (read-only)**
- `pytest tests/authz tests/tenancy tests/mcp_access tests/audit/test_service.py -n 8`: 295 passed in 120 s. Worktree clean afterwards.
- I did not re-run the builder's 15 mutations. I checked by reading that the new tests would fail under the main ones (unfiltered mask, dropped `tenant_id`, old activity filter, old texts).

Recommendation: file one follow-up issue for findings 1 and 2, so the remaining slug oracles recorded in B-4093 have an owner.
