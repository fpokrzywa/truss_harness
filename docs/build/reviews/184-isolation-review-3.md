# Isolation review 3, job 184 (#366, PR #367), e714b60

APPROVED: isolation review 3, job 184 (#366, PR #367), head e714b60, merge base f49addf.

**Answer.** No edited test catches fewer cross-client leaks than on the base. I ran 37 mutations other than the clock shift on both sides. Every test that fails on base also fails on head. For the 33 in the main run I also compared where each of the 210 common failures stops: same statement, same assertion apart from generated ids and one latency. The branch changes 7 test files and DECISIONS.md, and no production code. It merges cleanly with origin/main f2af18b, whose newer commits do not touch the ledger, the runtime, or these tests.

**5 or 12 of 13: both numbers are right; they measure different mutations.** `reservations.record` has two call sites.
- **Direct-invocation site, `_charge_unknown` only:** 5 on base and the same 5 on head. I got 5 with a nonexistent client, with an existing one, and with the charge really landing in another client past row-level security. This is review 2's number.
- **Both sites (adds run holds, `settle_orphaned`):** 12 to 14 on base, and the same plus the new midnight test on head. The exact total depends on which "other client" the mutation picks.
  - A nonexistent client gives 14/15.
  - An existing client with all columns kept gives 11/12.
  - The charge actually landing elsewhere gives 10/11.
  - The builder's 12/13 lies in this range. All 12 of its ids are in my 14. The two others are `test_settle_terminal_charges...` and `test_an_orphan_without_a_currency...`; both run as owner.
- So the builder's inference (its plugin also moves run-hold charges) holds. Review 2's plugin is deleted and it listed no ids, so "review 2 moved only `_charge_unknown`" rests on the count matching exactly. "12" is 12 failures of 122 tests on base and "13" is 13 of 124 on head, so "12 of 13" is not a missed test. No test was lost in any variant.

**Can the head's window hide a charge?**
- **Same UTC day: no.** I instrumented every `daily_since` call in 10 non-midnight runs (the unmutated one and 9 mutations), except in the two new tests. All 157 calls returned exactly `period_start("daily")` of the live clock, with no difference. So the sums use the same boundary as base.
- **No mutation of ledger code can move it.** The only production INSERT into `budgets` is the governance API, which these tests do not use. `created_at` keeps its default, and no ledger code writes it. The helper filters on an explicit tenant, and RLS off on `budgets` loses nothing (5/5).
- **Another client's budget: no.** The window is a lower bound on time only. The tenant filter, rule id and scope in the sum are unchanged, so a charge in B is not in A's sum. `real_co_*`, `swap_co_*`, `dup_co_all`, `hold_other_budget` and `other_used`/`other_held` lose nothing.
- **Across midnight: the window follows the budget's day.** I repeated seven mutations with `budgeting.utcnow` a day ahead on head. Each still fails every test that base fails for it without the midnight shift. A crossing test's window starts at most one day earlier than base's. Base gives no usable result there, because 13 of these tests fail unmutated.
- **Limits that base shares:** the sums have no upper bound, so a charge dated 3 days ahead fails 0 on both sides. So does one at 00:00:00 of the same day.

**Findings (none blocking)**
1. `backend/tests/ledger/util.py:189-204`: review 2 finding 1 is closed. The window comes from `min(budgets.created_at)`, and there is no fallback when no row is visible (the assert fails). All five tests a-e catch a misdated `reserve_step` hold again, including the cross-client `test_reservations_cross_client.py:132` (`(0, 40)`).
2. `backend/tests/ledger/test_reservations.py:115-168`: review 2 finding 2 is closed. A run hold, a direct hold and a usage row dated before the budget's day are asserted not counted, each hold after first being counted. It fails under `nosince`, `other_used`, `other_held` and `hold_other_budget`, so it is not vacuous.
3. Call-site order: checked by reading all sites, and the builder's table is right. Each budget is written in the same transaction as, or an earlier one than, the rows it windows. In `test_cross_client.py:_seed` the usage row is recorded before `add_budget`, but in one transaction whose `now()` precedes it.
4. No assert, expected value, rule, tenant or session role was removed or changed. The removed lines are imports, one docstring line, one signature line and the `since` values. No skip, xfail, retry, rerun or sleep was added. The only new `raises` is `test_reservations.py:124`, which tests that the helper refuses to run without a budget. No test was skipped in any run.
5. Note, pre-existing and out of scope: `test_cross_client.py:235,259,261` still use `_today()` with `reconcile`, and 235 can be vacuous at midnight. The 5 clock-dependent tests of spec criterion 3 remain. Both are named as follow-ups in build.md.
6. Note, wording only: B-4065 says "12" for charges written to another client. That holds for the builder's mutation (both call sites); 5 is the direct-invocation site alone. No change required.

**Mutation table** (failed tests; base of 122, head of 124; "new" = a test that does not exist on base)

| mutation | base | head | lost |
|---|---|---|---|
| none | 0 | 0 | none |
| unknown charge -3 days, both sites | 7 | 8 (same 7 + new) | none |
| unknown charge -3 days, `_charge_unknown` only | 5 | 5 | none |
| unknown charge -3 days, `settle_orphaned` only | 2 | 3 (+ new) | none |
| unknown charge -1 day | 7 | 8 (+ new) | none |
| unknown charge 1 microsecond before today 00:00 UTC | 7 | 8 (+ new) | none |
| unknown charge at today 00:00 UTC | 0 | 0 | none |
| unknown charge +3 days | 0 | 0 | none |
| every usage row -3 days | 13 | 13 | none |
| `reserve_step` hold -3 days | 5 | 5 | none |
| `reserve_invocation` hold -3 days | 3 | 3 | none |
| both reserve functions -3 days | 7 | 7 | none |
| hold and charge -3 days | 12 | 13 (+ new) | none |
| charge to nonexistent client, `_charge_unknown` | 5 | 5 | none |
| same, `settle_orphaned` | 9 | 10 (+ new) | none |
| same, both | 14 | 15 (+ new) | none |
| charge to existing client, columns kept, `_charge_unknown` | 5 | 5 | none |
| same, `settle_orphaned` | 6 | 7 (+ new) | none |
| same, both | 11 | 12 (+ new) | none |
| charge really lands in another client (tenant setting switched), `_charge_unknown` | 5 | 5 | none |
| same, `settle_orphaned` | 5 | 6 (+ new) | none |
| same, both | 10 | 11 (+ new) | none |
| same, both, and -3 days | 10 | 11 (+ new) | none |
| charged correctly and duplicated into another client | 9 | 9 | none |
| direct hold names another budget id | 7 | 8 (+ new) | none |
| tenant filters of `_used_sql` and `_held_sql` removed | 4 | 4 | none |
| same, used only | 3 | 3 | none |
| same, held only | 2 | 2 | none |
| used sums the other clients only | 12 | 14 (+ 2 new) | none |
| held sums the other clients only | 11 | 12 (+ new) | none |
| RLS off on `usage_events` | 4 | 4 | none |
| RLS off on `budget_reservations` | 6 | 6 | none |
| RLS off on `budgets` | 5 | 5 | none |
| RLS off on `usage_events` and `budget_reservations` + tenant filters removed | 12 | 12 | none |
| since filter removed | 1 | 2 (+ new) | none |
| settle moves `created_at` and charge -3 days | 10 | 11 (+ new) | none |
| clock a day ahead, unmutated | 18 | 5 | the 13 the issue cures |

With the clock a day ahead on head plus a mutation (base column = that mutation without the clock shift). Each head set contains base's set and the 5 out-of-scope tests.

| mutation | base | head | lost |
|---|---|---|---|
| charge -3 days | 7 | 13 | none |
| `reserve_step` hold -3 days | 5 | 10 | none |
| both holds -3 days | 7 | 11 | none |
| hold and charge -3 days | 12 | 17 | none |
| charge really in another client | 10 | 16 | none |
| tenant filters removed | 4 | 9 | none |
| same + RLS off | 12 | 17 | none |

**What I ran**
- `git worktree add --detach .worktrees/184-iso3 e714b60`; `python scripts/dev_db.py env` (0); `uv sync --frozen` (0). Base was the same worktree checked out fully at f49addf.
- `uv run --frozen pytest -q -rfEs -p <plugin> tests/runtime/test_orphaned_holds.py tests/ledger tests/models_admin/test_invocation_holds.py` with PYTHONUTF8=1, UV_LINK_MODE=copy, VIRTUAL_ENV unset.
  - Single runs on head (`-n 8`), one mutation each: none 124 passed; `_charge_unknown` to another client 5 failed; both sites 15 failed. A background time limit stopped the shell around the last run, but that pytest finished.
  - All mutations in one session (`-n 10`, each test once per mutation): main 33 gave base 223 failed / 3803 passed, head 225 / 3867. The extra set gave base (5 mutations) 29 / 581, head (17) 166 / 1942.
  - The three single runs and five of the mutations repeated in the second session gave the same counts (for the five, the same ids).
- One run of mine exited 3 on a syntax error in my plugin; I fixed and reran it. No setup or collection errors, and no WinError 10055 or 10048.
- Not run: full suite, `checks.sh`.
- **Cleanup:** worktree 184-iso3 and its plugin files are removed. The job worktree is clean at e714b60, and the main checkout shows no change. A scratch database that `dev_db.py env` may have created for 184-iso3 was not dropped (the script has no drop command).
