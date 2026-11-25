# Isolation review 1: job 180 (#273, PR #370), head 3d7c863

APPROVED. Isolation review 1, job 180 (#273 SP6-6, PR #370), head 3d7c863 against origin/main. No cross-client finding. Finding 1 is a same-client scope leak that I recommend fixing in this PR; it does not block on cross-client grounds. The verdict rests on my reading of the diff plus a partial test run (see Tests).

Question 1: can one client's data reach another? No.
- Scope: the diff has no migration, policy, grant, SECURITY DEFINER or route change; openapi gains one `error_code` enum value. `request` still resolves the target (`plannable`), then the release (`scoped_release`), both filtered by organization and permission, before any check or write. The old pre-release Snowflake refusal is gone, and an unreadable release still answers `release not found`.
- Pins: `resolved_for_release` reads pins under the requester's scope; B's pin gives `release_source_changed` without B's id or hash. `provider_destination` filters on the tenant explicitly as well as by RLS. Unknown, B's, shared and unreadable-connection providers give one 409.
- `base_url`: it is only compared. No renderer, preview response, audit row, plan row or artifact carries it (`render_snowflake` reads only `model.serving_endpoint`; preview returns tools and screens).
- Verify: `_required_checks` reads target, plan and release by the deployment's own tenant_id and fails closed on each. Executor tenant checks, grant audience and key-pair broker are unchanged. The `release_digest` compared comes from the leased plan's context.

Question 2: does anything address outside the plan's own account or organization? No.
- `_in_account` needs `https` and exact equality with the target's normalized address. User info, odd port, other suffix and privatelink mismatch are refused. A `fake` provider passes only in development and test.
- Deep-health and health SQL use names quoted from the plan's target. Table names must be three plain parts, checked at render and again in the handler.
- Fake: data accounts are keyed by lowercased host. The handler runs the installed patch's own stage directory under a process lock, is never put in `sys.modules`, and `_xoptions` is restored. File names are flat and looked up in the staged dict only. The fake still refuses any other environment.

Question 3: does a check that refused before now pass? None wrongly.
- Snowflake plans now pass by design, behind the kind, Cortex model, provider and generator gates. A `/1` Snowflake release never reaches the template. The kind gate also refuses Snowflake releases on Databricks targets. The kind check moved after `evaluation_required`, still before any plan, artifact or job write.
- Deep health is stricter: no release digest or an empty `checks` list fails. Databricks required checks are unchanged.

Question 4: are tests weakened? No. `test_release_gate` covers more kind/platform pairs; `test_targets` only changes the code (409 and no-write kept); adapter verify tests now pass the sealed digest. The removed stub test asserted artifact digest == release digest, which B-4044 corrects. The four new cross-client tests run in both directions, compare with unknown ids and check the other side's rows unchanged.

Findings
1. (low, same client, newly reachable; S:/Development_2026/shi_work/agt_trus_harness/.worktrees/180-sp6-6-delivery-plans/) `backend/src/shifde/delivery/plans.py:478,485` with `backend/src/shifde/appgen/render_snowflake.py:95`.
 Scenario: a requester may read the release and request the plan but cannot see one pinned tool (another person's private tool, or no `tool:read`). `resolved_for_release` deliberately skips visibility, and render's 409 `release_not_renderable` then echoes the tool key and an SQL `:name`. Run against `_tool_sql`: "the SQL of tool 'hidden_tool_key' is refused: :secret_param is not a declared parameter".
 Reachable because publish applies `statement_rules` only (`backend/src/shifde/build/tools.py:485`), not `parameter_rules`, so a Snowflake tool with an undeclared `:name` publishes. I did not reproduce it through the route. Preview refuses this case first (`release_pins_hidden`, `backend/src/shifde/appgen/service.py:531-535`); #316 item 3 is the same class and was a follow-up. Render's Databricks branch does not echo it.
 Fix: in `_snowflake_artifact`, catch `ReleaseNotRenderable` and re-raise it with a fixed detail (or only when a pinned version is not visible). Add a test that makes the tool private with an undeclared `:name` and asserts 409 `release_not_renderable` without the key, parameter or SQL. If not fixed here, file an issue.
2. (note) `backend/tests/delivery/test_routes_cross_client.py:1154`: the own-object-first check accepts 200, 403 or 409 for every call. It still excludes 404, so it is not vacuous, but exact expected statuses per call would stop a route that always answers 403 from satisfying it.

Tests
- `tests/delivery/test_routes_cross_client.py -k snowflake`: 11 passed.
- A wider run (that file plus the generated app, gateway cross-client, runner and fake files) was stopped by my 580 s limit with no result, while another session was running tests in the same worktree. I have no pass or fail for those files; rely on the gate run.
- The worktree is clean; I changed no repository files.
