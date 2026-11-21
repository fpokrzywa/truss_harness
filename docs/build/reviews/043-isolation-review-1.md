# Job 043 (issue #33) isolation review 1

Reviewer: isolation-reviewer (read-only; the lead recorded this from its report). Diff: `origin/main...1a10d158c562ad9b4a69a9d51887333f5155e63d`. Verdict: **APPROVED**, no blocking findings. Code read, no tests run.

## Cross-client access
- `shifde.approvals` keeps forced row-level security; `tenant_isolation` checks `shifde.current_tenant_id()` in USING and WITH CHECK (runtime_0001_runtime.py:97-104, 1062).
- The grant adds only column `arguments` for `shifde_app`; nothing becomes owned by the app role; the guard function's PUBLIC execute stays revoked.
- `scrub_run_content` UPDATE (repository.py:561-568) filters `tenant_id = :t AND run_id = :r` on top of RLS; the sweep clause (repository.py:600-603) filters `p.tenant_id = :t` joined to the client's own run.
- `scrub_ended` in `ApprovalService.decide` (service.py:1085-1086) runs inside the deciding transaction after authorization, on the run row already loaded for the caller's organization.
- `test_scrubbing_as_a_never_touches_bs_rows` (test_capture_off.py:646-680) now snapshots B's approval arguments, and asserts A's own decided approval is scrubbed (not vacuous). No new routes.

## Relaxed guard (runtime_0005:37-56)
A decided approval may be updated only if `arguments` equals exactly `{"scrubbed": true}` (NULL refused), `to_jsonb(NEW) - 'arguments' = to_jsonb(OLD) - 'arguments'`, and the run (matched on the row's own tenant_id, run_id) is terminal. Invoker rights, pinned search_path, only BEFORE UPDATE trigger on the table. Every other decided-approval update still raises 23514; pending rules unchanged. Downgrade revokes the grant and restores the runtime_0001 body.

## Notes (not blocking)
1. The database does not check capture-off or root-run-ended; the application does (settle.py:66-84). The worst case is a client losing its own data; no exposure. Same as the existing artifact and invocation guards.
2. Depends on the `deliberate_changes.yaml` entry added in 1a10d15.
