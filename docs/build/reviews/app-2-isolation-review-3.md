# app-2 isolation review 3 (merge-gate fix round B-2452, `runtime/lifecycle.py` `finalize`, 53aaee9)

Verdict: APPROVED (opus isolation-reviewer, 2026-11-17).

- `repo.control_state` in `finalize` filters `id AND tenant_id`, with `tenant_id = ctx.tenant_id` (the claimed run's client), on the worker's app-role session (`require_app_role`), so RLS applies too; no client id comes from input.
- The lock claim holds: `ctx.txn()` first runs `repo.fence` (`FOR NO KEY UPDATE` on the run row, lease token, `running`); `request_cancel` and `lock_run` wait on it; READ COMMITTED, so a cancel committed first is seen, and a later canceller finds the run `cancelled` or `waiting` and cancels it itself.
- The cancel path goes RUNNING -> CANCELLED through the fenced `repo.transition` and `settle_terminal`, the same call as `cancel_open_children` and the API cancel (`settle_ended_run` keyed by the run's own tenant).
- The regression test fails without the src change (`child_runs` on an agent run parks it), runs as app role and schema owner, and asserts client B unchanged.

Non-blocking notes (open):
1. A pipeline or graph run ended cancelled here does not cancel its own open children at this point (as the existing `except state.Cancelled` path); an agent child sees the ended parent at its next `control()`.
2. An ended pipeline parent is not checked before parking an agent child (possible follow-up to B-2452, not a leak).
3. The test seeds no pending approval or budget hold for the cancelled case; that cleanup is the shared `settle_terminal` covered elsewhere.
