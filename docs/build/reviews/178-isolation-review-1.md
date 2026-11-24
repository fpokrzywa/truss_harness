# Isolation review 1, job 178

APPROVED: isolation review 1, job 178 (#361, PR #362), head d01e67324d306302c8d09a1f9128194655ca1f58 (merge-base fdea71b; worktree clean). Read-only; no tests run. Files in the diff: worker.py (+67/-32), test_worker_health.py (+232/-0), DECISIONS.md (+1). No route, migration, grant, policy, definer or query changed, so no new cross-client route test is due.
Paths are under S:/Development_2026/shi_work/agt_trus_harness/.worktrees/178-the-approved-write-worker/.

No blocking findings.

Checked
A. Client context: each sweep step opens its own `Session` (worker.py:698, 716, 746) and context lives in that Session's `info` (core/db.py:60-67). It is applied with transaction-local `set_config` in `after_begin`, and every pool checkout runs `DISCARD ALL` plus the preset refusal (core/db.py:203-216). The sweep path uses no module-level cache, contextvar or worker-held Session. `_sweep_client`, `_sweep_client_invocations`, `_clients` are byte-identical to main; the platform-admin flag is confined to `_clients`. Nothing passes from sweep thread to main loop except the pool, `stop`, `closed`, `_sweeping`.
B. Races with claim, heartbeat, reap: the sweeps were already concurrent with run threads, heartbeats and other workers' reaps (REAPER_LOCK is a transaction lock, released before the sweeps). What is new is only this worker's own claim and lease reap.
 - Capture-off touches only runs whose run and root are committed terminal (repository.py:586-606).
 - The reaper's unknown-outcome settle runs in the same transaction that ends the run, so a sweep cannot clear write arguments before it. No sweep changes run, approval or job status, and pending unknown-outcome approvals keep their arguments.
 - Run holds (`settle_orphaned`) and invocation holds (`invocation_id IS NOT NULL`, FOR UPDATE) are disjoint. `status='held'` and the unique `event_key` prevent a double settle.
 - `scrub_ended_invoke_jobs` touches only done/dead jobs; claim and reap use SKIP LOCKED on queued/leased.
 - `_audit_reaped` and the sweep can now scrub the same just-ended family. Both go in run-id order and artifacts, invocations, approvals order, and the sweep takes no audit lock: no deadlock cycle found.
 - No path reports an ended or approved write as cancelled, or redelivers it. Lease token, fencing and claim order are untouched (charter 5.3 holds).
C. Overlap: a non-blocking `_sweeping` for both entry points (605, 610), released in `finally` (621). A second sweep in one worker is impossible.
D. Shutdown: only the main thread starts sweeps, so none starts after the loop. `closed` is set before the bounded acquire, sharing one grace (268, 280-283), and the lock is kept before `dispose()`. Past the grace the daemon's transaction rolls back, and it is idempotent. Wait timeout never negative.
E. Exceptions: an escaped `Exception` is logged and the next reap retries (618). Per-client failures stay contained; only a connection-level failure of `rollback()` escapes, which is not client-controlled, so no poison client.
F. Readiness: the sweep no longer sets `claim_tick` (a stuck main loop turns unready, tested). Predicate and windows unchanged.
G. Assertions: the diff removes no test line. test_worker.py, test_jobkinds.py and every conftest are unchanged. build.md's function table matches the diff.

Non-blocking notes
1. worker.py:610 (with 639, governance/service.py:1879): a stuck or very long sweep is no longer visible. Before, a stuck step turned /readyz 503 and a long sweep made platform health `reaper.status` "stale"; now `mark_reap` and `claim_tick` stay fresh and the skip is silent. Scenario: single worker, sweep connection half-open. Capture-off scrubs and orphaned invocation holds stop for all clients until the OS TCP keepalive errors the socket (about 2 h default; also unbounded lock waits if `db_statement_timeout_ms=0`). At the default 60 s statement timeout a live server bounds each statement, so it self-heals; it delays scrubbing, it exposes nothing across clients. Fix (follow-up issue): record the sweep start and log `worker.sweep_still_running` with elapsed seconds when a due sweep is skipped past a bound.
2. worker.py:610-611: if `Thread.start()` raises (thread exhaustion), `_sweeping` stays held and the error leaves `run()`. The worker exits 1, as intended for a crash, but `_shutdown` first waits out the grace and logs a misleading `worker.sweep_outlived_grace`. Fix: release in an `except` around `start()`.
3. worker.py:617 and 710: after a stop, the capture-off sweep returns and the hold sweep still calls `_clients` once before its first `_sweep_over()` check. Past the grace this lists clients on the disposed engine, opening a fresh connection in embedded use. Ids only, nothing written. Fix: check `_sweep_over()` between the two sweeps.
4. test_worker_health.py: every new test stubs both sweep steps, so none runs a real `_sweep_client` of A while this worker claims and runs B's job. B's job does run during a held sweep and its (tenant, job) pair is asserted (around 381-385). Isolation rests on the unchanged session code (A above), which has its own tests. Nice to have: one unstubbed step asserting B's rows.
5. test_worker_health.py:286 `..._stays_ready` is unchanged, but it passes on loop ticks alone now; the new stuck-main-loop test covers the converse. Header lines 5-7 still say "reaper sweep step": stale text only.
6. worker.py:191: one more steady pool user (concurrency + 4 against pool_size concurrency + 3, overflow 4). Within overflow; no starvation of heartbeats found.

Not verified here: the suites, CI on PR #362 and the ten loaded runs (build.md's claims); the CI client counts are inferred by the builder.
