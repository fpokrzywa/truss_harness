# build isolation review 2 (parts 2a and 2b)

Reviewer: `isolation-reviewer` (opus), 2026-11-15. Diff `7f05da8..2db397a` (branch `build-2b`). Saved by the lead from the reviewer's report (the reviewer is read-only).

**Verdict: APPROVED.** No path from client A to client B's data, artifacts, connections, secrets, models or routes. No must-fix before merge.

## Checked

- The six ported files (`prompts`, `skills`, `executors`, `agents`, `tools`, `tool_runtime`) and `env.py` are byte-identical to SETH `3ba0e06`.
- `tool_runtime.py`: the only spec value that reaches the database is `binding.connection_id` (`tool_runtime.py:138, 206`), passed to `ConnectionService.open_for_use` (`connections/service.py:854-869`), which applies, in order: `get_connection(..., tenant_id=)`; the `_get_row` tenant check (110-114); `connection:use` (+ `connection:write` for write classes); `_own` (116-121); read-only refusal of writes (841); the egress guard built only in its own client's context. Spec `tenant_id`/`base_url`/`url`/`host`/`secret_id` are never read; URLs go through `join_url`, which refuses leaving the base host. Write classes are refused by REST (methods), MCP (non-read-only tools), SQL (READ ONLY transaction) and never retried.
- Validators: every lookup in `prompts`, `skills`, `agents`, `tools` goes through `Env`; B's ids give the same issue (path, code, message) as an unknown id.
- Mutation checks (scratch checkout `verify3`, reverted): removing the tenant filter in `build.repository.get_version`/`get_artifact` failed 8 tests (owner-session variant: `test_agents_tools` x2, `test_prompts_skills` x6); in `connections.repository.get_connection` + `models_admin.repository.get_route`, 3 tests in `test_agents_tools`; on the tool path, repository filter + `_get_row` removed still passed (`_own` held); with `_own` also removed both owner-session cases of `test_a_tool_naming_bs_connection_gets_the_unknown_id_outcome` fail and the egress-guard tenant check still stops the call. Tests are not vacuous.

## Follow-ups (none blocking)

- **F1 (part 4, `service.py` `_Env`)**: besides answering with the client id, the production `Env` must (a) apply visibility, workspace and project checks in `version`/`latest` and return None (same issue as unknown), filling `workspace_id`/`project_id`; (b) in `connection`, return None without `connection:read`, compute `usable_by_publisher` from `connection:use`, take `implemented`/`capabilities` from the registry (`RepoEnv` hard-codes True, `env_support.py:82-83`); (c) in `mcp_tool`, go through `ConnectionService.latest_discovery` (needs `connection:read`), accept only discovery kind `mcp_tools`; (d) in `model_problems`, use `models.problems_at` (SHI switch read for the caller's client, allowlist chain, keep the second tenant check); (e) in `schema_discovery`, read `discovery_of` only for a connection already shown readable; (f) in `permits`, use the artifact's own workspace/project; (g) bypass checks only when an approver re-validates (`check_visibility=False`) and only after the request is confirmed to be the caller's client's, with `reveal_names=False`; (h) build the `Env` per request from the `TenantScope`, never cached across clients; (i) port SETH's `version_spec` (`service.py:1073`, RLS only) with the tenant id.
- **F2 (part 4)**: run the cross-client tests of `test_agents_tools.py` and `test_prompts_skills.py` against the service `Env` too; today they prove the validators and repositories, not the production `Env`.
- **F3 (part 3)**: `RepoEnv` lacks `schema_discovery` and `permits`, which `pipelines.py` and `ontologies.py` call; add tenant-filtered versions with B-versus-unknown tests before those files land.
- **F4 (`kind_ops`, part 4b)**: the tool test panel loads the version through the tenant-filtered repository; write classes need `tool:test_side_effects` plus `acknowledge_side_effects` as SETH's `ToolOps.test`; it uses the caller's `ConnectionService`. B-1917 ("the approval gate is not in these files") gets a carry_forward row.
- **F5 (runtime)**: `invoke_once` runs with a `ConnectionService` built from the run row's own tenant and run principal, never from a job payload; write classes go through the approval gate; the `ontology.query` builtin (`executors.py:66-71`) is scoped to the client; `session` agent memory is keyed by tenant.
- **F6 (part 2b, small)**: no test covers a read_write connection used by someone with `connection:use` but without `connection:write`; B-1916 claims it, but `test_tool_runtime.py:349` only exercises a read-only connection.

## Not isolation

- **N1**: four positive controls in `test_tool_runtime.py` timed out (`timeout_s: 5`, line 227) under `pytest -n 8` in the reviewer's run; all 15 pass alone (~1 s per call). Expect gate flakes. (The lead's gate run at `2db397a` passed: 1261 tests.)
- **N2**: the assertion at `test_permissions.py:61` (copied unchanged from SETH) cannot fail (`-` binds before `|`); add a separate assertion that can.
