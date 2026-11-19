APPROVED

# F-10 isolation review 1: PR #59, job 019, `origin/main...origin/factory/019-f-10-approval-rules`

(Saved verbatim by the lead from the isolation-reviewer's report; the reviewer runs read-only.)

## Scope checked
- Changed source: `backend/src/shifde/runtime/policies.py`, `runtime/pipeline.py`, `governance/validation.py`, `build/pipeline_nodes.py`.
- Changed tests: two new files, `tests/runtime/test_f10_pipeline_approval_rules.py` and `tests/governance/test_f10_approval_rule_when.py`. `tests/governance/test_p8_policies.py` only gains rejection cases.
- No migration, no grant or RLS change, no new route. Nothing under `core/`, `alembic/`, `tests/schema/`, `factory/` or `.claude/` changed.

## Isolation findings
None blocking.

1. **Rule lookup is limited to the run's client.** `write_node_approval` goes through `_approval_rules`, then `_policies` (policies.py). That query names `p.tenant_id = :tenant`, and `scope_match` binds `:tenant` from `gate.budget_scope(ctx)`, which is `ctx.tenant_id`. RLS also applies. Another client's rule can only match if it is that client's own row, and the explicit tenant predicate excludes those. `test_another_clients_rules_never_pause_this_clients_write` covers this: client B's rules are set up tenant-wide and also forged to name A's tenant id and A's workspace, and the test asserts no approval and no policy decision on A's run.
2. **Approval rows are created under the right client and scope.** `_insert_rule_approval` (pipeline.py) writes `tenant_id = ctx.tenant_id` and takes `workspace_id`/`project_id` from the stored pipeline run. The step, event, audit and decision are written in one `ctx.txn()`, which is lease-fenced. `find_approval` and `list_steps` filter by tenant and run. The approval's arguments carry the node, type, method and environment, never the body.
3. **Only authorised people can decide the approval.** `ApprovalService.decide` (runtime/service.py, around line 900, unchanged) requires `approval:decide` on the approval's scope. It also requires `required_permission` when that differs from the default. Lock-time checks require the approval and its run to belong to the caller's tenant. Separation of duties is still enforced. `approver_permission` is still validated against known permissions: `no:such` and `platform:admin` are rejected in test_p8.
4. **Lease loss (C1) is handled.** A lost lease raises `LeaseLost` before the approval transaction, so nothing is written. A pending approval under a foreign lease token raises `LeaseLost`, with no write-ahead step and nothing sent. A lease lost during an approved write leaves a `running` keyed step, which recovery parks as `indeterminate_side_effect`. Tests cover all three cases.
5. **Approvals are only added, never waived.** A granted upstream approval node does not satisfy the rule (tested). Only an operator's `retry` decision on an indeterminate step skips the rule check. That step can only exist after a write-ahead, which already passed the rule, and `run_tool` does the same.
6. **The safeguard tests were not weakened.** No cross-client test, RLS schema test or startup role check file is in the diff.

## Ways a rule can be bypassed inside one client (advisory, not cross-client, not blocking)
- **A1, pipeline.py `_write_node` and pipeline_nodes.py `TARGET_ENVIRONMENT`.** The pipeline author declares `target_environment` in node config, and it is matched exactly and case-sensitively. An author can leave it out, or write `Prod` instead of `prod`, and so avoid a `target_environments`-only rule. A `data_export` or `side_effect_classes` rule still applies. Suggested fix (follow-up): take the environment from the connection, which an administrator controls, or normalise case. At minimum, document that `target_environments` is only as reliable as the author's own label.
- **A2, pipeline.py `_write_node` and policies.py `write_node_approval`.** Every pipeline write, including `api_request` with DELETE, is classed `write_non_idempotent`. A rule naming only `destructive` (or `write_idempotent`) never pauses a pipeline DELETE or PUT. Suggested fix (follow-up): class by method, with DELETE as `destructive`, or match a pipeline write against any write class.
- **A3, policies.py `_approval_rules`.** This existed before the change and was only refactored. When several rules match, only the first rule's `approver_permission` is stored. A stricter permission on a later rule is not required. Suggested fix: store every required permission, or document the precedence.
