# runtime isolation review 5 (RT-6 `steps.py`, cost items 9-11, Item 15 part; `git diff 4d64fed 67d8f18`)

Reviewer: `isolation-reviewer` (opus, same as review 4), 2026-11-15. Read-only; saved by the lead.

**Verdict: APPROVED.** No way for a run of one client to reach, write, add up or detect another client's data. Findings 1-2 Low (act in later parts), 3-4 Info.

Checked: at 67d8f18 in `.worktrees/verify3`, `tests/runtime ontology ledger governance models_admin` 583 passed, 0 failed. `steps.py` vs SETH 3ba0e06: only the client id in every repository call, no caller-chosen ids, and the Item 9/10/11/15 code. (1) `invoke_for_run` (`models_admin/service.py:1048-1071`) refuses no `max_tokens`, only lowers it to the model maximum, uses the run's scope (`ctx.run_ref()`) and the existing model check by client; `record_model_call_durably` (`ledger/usage.py:279-311`) opens its own session and sets the run's client context before writing (RLS WITH CHECK under `shifde_app`; tested with `app_db`). (2) `repo.run_cost` (`runtime/repository.py:1107`) filters by client and run (B's run answers 0 like unknown; owner cases); `estimate.call_cost` reads rates by client via `rate_keys`; `unknown_cost_policy` reads only the run's client row. (3) Tool path: the connection comes from the tool spec and resolves through the run's own connection service (`_resource_state` via `conns.get_row` `:638`; `invoke_once` via `conns.open_for_use`): B's connection not found, B's secret never opened; replay reads `list_steps`/`get_artifact` by client; ontology tools pass `tool_gate` before `tool_query` (`:878`, `:1151`). (4) Caller `provider_key` closed: `ledger.record` and `insert_usage_event` go through `_refuse_foreign_model_keys` (`ledger/usage.py:163-176`); provider key checked against the adapter registry (`models_admin/service.py:263`); no other INSERT into `usage_events`. (5) Review 4 items 1-4 fixed, tests non-vacuous. (6) No new existence or lock oracle (B's model id behaves like unknown under both cost policies; the durable write's `lock_timeout` is on the client's own rollup row only).

## Findings

1. **Low (before RT-10 wires the worker).** `ledger/usage.py:279-311` `record_model_call_durably` never compares `tenant_id` with `values["tenant_id"]` and accepts any `Database`; with an owner-role database a caller could write B's usage row under A's context (not reachable: `steps.py` passes `ctx.tenant_id` for both). Fix: raise `ValueError` on mismatch; RT-10 passes the app-role database as `RunContext(db=)` (startup check).
2. **Low (RT-7/RT-8/RT-9).** `steps.py:651-660`, `:1151-1160`: `ToolCall.tool` and `ToolPolicy.pinned_version_ids` are caller-built; `_policy` checks one against the other only. Not cross-client (B's connection refused by the run's connection service). Fix: build `call.tool` from `ctx.env.version()` and pinned ids from the run's pin set; forged-ref test.
3. **Info.** `steps.py:325` `_check_max_cost` runs before the gate; its refusal can show the worst-case price of a shared model whose SHI switch is off (global rate card, not client data).
4. **Info, test gap.** No test calls `record_model_call_durably` as `shifde_app` naming B's client.

## Follow-ups

- RT-7/8/9: pass `max_cost=`; finding 2. RT-10/12: `db=` is the app-role database. RT-7: limit `_Env` connection and route access in a run env (B-2146). Item 11 cached-token mapping remainder. `shifde_ops_stats` sums call and token volume per provider across clients: keep it platform-only.

Findings 1, 2, 4 (and 3 optionally) handed to the RT-7 builder.
