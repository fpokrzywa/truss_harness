# runtime isolation review 4 (RT-5 `ontology_exec.py`, `policies.py`, `schedules.py`, G2; `git diff 43f6745 4d64fed`)

Reviewer: `isolation-reviewer` (opus, fresh), 2026-11-15. Read-only; saved by the lead.

**Verdict: APPROVED.** No way for one client to reach, change or detect another client's data. Findings 1-3 Low (inside one client), 4 a test gap.

Checked: at 4d64fed in `.worktrees/verify3`, `tests/runtime tests/ontology tests/governance` 415 passed, 0 failed. Ported files vs SETH 3ba0e06: `ontology_exec.py` identical; `policies.py`, `schedules.py` differ only as B-2130, B-2132 say. (1) G2 tie keyed by client and version: `_anchor_run`, `run_target`, `pin_closure` read with the client id; run target columns have composite keys; `_run_reads` (`ontology/service.py:341-373`) admits only the exact version id of this ontology inside the run's pin set, or an agent run pinning `ontology.query`; B's ontology id fails in `artifact_row` like an unknown id; B's or an unknown run gets "run not found"; the unchecked path still checks the run's user for `ontology:query` (`_authorize_call`); the 8 refusals and 3-4 allowed cases in `test_run_tie.py` are not vacuous. (2) Policies: reads, run cost and earlier-alert checks filter by the run's client; B's tenant-wide rule, rules forged to name A's client or workspace, a 0-threshold rule, a 100 USD usage event on A's run and B's decision row are ignored, as app role and owner. (3) Schedules: pipeline, version, service account, bindings, audit row, read-back filter by client; `iam.guard_bindings` runs SETH's loop over the account's own bindings in this client; composite keys to pipeline, version, service account; B's ids answer like unknown ones; nothing claims due schedules (cron off, OD-8). (4) Review 3 items 1-4 closed (`build/service.py:668-724`, `:762-771`, `:1441`) with non-vacuous tests. (5) No new existence or lock oracle (no `FOR UPDATE`; identical messages for B's and unknown ids).

## Findings

1. **Low.** `build/service.py:707-724` `run_versions` takes the first non-null target column in a fixed order rather than the column for the run's kind; today it only refuses. Fix: choose from `run["kind"]` as `_run_env` does; refuse when null.
2. **Low.** `ontology/service.py:362-372`: the agent-tool rule also opens `get_object` and `action_inputs`, but only `ontology_exec.gate_query` checks private-ontology visibility. Fix: agent-tool rule for `query` only; actions through their own action run.
3. **Low.** `build/service.py:1441` `permits` does not check that the pinned version belongs to `ref.artifact_id`. Fix: re-read the version, require `artifact_id == ref.artifact_id`.
4. **Info.** `tests/runtime/test_ontology_exec.py`: add A's ontology with B's version id to `gate_query`/`run_query` (safe today, `version_of` filters by client).

## Follow-ups

- RT-6: every agent tool path calls `tool_gate` before `tool_query` with the run's own context.
- RT-7: an action started from an agent goes through its own `ontology_action` run.
- RT-12: route-level cross-client tests for schedules.
- Future cron runner: claim due schedules with each row's own `tenant_id` only.
- `usage_events.run_id`, `policy_decisions.run_id`: no key to `runs`; a `(tenant_id, run_id)` key would add a second guard (ledger/authz owners).
- Review 3 follow-ups (connections, routes, caller `provider_key`) remain open.

Findings 1-4 handed to the RT-6 builder (same branch).
