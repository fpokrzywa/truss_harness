# runtime isolation review 2 (RT-3 `repository.py`, `git diff 0ced96d 8b1cdef`)

Reviewer: `isolation-reviewer` (opus), 2026-11-15. Read-only; saved by the lead.

**Verdict: APPROVED.** No blocking finding; all findings Low or Info.

Checked: `tests/runtime` at 8b1cdef in `.worktrees/verify3`: 162 passed in 326 s. The builder's proof rerun in memory without writing files: all 62 `tenant_id = :t|:tenant|x.tenant_id` predicates in `repository.py` replaced with TRUE: all 50 schema-owner cross-client cases fail, all 50 app-role cases still pass (RLS), so the tests are not vacuous. (1) Every statement on a client table names the client, joins and subqueries included (`list_runs` EXISTS, recursive trees in `list_approvals` and `unknown_outcome_runs`, `verdict_summary` subqueries, `finished_awaited_children`, the `add_event` CTE, the three SQL-function wrappers, the `enqueue` trace read); unfiltered only where B-2115 lists, plus `pg_notify` (empty payload). (2) `claim_jobs`/`reap` only call the RT-1 definers; `mark_reap`, `upsert_worker`, `beat_worker` touch only `workers` (no client data). (3) F5 (a), (b) closed; (c) soundly reasoned (no reader relies on an id alone); (d) comments only (finding 3). (4) No existence or lock oracle: ids server-generated uuid7; every unique key starts with `tenant_id`; naming B's run, step child, invocation, workspace or project in an insert gives 23503 like an unknown id (composite FK on `(tenant_id, id)`); client-filtered UPDATE / FOR NO KEY UPDATE never qualify B's row; `enqueue` and reaper branch 4 name `uq_jobs__dedupe`.

## Findings

1. **Low.** `backend/src/shifde/runtime/repository.py:1138-1142` `principal_display` users read relies on RLS alone: under the owner role `principal_display(s, A, "user", <user only in B>)` returns that person's display name (the app role hides it unless the session is a platform administrator's). The cross-client case covers only the service-account branch (`test_repository.py:199-201`). Fix: `AND EXISTS (SELECT 1 FROM shifde.memberships m WHERE m.user_id = users.id AND m.tenant_id = :t)`, fall back to the id; add a user-branch case.
2. **Info.** B-2117 says `runs.lease_job_id` is written only by the claim and reaper definers; `transition` allows it (`repository.py:72`, RUN_UPDATABLE) and SETH `lifecycle._claim` sets it. Safe today (compared for equality only, `lifecycle.py:274`, `worker.py:232`). Fix: correct B-2117; the lifecycle port passes only the job id the claim returned for that (tenant, run), or a dedicated claim function checks `EXISTS jobs(id, tenant_id, run_id, status='leased')`. `input_ref`/`output_ref` are likewise caller-written via `transition`/`update_step`; B-2117's reasoning holds because every read filters by `tenant_id`.
3. **Low (RT-6).** `backend/src/shifde/ledger/usage.py:256` `v.setdefault("provider_key", provider_key)` lets a caller's `provider_key` win; `insert_usage_event` (`repository.py:1095`) passes values through. Fix: assign outright or refuse a caller-supplied one; enforces F5 (d) in code.

## Follow-ups for later runtime parts

- RT-7/RT-11: `runs.agent_id`, `orchestration_id`, `prompt_version_id` have no key; consider composite FKs to `artifact_versions (tenant_id, id, artifact_id)` (runtime_0001 unmerged) or every reader filters by `tenant_id`.
- RT-11/RT-12: the `scope` tuple passed to `list_runs`/`list_approvals` (`repository.py:205`, `:750`) is trusted; the service builds it from the caller's own workspace/project bindings; route tests cover workspace and project refusal.
- RT-4..RT-12: every caller passes the context's `tenant_id`, never one from a request body; `add_event` and `initiator_controllers` receive rows from client-filtered reads only.

Findings 1-3 and the follow-ups were handed to the RT-4 builder (same branch).
