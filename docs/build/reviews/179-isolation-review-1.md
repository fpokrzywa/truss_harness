APPROVED

Isolation review 1, job 179 (#363, PR #365), head `7ce98766` (merge-base `ac86bbd`). Saved by the lead from the isolation-reviewer's report, shortened. Read-only; no tests run. Files: `worker.py`, `test_worker_health.py`, `DECISIONS.md`, `process-control.md`. No route, migration, grant, policy, definer, query or session code changed. No blocking finding.

- Client context: `_clients`, `_sweep_client`, `_sweep_client_invocations`, `_audit_reaped` (`worker.py:687-804`) and `core/db.py` untouched; the only new shared state is the float `_sweep_started`, no client data.
- State and reap: `_sweep_started` is set after and cleared before the sweep lock is held; past the bound only `mark_reap` is dropped (674). No lease, fencing or claim path depends on a sweep; a second concurrent sweep stays impossible.
- Errors and shutdown: per-client try/rollback unchanged; after a stop there is no second listing; a step past the grace commits or rolls back its own client's transaction as before.
- Output: `worker.sweep_still_running` carries worker_id and elapsed_seconds only. `/healthz`, `/readyz` unchanged.
- Tests: no assertion, skip, retry or C1/job-kind test changed; the new isolation test is not vacuous.

Not blocking:
1. `worker.py:646-657`: the bound covers the whole pass, so a worker with roughly 4,600 clients at 120 s would log and stop marking with nothing stuck (false operational signal).
2. `worker.py:620`: effectively unreachable thread-start edge.
3. `_rows_but_the_jobs`: nothing asserts B's job audit rows are in B and absent from A.

The suites were not verified; `origin/main` had moved to `a869015` (job 175), no conflict, no overlap in the paths reviewed.
