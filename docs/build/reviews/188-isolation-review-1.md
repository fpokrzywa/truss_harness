# 188 isolation review 1 (#377, head 5845399)

APPROVED. Isolation review 1, job 188 (issue #377, PR #379), head 5845399. No blocking findings.

Scope
- The diff against merge base f49addf is one file, `backend/tests/governance/test_routes_cross_client.py` (+33/-7). Nothing under `backend/src`, no migrations, no `tests/schema`.
- origin/main moved to 44027c6 during the review. `governance/` is identical there, and its added test lines hold no hex-and-dash substring assertion.

Nothing removed, narrowed, vacuous or loosened (line by line)
- :454 and :489 are still exact list comparisons, with the new keys in the new sort order.
- :494 still searches the full text of B's export.
- :483-484 (`run_b`, `world.b.ws`, `world.a.ws`), the summary, reconciliation, rollups and 404-equivalence checks are unchanged.
- Added: :485, a positive check that A's marker is in A's own export, and the unit test at :414.
- Seed change: A's event now carries A's own model (`add_model`). B's events change only in their keys; the `b-run` raw row is unchanged.
- No marker is a substring of another, of `b-run` or of the CSV header.

Mutations
- Method: own pytest plugin outside the worktree. It relaxed `tenant_id = :t` in one named function, with row-level security disabled on `usage_events` and `usage_rollups_daily` in the per-worker test database. Base ran from the main checkout (no file written); head ran from the worktree.

| Mutation | Base | Head |
|---|---|---|
| None, RLS on | pass | pass |
| None, RLS off (filters alone hold) | pass | pass |
| `export_events` filter off, RLS on (RLS alone holds) | pass | pass |
| `export_events` all rows, RLS off | fail :462 | fail :483 |
| `export_events` A's row into B's export only | PASS (leak missed) | FAIL :494 |
| `export_events` B's two rows into A's export only | fail :462 | fail :483 |
| `export_events` `b-run` row into A's export only | fail :462 | fail :483 |
| `list_events` all rows | fail :433 | fail :454 |
| `list_events` A's row into B's list only | fail :467 | fail :489 |
| `summary_rows` all rows | fail :435 | fail :456 |
| `totals_rows` all rows | fail :436 | fail :457 |
| `_day_totals` (reconcile) all rows | fail :437 | fail :458 |
| `run_events` all rows | fail :439 | fail :460 |
| `_rollup_groups` (rebuild) all rows | fail :456 | fail :477 |

- Head catches every leak base catches, at the corresponding assertion. It also catches the A-into-B export leak, which base could never detect because the CSV has no `event_key` column.
- Build.md mutations (a) and (b) are reproduced: (a) fails at the first export check, (b) at :494.

Sweep (own AST script over `backend/tests`)
- Covered `in`/`not in` with hex-and-dash constants up to 20 characters, names bound by assignment or loop, hex slices, and `find`/`count`/`index`/`search` arguments.
- The only hit in a cross-client substring assertion was the flaky marker, now only in the unit test at :417.
- Remaining hits are not cross-client substring markers:
  - `admin/test_iam.py:116`
  - `app/test_connections_api.py:984`
  - `app/test_p8_review_r1.py:290`
  - `cli/test_backup.py`
  - `connections/test_snowflake.py:116`
  - `adapters/dataplatforms/test_snowflake.py:254,263`
  - `adapters/native/test_docker_runtime.py:302`
  - the `name in globals()` checks at `delivery/test_routes_cross_client.py:172` and `models_admin/test_routes_cross_client.py:177`

Runs at head: `pytest -n 4 tests/governance` gave 84 passed. The worktree is clean at 5845399 and my scratch folder is removed.

Observation (pre-existing on base, out of this job's scope, not blocking)
1. `backend/tests/governance/test_routes_cross_client.py:455-458`: usage summary and reconciliation are only read as A, so this test would not notice a summary leak only from A into B. Optional later fix: the same exact group-key and totals checks as B.
