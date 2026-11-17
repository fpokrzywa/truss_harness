# suite-speed-2

Goal: full backend suite (`uv run pytest -n 8 -q`) under 1200 s; main measured 1204.5 s idle.

Changes (tests only; no test, assertion, check or config removed or loosened):

- `backend/tests/app/test_p8_perf.py`: the million-event seeding (same rows, same volume, same
  INSERT ... SELECT generate_series) now runs as the test database's superuser with
  `session_replication_role = replica`, in eight parallel 125 000-row transactions, with the
  secondary indexes of `usage_events` dropped during the load and rebuilt (in parallel) from their
  own definitions afterwards, then `VACUUM (ANALYZE)`. All assertions unchanged. Seeding went from
  122 s to about 44 s; the test from about 170 s to about 80 s.
- `backend/tests/conftest.py`: `pytest_collection_modifyitems` moves the three slowest tests to the
  head of the collection so xdist starts them first instead of leaving one worker as the tail.
  Collected count unchanged (3183).

Measured (`uv run pytest -n 8 -q`, full suite, 3183 passed): main 1204.5 s; this branch 1097.3 s. Also changed: `backend/tests/app/conftest.py` (one pooled admin engine per worker for clone and drop; a finished template is remembered per worker, so no per-test connection, lock or template query). Runs of the perf test alone: 170 s -> about 65-80 s.
