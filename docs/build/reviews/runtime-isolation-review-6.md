# runtime isolation review 6 (RT-7 `agent.py`, `action.py`, run-env narrowing; `git diff 67d8f18 ac111ad`)

Reviewer: `isolation-reviewer` (opus, fresh), 2026-11-15. Read-only; saved by the lead.

**Verdict: APPROVED.** No way for a run of client A to reach client B's data. 5 findings: 1 Medium (inside one client, crosses workspace), 4 Low/Info.

Checked: `tests/runtime tests/ledger tests/schema` at ac111ad in `.worktrees/verify3` (`-n 8`): 590 passed, 1 failed (the known B-2107 case). (2) A run of A cannot reach B's connection, secret, route, MCP tool, tool version or ontology: `_run_env` re-reads the stored run and artifact with the client id; `version()` answers only for the pinned set; route and connection lookups require an id named by a pinned spec, then client-filtered reads; tests cover B's ids, A's unpinned ids, unknown ids and mixed ids with A's own controls, as `shifde_app` and schema owner. (4) No caller-built env or ref left in the run path (`AgentRunner.art` replaced by the stored row in `_run_env`; tool `VersionRef`s and pinned ids from `env.version()` only). (3) The three new keys are composite with `tenant_id` and correct targets (`artifact_versions (tenant_id, id, artifact_id)` / `(tenant_id, id)`); no existence or lock signal; `deliberate_changes.yaml` entries justified. (5) Tests non-vacuous except findings 4, 5.

## Findings

1. **Low (test).** `backend/tests/ledger/test_findings.py:315-344`: the changed test now raises ValueError before any session opens in both parametrizations, so the `app_db` case no longer shows RLS refusing B's values on the durable path (docstring says it does). No coverage lost overall (`tests/ledger/test_cross_client.py::test_tenant_a_cannot_write_rows_for_tenant_b_or_global_rows` proves the refusal; the new positive control fails without `set_tenant`). Fix: add an app-session case under A's context with `ledger.record_model_call(s, _call(b, ...))` expecting DBAPIError 42501, or correct the docstring.
2. **Medium (inside one client: crosses workspace, not client).** B-2161's rule (connection at client level or in the run's workspace/project) holds only for lookups through the run env. Tool execution reads the tool's connection by client id only: `runtime/steps.py:638` (`ctx.artifacts.conns.get_row`), `steps.py:1156` (`tool_runtime.invoke_once(ctx.artifacts.conns, ...)`). Fixture `far_tool` (pinned) with `conn_far` in another workspace: the env refuses it, `run_tool` would still call it. Fix: resolve the tool's connection through `ctx.env(...).connection` or the same `_in_run_scope` check in `_resource_state` and before `invoke_once` (fail as `connection_missing`), or record the narrower claim. Settle before RT-10.
3. **Low.** `runtime_0001_runtime.py:838` `ck_runs__target` does not require `agent_id` for `kind='agent'` or `orchestration_id` for `kind='orchestration'` (it does for pipeline and ontology); a NULL skips `fk_runs__agent_of_version` (runner fails closed). Fix: add both; add an `fk_runs__orch_of_version` (`:856`) case to `test_a_run_cannot_name_another_clients_or_another_artifacts_row`.
4. **Info (test gap).** No test reaches a connection at the run's own workspace or workspace+project, or refuses one from another project in the same workspace; routes in a run env are narrowed by name and client, not by the route's own scope (`build/service.py:1409`).
5. **Low (test gap).** A's agent naming B's `model_id` directly (not via a route) relies on `_invocable_model` filtering by client; all RT-7 agent tests stub `invoke_for_run`. Fix: a case with the real `_invocable_model`: B's model not found, A's succeeds.

## Follow-ups

- RT-8/RT-9: tool refs and pinned ids only from the run env (B-2162); pass `max_cost=`.
- RT-10/RT-12: `RunContext(db=)` is the app-role database.
- RT-11/RT-12: route cross-client tests for `start_action_run` (untested, B-2166), incl. a request body's workspace/project setting the run's scope.
- Finding 2 decided before RT-10.

## Round 2 (`git diff ac111ad 3199791`, fix round B-2170..B-2174): APPROVED

Same reviewer, 2026-11-15. At 3199791 in verify3: `tests/runtime tests/ledger tests/schema tests/models_admin` 654 passed + the known B-2107 failure; `tests/build tests/governance tests/ontology` 823 passed.

1. F2 closed on both paths: `steps._resource_state` (`steps.py:637`) and `_require_run_connection` before `invoke_once` (`:1168`) go through `ArtifactService.run_connection` (stored run re-read with the client id, connection read with the client id, `at_run_scope`); `invoke_once` uses the checked `spec["binding"]["connection_id"]` and `:1169` is its only caller; refusal is NotFound -> `AUTHZ_DENIED`, nothing sent, no retry. Proved by a test with `_resource_state` patched out and the four-scope test.
2. F4 correct: run-env routes allowed only at client, run workspace or run project scope (matches `model_routes`); 5 route scopes each also known in the caller's own env; connection scopes covered.
3. F3 correct: `ck_runs__target` requires `agent_id`/`orchestration_id` (justified `deliberate_changes` entry); two `fk_runs__orch_of_version` cases with controls; NULL-id test for both kinds.
4. Nothing weakened: the changed runtime tests only supply the run's own `agent_id`; the fixture change (rest/rest.http/active) leaves RT-7 assertions unchanged.
5. F1, F5 good: app-session test asserts `current_user = shifde_app`, 42501 for B's values, A's written, owner sees only A's row; real `_invocable_model` answers B's model id like an unknown one.

Info: (1) ontology tools via `ontology_exec.tool_query` (`steps.py:~1164`) are not covered by `run_connection`; source-mapping connections are read through `OntologyService` (B-2161 leaves them un-narrowed; inside one client): narrow or confirm with RT-8/RT-9. (2) Check-then-open: `run_connection` checks the row, then `open_for_use` re-reads it; a scope change between the reads could bypass the check (negligible now); later pass the checked row or the run's scope into `open_for_use`.
