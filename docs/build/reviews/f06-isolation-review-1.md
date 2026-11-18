# Isolation review 1: job 007 (issue #4, F-06 tool-invocation audit), PR #38 at `36d32eb`

Reviewer: isolation-reviewer (opus), 2026-11-18. Verdict: **APPROVED**. Reviewed by reading the diff against `origin/main` and the code it calls; no tests were run by the reviewer.

1. `runtime/steps.py:244-273` `_tool_audit` writes only through `ctx.audit(..., commit=False)`; `runtime/context.py:209-242` takes `tenant_id`, `workspace_id`, `project_id` from the run row loaded for `ctx.tenant_id` and the audit context from `ctx.scope.ctx`. No client or scope value comes from the tool call, step or user input.
2. All seven call sites in `steps.py` (about lines 908, 985, 1242, 1267, 1318, 1347, 1389) sit inside an existing lease-fenced `ctx.txn()` (`context.py:193-206`), so a worker that lost its lease cannot add events.
3. Event fields come from the same run's step row and tool version; identifiers only, no arguments, argument hash or output; `capture_content` not bypassed.
4. No migration, definer, grant, route or role change.
5. New cross-client test `tests/runtime/test_tool_audit.py:352-387`: real `run_tool` and `invoke_for_run` for clients A and B under `shifde_app`; each client's listing holds only its own events, nothing matches the other client's run id, listing the other tenant returns `[]`.
6. No existing test removed or loosened.
7. Non-blocking: `steps.py:256-257` records `indeterminate` as outcome `failure` with `details.status = "indeterminate"` (B-2540).
