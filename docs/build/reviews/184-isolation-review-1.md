# Isolation review 1: job 184 (#366, PR #367), head da883216

APPROVED. Isolation review 1, factory job 184 (#366, PR #367), head da883216, base origin/main 2a6050d.

**Scope**
- The diff touches 7 files, all under `backend/tests/`: `ledger/{util,test_cross_client,test_enforcement,test_reservations,test_reservations_cross_client}.py`, `models_admin/test_invocation_holds.py`, `runtime/test_orphaned_holds.py`.
- Nothing changed in `backend/src`, migrations, `tests/schema`, routes, conftest, `factory/` or `.claude/`.
- The only removed lines are imports, one docstring line, one function signature (gains `owner_db`) and the `since=` values. No assert or expected value was removed or changed. No skip, xfail, retry or broadened `raises` was added. No query scope (tenant, rule id, session role) changed.

**Cross-client assertions, one by one**
- `test_cross_client.py:228-248` (A's `shifde_app` session naming B): the `used_amount` call still runs in A's session. Only the window is read through `owner_db`, over B's own rows, so B's 7000 tokens are always inside it. Before, a midnight crossing made `spent == 0` vacuous, so this is now stronger. `daily_since` asserts that B has rows.
- `test_cross_client.py:268` and `test_enforcement.py:179,194`: the window covers the earliest row of A and B together, so B's rows cannot fall outside it. `used == 100`, `(100, 0)` and `held_b == 5000` stay non-zero.
- `test_reservations_cross_client.py:132`: the window comes from A's holds in A's session. Both sides' holds are written in one transaction with `created_at DEFAULT now()`, so B's holds share the instant and are in the window. `(0, 40)` is unchanged and non-vacuous.
- `test_orphaned_holds.py:_used_and_held`: same app-role session and tenant. The per-client figures (`AMOUNT`) stay non-zero. The `(0, 0)` case still has a visible expired hold, and a `(0, AMOUNT)` assertion precedes it.
- `util.py:daily_since`: bound `tenant_id = ANY(:t)`, read-only, caller's session. It fails loudly when no row is visible, with no fallback to the live clock or an unbounded window.

**Mutation runs** (plugin outside the checkout, scratch test databases, the six touched test files; worktree left clean)

| Mutation | Result |
|---|---|
| none | 80 passed |
| `_used_sql` without the tenant filter | 3 failed: both `test_cross_client` service-query tests (7000 vs 0, 7100 vs 100) and `test_another_tenants_usage_budgets_and_holds_are_ignored` |
| `_held_sql` without the tenant filter | 1 failed: `test_another_tenants_usage_budgets_and_holds_are_ignored` |
| RLS off on `usage_events` (`reconcile` stubbed to pass, so `used_amount` alone must catch it) | 5 failed, including `..._return_nothing_under_rls` |
| RLS off on `budget_reservations` | 6 failed, including `test_under_as_context_bs_holds_are_invisible...` and the client A/B sweep test |

Under these mutations the affected cross-client tests fail as they should.

**Non-blocking notes** (none is a cross-client exposure)
1. `test_reservations.py:277` and `:364`, and `test_orphaned_holds.py:119` after a settle: `since` is now derived after the charge, so it follows the charged row's own `occurred_at`.
   - A future bug that backdates the unknown-usage row by days, so the spend drops out of the client's own daily budget, would no longer fail these sums.
   - I backdated `reservations.record` by 3 days. Exactly one test still catches it: `test_the_invocation_sweep_charges_a_marked_expired_hold...`, where `since` is taken before settlement.
   - That covers the direct-invocation sweep (`_charge_unknown`). `settle_orphaned` (run holds) has its own `record` call and no remaining catcher in these files.
   - This is a single-client budget-accounting loosening. Optional fix: in the run-hold test at `:277` (and `:364`), compute `since = daily_since(s, tenant)` before the settle call, when the hold already exists. That is equally midnight-safe and restores the check.
2. `test_cross_client.py:235,259,261` still call `reconcile` with `_today()`. At 235, a midnight crossing can still make the `(0, 0, True)` check vacuous; 259 and 261 fail visibly. Pre-existing and declared out of scope in the spec; worth the follow-up issue the spec mentions.
3. New tests: `test_reservations.py:116` uses fixed instants, asserts both 0 and 250, and checks the helper's refusal on no rows. `test_orphaned_holds.py:175` goes through the real reaper and reads as `shifde_app`. By reading, the old live-clock `since` would give `(0, 0)` there. Both are non-vacuous; the second has no client B assertion, which the neighbouring crash test covers.

No change required for isolation.
