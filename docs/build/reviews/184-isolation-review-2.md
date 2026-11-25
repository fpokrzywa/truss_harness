# 184 isolation review 2 (#366, head 8074343)

BLOCKED: isolation review 2, job 184, head 8074343 (merge base 8212720; origin/main 5cd5e1a differs from it by docs only; backend/src is identical on head and base).

Answer: the charge check that round 1 lost is restored, and build.md's mutation claim is true. But six changed tests still accept a wrong-period HOLD, or hold plus charge, that origin/main refuses; one is cross-client. No production code changes and I found no cross-client exposure. This is a loosened test (charter 3), the same class as feedback-1. Round 1 already had it and neither build.md nor decision.md measured it; their mutations only moved usage rows.

FINDINGS
1. backend/tests/ledger/util.py:198-205: `daily_since` takes the window from `min(budget_reservations.created_at)`, which is itself a value under test (`_held_sql` filters on it). A hold written in the wrong period moves the window with it, so `held` is still counted.
   - Scenario: `reserve_step` writes `created_at` 3 days back, so the hold falls outside the day's budget window and the client can overspend a hard budget. shifde_app has table-level INSERT, so the app role can do this. Main: 5 tests fail. Head: 0 fail, and 1621 tests of runtime+ledger+models_admin+assist+appgen all pass.
   - Tests that assert less than main, with the sum that lost its check:
     a. tests/ledger/test_reservations.py:170 test_an_unmarked_hold_past_its_ttl_expires_and_stops_counting_a_marked_one_does_not
     b. tests/ledger/test_reservations.py:407 test_the_invocation_sweep_charges_a_marked_expired_hold_with_the_scope_of_its_rules
     c. tests/ledger/test_reservations_cross_client.py:132 test_under_as_context_bs_holds_are_invisible_and_bs_client_id_reaches_nothing (cross-client; its non-vacuous half `(0, 40)`)
     d. tests/runtime/test_orphaned_holds.py:125 test_a_marked_hold_survives_a_crash_and_its_ttl_until_the_reaper_charges_it_in_full
     e. tests/runtime/test_orphaned_holds.py:125 test_an_unmarked_hold_past_its_ttl_is_expired_and_no_longer_counts
   - With hold AND charge 3 days back: main 12 fail, head 2. Six charge tests pass again on head, including cross-client test_the_invocation_sweep_of_client_a_never_touches_client_bs_holds_or_usage.
   - Fix (tests only): take the window from a row the test writes itself and no ledger code writes: `SELECT min(created_at) FROM shifde.budgets WHERE tenant_id = ANY (:t)`. I ran it in the scratch worktree ("fix" below): it catches everything main catches, with no test lost, and stays midnight-safe. Only test_daily_since_is_the_day_of_the_rows_not_of_the_live_clock needs adapting (it has no budget row). In it, backdate a budget to 2026-03-01 23:59:59 and keep the "reported failure" assertion. Correct B-4065, the util docstring and spec requirement 2.
2. test_reservations.py:145-152 (new test) pins finding 1 as intended ("with a hold it is the hold's day"). Change it with the fix; add the assertion that a hold dated before the window's anchor is not counted.

NOT FINDINGS (checked)
- Wrong client: with the tenant filters of `_used_sql`/`_held_sql` removed, the same 4 tests fail on base and head. With the unknown charge written to another client, the same 5 fail on both. No cross-client assertion was weakened there.
- test_cross_client.py:233: B's window is read as owner; the sum still runs as shifde_app in A's context and fails under nofilter, so it is not vacuous. The two-client sums (:272, test_enforcement.py:179, 194) pass both tenants.
- The "since" filter dropped from used/held: base 1 fails, head the same 1 plus the new test.
- A settle that moves `created_at` cannot pass: shifde_app has no UPDATE on that column, and all 10 base failures are kept.
- No assert, expected value or query scope changed in the diff; no skip, xfail or retry.

MUTATIONS (pytest plugins outside the checkout; failed/passed; base = full checkout of 8212720, r1 = da88321)
| mutation | base | r1 | head 8074343 | fix* |
| none | 0/122 | - | 0/124 | 1/123 |
| unknown charge -3 days (build.md's `mut_unknown`) | 7/115 | 1/123 | 8/116 | 9 |
| every usage row -3 days | 13/109 | 6/118 | the same 13 | the same 13 +1 |
| unknown charge to another client | 5 | - | the same 5 | - |
| tenant filters removed | 4 | - | the same 4 | the same 4 +1 |
| since filter removed | 1 | - | the same 1 + new test | - |
| settle moves created_at and charge -3 days | 10 | - | the same 10 + new test | - |
| HOLD -3 days (reserve_step and reserve_invocation) | 7 | 2 | 2 | the same 7 +1 |
| RUN HOLD -3 days (reserve_step only) | 5 | - | 0 | the same 5 +1 |
| same, five suites (-n 8) | 5/1614 | - | 0/1621 | - |
| hold and unknown charge -3 days | 12 | - | 2 | the same 12, the midnight test, +1 |
| clock a day ahead (spec's shift_midnight) | 18/104 | - | 5/119, the 5 spec names | the same 5 +1 |
*the "+1" (and the 1 unmutated) is the unadapted daily_since unit test.

build.md's mutation, failing ids on BASE (7):
 ledger/test_reservations.py::test_settling_one_unknown_invocation_uses_the_callers_attribution_once
 ledger/test_reservations.py::test_settling_orphans_charges_a_marked_hold_once_and_releases_an_unmarked_one
 ledger/test_reservations.py::test_the_invocation_sweep_charges_a_marked_expired_hold_with_the_scope_of_its_rules
 models_admin/test_invocation_holds.py::test_an_unexpected_provider_error_settles_the_hold_as_unknown_usage_of_the_call
 runtime/test_orphaned_holds.py::test_a_marked_hold_survives_a_crash_and_its_ttl_until_the_reaper_charges_it_in_full
 runtime/test_orphaned_holds.py::test_the_invocation_sweep_of_client_a_never_touches_client_bs_holds_or_usage
 runtime/test_orphaned_holds.py::test_the_reaper_tick_settles_every_clients_marked_expired_invocation_holds_in_its_client
HEAD (8): the same 7 plus runtime/test_orphaned_holds.py::test_usage_charged_just_before_midnight_still_counts_when_summed_just_after_it. R1 (1): only ..test_the_invocation_sweep_charges_a_marked_expired_hold...
HOLD -3 days, BASE: finding 1 a-e plus test_reservations.py::test_a_direct_invocation_hold_counts_against_the_rules_it_names_only and ::test_a_token_quota_counts_a_direct_invocation_hold_that_names_it. HEAD: only those last two. They use reserve_invocation, so nothing on head covers reserve_step's period.

COMMANDS (exit code)
- git worktree add --detach .worktrees/184-iso2 8074343; python scripts/dev_db.py env (0); UV_LINK_MODE=copy uv sync --frozen (0)
- `uv run --frozen pytest -q -n 4 -rfEs -p <plugin> tests/runtime/test_orphaned_holds.py tests/ledger tests/models_admin/test_invocation_holds.py` (PYTHONUTF8=1, VIRTUAL_ENV unset): exit 0 for base none, head none, head run-hold; every other run in the table exit 1 (failed tests only)
- `uv run --frozen pytest -q -n 8 -rfE -p mut_stepdate tests/runtime tests/ledger tests/models_admin tests/assist tests/appgen`: head 0, base 1
- git worktree remove --force .worktrees/184-iso2 (0); temporary plugins deleted. The job worktree is clean at 8074343; I edited nothing there or in the main checkout (which shows other sessions' uncommitted docs). Not run: full suite, checks.sh. Scratch database of 184-iso2 not dropped, if `dev_db.py env` created one.
