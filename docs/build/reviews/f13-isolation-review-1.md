# Isolation review 1: job 008 (issue #5, F-13), PR #40

Reviewer: isolation-reviewer (opus), 2026-11-18. Verdict: **APPROVED**. Read-only review of `git diff origin/main...HEAD`.

1. `runtime/steps.py:279-289` (`audit_level: full`): content enters the audit row only when `capture_full(ctx.session, ctx.tenant_id, ctx.settings)` is true for the run's own client; `ctx.audit` takes tenant, workspace and project from the run row (`runtime/context.py:209-240`); arguments go through `_journal_arguments`, output is the stored, scrubbed artifact value. Content lands only in the same client's log, only while its capture is `full`.
2. `steps.py:955-956` (`require_approval`) only adds approvals; approval rows keyed by `ctx.tenant_id` and `ctx.run_id`; `pipeline.py:391-404` upstream approval node waives only `DEFAULT_APPROVALS`.
3. `steps.py:726-727`, `pipeline.py:403`, `graph.py:149`: `node_call=True` only tightens `allowed_agent_ids`.
4. Test files gain lines only; no RLS schema test, role check or cross-client test touched; no migration, grant, route or policy change.

Non-blocking:
5. `runtime/action.py` still builds `ToolPolicy(agent_id=None)` without `node_call`; single-action path ignores `allowed_agent_ids` (within one client; B-2570 out of scope). Follow-up issue filed.
6. `steps.py:287` preview is 8000 characters, can exceed 8 KiB for multibyte output.
7. Content written under capture `full` stays in the append-only audit log after capture is switched off (B-2570; within the client).
8. Suggested test: two clients, A capture `full`, B `metadata`, both calling a `full`-audit tool; only A's row has content. Follow-up issue filed.
