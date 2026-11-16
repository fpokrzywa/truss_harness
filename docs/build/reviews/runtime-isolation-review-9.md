# runtime isolation review 9 (RT-10 `lifecycle.py`; `git diff 21bce40 111c150`)

Reviewer: `isolation-reviewer` (opus, fresh), 2026-11-16. Read-only; saved by the lead. Findings go to the RT-11/RT-12 builders.

**Verdict: APPROVED.** No way for client A to reach client B's data. `_advance` sets the client, then reads the job with `job_of_run(tenant_id, run_id, job_id)` before it reads or moves the run; B's job for A's run, B's run named under A, A's job of another run and unknown ids all return `skipped` from the same branch and change nothing. Every repository and SQL call passes `tenant_id`; SETH's raw SQL by id for the trace (`:200`, `:203`) is now `get_run` plus `job_of_run`; `fk_jobs__run (tenant_id, run_id)` is a second barrier. `_claim` (`lifecycle.py:181-194`) sets `lease_job_id` only when the job is that run's job, `leased`, with `lease_owner` = this worker. `RunContext.run` is re-read with the client id and checked `running` under the new token (`:458-461`). `advance` and `advance_in(usage_db=)` call `require_app_role` before any statement. The "client unavailable" failure (`:474-477`) fires only when A's own `tenants` row is missing, which B cannot cause.

Checked: the whole diff against SETH 3ba0e06 `lifecycle.py`; `repo.transition`, `add_event`; `settle_terminal`, `settle_ended_run`; `RunContext`; `core/db.py` (client setting per transaction, re-applied on every begin); the `tenants`, `fk_jobs__run`, `runs.lease_job_id` schema; B-2240..B-2246; carry_forward "Runtime part 10"; review 8.
- C1 holds: the catch-all (`:546-562`) goes through `finalize`, `settle_terminal`, `settle_ended_run`; the test asserts `indeterminate_side_effect`, invocation and step `indeterminate`, one `outcome_unknown` event, the pending unknown-outcome approval, B's in-flight write untouched. `Waiting(transitioned=True)` is not finalized again (`:531`).
- The scripted fake executor replaces only `runner.run()`; context, audit, exception handling and `finalize` are the real code.
- `pytest -n 8 tests/runtime` at 111c150: 450 passed, 0 failed.
- Two removal proofs spot-checked through a pytest plugin outside the repo: pre-check in `_advance` removed fails `test_b_job_never_recovers_a_running_run_of_a_but_its_own_job_does` (2/23); `job_of_run` client and run predicates removed fail 3 cases. Both match B-2246.
- Review 8 items 1-3 unchanged (files not touched); item 4 reduced (`ctx.run` now the claimed row re-read by client); the remaining drift within one run is an RT-11/RT-12 item.

## Findings

1. **Low, inside one client.** Recovery (`lifecycle.py:365-376`) resets a `running` run whose `lease_job_id` is NULL or equal to this job without checking the job is `leased` by this worker; the `running -> pending` transition is not token-fenced (SETH behaviour, B-2241). Fix (RT-12): require `job["status"] == "leased" and job["lease_owner"] == worker_id` here too, or record why the claim definer suffices.
2. **Low, inside one client.** The fail-before-start path for an unavailable client (B-2243, `:474-477`) has no test. Fix: stub `tenant_header` to None; assert `failed` with `authz_denied`, lease cleared, B unchanged.
3. **Info, inside one client.** `advance_in(session, None, ...)` is public and does not check the role behind `session`. Fix (RT-12): keep the worker on `advance`, or rename to `_advance_in`.
