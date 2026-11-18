APPROVED

Isolation review 1 of PR #43 (`factory/010-arch-m3-recover-a` @ af19bf1 vs origin/main), isolation-reviewer (opus), read-only. `pytest tests/runtime/test_worker.py tests/runtime/test_cross_client.py tests/app/test_p5_review.py tests/runtime/test_capture_off.py -n 4`: 63 passed.

No blocking findings.
1. No double run in one worker: abandoned `_Job` stays in `_inflight` (worker.py:318-320); `_dispatch` (196-209) hands a requeued claim back; only `_active()` (286-288) excludes it. Across workers the run lease token fences (lifecycle.py:501, context.py:156, pipeline.py:689).
2. Late return after reaper settles is fenced: `finish_job` (repository.py:1093-1102) keyed on tenant_id, lease_owner, status='leased'. New test snapshots every run/step/invocation/approval/job row before the late return and asserts unchanged (test_worker.py:592-597), `len(deliveries) == 1`.
3. C1 kept: new test requires `timed_out` + UNKNOWN, not cancelled, indeterminate invocation, pending unknown-outcome approval (test_worker.py:569-575). Existing C1 test unchanged apart from removing `reaper._executor.shutdown()`.
4. No shared session/client context: each thread opens its own session per operation; `app.*` applied per transaction via after_begin (core/db.py:93-106). B's state asserted unchanged.
5. Exactly 8 `_executor.shutdown` cleanup calls removed (6 test_worker.py, test_capture_off.py:348, test_p5_review.py:619, the last replaced by `wait_for(jid not in worker._inflight)`). No assertion removed or loosened.
6. Races: `abandoned` set under `_lock`; `notify_all` in `_work` finally and `_beat`; `_shutdown` `wait_for(not _active())` cannot miss a wakeup.

Non-blocking (availability, not isolation):
7. worker.py:209: abandoned threads are unbounded by `concurrency`; a repeatedly hanging tool for one client could pile up threads and, if they hold pooled connections during the external call (pool 5+5, core/db.py:245-246), exhaust the pool for all clients. Previously slots filled instead (worse). Suggest a per-worker/per-client cap on abandoned threads with a log/metric. (Follow-up issue.)
8. worker.py:164-170: `_shutdown` disposes the engine while abandoned threads may live; a late return opens a fresh pool, still fenced (tested). Daemon threads end with the process.
