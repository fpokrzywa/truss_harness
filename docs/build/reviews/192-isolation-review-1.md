# Isolation review 1: job 192 (#385, PR #389)

APPROVED. Isolation review 1, job 192 (issue #385, PR #389), head 1498100, base origin/main. No blocking findings.

Scope: the diff is `backend/tests/governance/test_routes_cross_client.py` (+14/-0) and one `docs/build/DECISIONS.md` row (B-4101, id not on main). Nothing under `backend/src`, no migration, route or grant.
Nothing removed or weakened: the diff only adds lines. The new block (495-508) comes after every B check and before `h = as_user(client, two.a.email)` at 509. B's rebuild comes after `before == after == 200` (480). The 404-equivalence checks that follow do not depend on rollups. 200 and 3 are literals that follow from the seed.

Mutations: own scratch worktree (removed), `-n 2`. `tenant_id = :t` relaxed in one function, RLS off on both usage tables, per-worker test database only.
| Leak | Base | Head |
|---|---|---|
| none, RLS on / RLS off | pass / pass | pass / pass |
| A into B: `summary_rows` | pass (missed) | FAIL :500 group keys |
| A into B: `totals_rows` | pass (missed) | FAIL :502 totals |
| A into B: `_day_totals`, both queries | pass (missed) | FAIL :506 sums (`consistent` at :505 still true) |
| A into B: `_day_totals`, events only / rollups only | pass (missed) | FAIL :505 `consistent` |
| A into B, or all rows: `_event_groups` (rebuild writes A's usage into B) | pass (missed) | FAIL :500 |
| A into B: `_event_groups` + `_rollup_groups` | pass (missed) | FAIL :500 |
| B into A, or all rows: `summary_rows`, `totals_rows`, `_day_totals` | fail | FAIL, same A-side assertion (:455-457) |
| B into A, or all rows: `_rollup_groups` | fail | FAIL :477 |
| B into A: `_event_groups` | pass | pass (observation 2) |
| A into B: `_rollup_groups` only | pass | pass (observation 1) |
build.md (a), (b), (c) reproduced. The unmodified file passes at head (21 tests).

B's rebuild does not mask a leak. Rebuild sets B's rows and zeroes stale ones but never deletes. I planted a rollup row (A's workspace, B's tenant, 0 tokens, 5 calls): B's rebuild answered consistent, and :500 still failed. Token leaks written into B's rollups earlier fail at 480 before B rebuilds.

Observations (non-blocking; base misses both; outside the spec's claims)
1. `test_routes_cross_client.py:497`: B's rebuild is checked for status 200 only. If `_rollup_groups` lost its filter and RLS, A's rollup figures would reach B in the rebuild answer's `detail`, unnoticed. Confirmed fix: `assert b_rebuilt.json()["consistent"] is True` (fails under this mutation, passes without it).
2. `test_routes_cross_client.py:476-480`: A's summary is not read after A's rebuild, so a rebuild that writes B's events into A's rollups goes unnoticed. Fix in a follow-up: assert `events_sum == rollup_sum == 100` on `rebuilt.json()`, or re-read A's group keys.

Scratch worktree removed; main and the job worktree are clean. Note: `test_every_route_has_a_cross_client_test` failed only in my mutated copies, because I renamed the test there.
