# Isolation review 1: #174 (PR #346, head 2289ac6)

APPROVED (isolation-reviewer, 2026-11-24; recorded by the lead, the reviewer is read-only).

1. Cross-client: no leak. The runtime_0008 guard's legal-hold read is limited to `tenant_id = OLD.tenant_id`, runs SECURITY INVOKER with a fixed search_path, and `retention_policies` and the nine run tables are ENABLE+FORCE row-level security with `tenant_isolation`. No grants, owners or policies change.
2. Downgrade is exact: `PREVIOUS_GUARD` is pinned equal to runtime_0006's `GUARD`; the scratch-database round trip checks body, settings, invoker rights, no PUBLIC execute, owner and all nine triggers.
3. The two guard tests moved from cutoff 0 to 30 days still test the same condition (at 0 the new floor check would refuse first). Minor: `_refused` checks only SQLSTATE 42501, not the message; comments say "inside the floor" meaning outside.
4. No weakening: RLS schema test, startup role check and cross-client tests untouched; test_schema.py only adds `runtime_0008 in heads`; deliberate_changes.yaml edits change reason text only.
5. maintenance.py keeps `e.tenant_id = :t`; the change only pins more runs.
6. Not blocking, same-client only: the guard enforces the global 1-day floor, not the client's own stored `retain_days`. Follow-up issue filed.
