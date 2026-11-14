# Ledger: isolation review, round 2

Branch `ledger` at `3e9f6d5` (main `4c27223` merged in, then fix round 1). Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: APPROVED.** `tests/schema tests/core tests/ledger -n 8`: 323 passed. Probes as `shifde_app` (asserted) with tenant A, rolled back.

1. Blocking 1 closed (`ledger_0001_ledger.py:99-103`): B's card or a random card with B's tenant → 42501, same message, back- and future-dated; NULL without admin → 42501; B's open version untouched. Allowlist reason corrected (`test_function_schema.py:45-57`).
2. Blocking 2 closed (`:360,400-403`): alert naming B's budget → 23503 on B's slot, a free slot and with `ON CONFLICT (tenant_id, …)`, same as a random budget; A cannot take B's slot.
3. Follow-up 3 closed (`:162-176,282-286`): SECURITY INVOKER version-scope check; B's or random version → 23503; global accepted.
4. Follow-ups 5, 6, 10 closed (`test_cross_client.py:160-180,286-412`; filters in `enforcement.py:108,131`, `pricing.py:190`, `usage.py:107-120`, `budgeting.py:109-110`; overloads of allowlisted definers reported, `test_function_schema.py:128-140`).
5. No protected test weakened (`test_rls_schema.py`, `test_startup_role_check.py`, `tests/conftest.py`, `global_tables.txt`, `core/db.py`, authz tests identical to main; function schema test only gains checks).
6. Downgrade correct; `test_every_migration_downgrades_and_upgrades_again` passes.

## Follow-up

7. `ledger_0001_ledger.py:104`: advisory lock taken before the card-ownership lookup; with its own tenant id A can wait on B's card-id lock (timing signal; needs B's id). Move the lock after the `NOT EXISTS` check. Owner: ledger/governance.
8. Round-1 items 4, 7, 8 unchanged, not reachable, owned as recorded.
