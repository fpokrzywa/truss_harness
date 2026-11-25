# Isolation review 1: job 182 (#360, PR #371, head b105a8b)

Saved by the lead from the read-only isolation-reviewer, unchanged.

APPROVED

Scope: `git diff origin/main...HEAD` at b105a8b (merge-base 2a6050d), worktree S:/Development_2026/shi_work/agt_trus_harness/.worktrees/182-assist-context-objects-of. No migration, table, policy, grant, route or SECURITY DEFINER function in the diff. `build.md` not read; worktree clean.
Tests: I ran 14 tests (new ones plus adjacent isolation tests): 14 passed. A run of the 4 full test files hit my 580 s timeout (about 125 python processes on the machine), so the full files and full suite are NOT confirmed by me. I did not run the tests with the source change reverted; "fails without the change" below is by reading.

Blocking findings: none.

What was checked and holds:
- assist/service.py:797-826: the object is still read first through PlanService.get / DeploymentService.get / TargetService.get under the caller's scope (permission and tenant). Only then is the release project compared. It fails closed: no plan, no release, unknown release or other project all give False, then the one `context not found` 404. Forbidden and NotFound are folded at :829-840. The check comes before the insert, so nothing is stored.
- delivery/service.py:777 `plan_project`: every query filters on tenant_id (repository.get_plan `p.tenant_id = :t`; RELEASE_SELECT `r.tenant_id = :t`, joins on tenant_id; release_record rechecks tenant). It runs on the request's app-role session, whose context is re-applied after the settle commit (core/db.py:67 after_begin). It returns a project id only to the comparison, never to the caller.
- runtime/service.py:143 `run_committed`: by (tenant_id, initiating principal, key), all server-side (scope and stored request id), tenant rechecked. It returns a bool that only picks a branch. The run itself comes from RunService.create -> `_replay` (request hash, run:read, artifact visibility, execute), then runs.get and the existing `run_project_mismatch` check. Pre-checks (specialist, permissions, drift) and the requester re-derivation in run_route_job still run first.
- runtime/worker.py:424-435: registered kinds log job id, exception type and format_tb frames (source lines only; no locals, message, cause or context). The lease and settle path is unchanged. Nothing in the tree turns on local capture in tracebacks.
- assist/schemas.py:108: `Body` keeps extra=forbid and adds NUL and surrogate checks. The message names the field only; problem.py echoes loc/msg/type, never the input. Design section 5.3 rules are kept.
- Existing tests: the only removed lines are 5 import lines, each replaced by a wider import. No assertion, parametrization or skip changed. tests/board/conftest.py only adds `bound_target`; `target`, `plan`, `deployment` are untouched. Cross-client tests (test_sp7_tools `another_clients_ids`, `p1_key_gets_not_found_for_p2`) are unedited and passed.
- New tests, by reading, fail on origin/main:
  - P2 plan and deployment on a shared target, HTTP and MCP: were accepted.
  - NUL in text: reached PostgreSQL as a DataError. NUL in specialist: gave `specialist_unknown` echoing the value.
  - Both log tests: were `exc_info=True` (the route test raises with a marker in message and cause).
  - Committed-run recovery: was `budget_denied`.
  - Positive controls on the same target and key prevent vacuous passes, and 404 bodies are compared with an unknown id's.

Notes (non-blocking):
1. assist/service.py:824-826: a deliberate one-bit oracle inside one client. A caller who can read a plan but not its release (release_id shown null) learns, per project where they may write assist requests, whether the release is of that project. No id, name or other client is revealed. Recorded as B-4052 (1) and tested. Accept.
2. tests/assist/test_requests.py (`plan_project` loop) and tests/mcp_access/test_sp7_tools.py (`run_committed` loop): the other-client case switches session and argument together, so RLS alone makes it pass. Session of A with tenant_id=B (and the reverse) would prove the explicit filters. The `run_committed` wrong-principal case does prove its filter.
3. docs/build/DECISIONS.md B-4052 (4) says the committed run "was created within the cap". The code does not prove it: the key `assist:{id}` is accepted as an HTTP Idempotency-Key, so the requester could pre-create the identical run uncapped and have the recovery link it. It is their own run, client, project and permissions (no exposure), and the replay path already allowed it when a cap was left. Budget honesty only; possible follow-up: reserve the `assist:` prefix in runtime/api.py:57.
4. assist/service.py:1326: if the run vanished between lookup and create, cap 0 refuses priced calls only; under `allow_unmeasured` an unpriced specialist model still runs unmeasured (steps.py:347-383). "Could not spend" in B-4052 is slightly strong. Practically unreachable; no isolation effect.
5. Pre-existing, unchanged: worker.py:435 (built-in kinds, the run-advance job of a handover included), runtime/lifecycle.py:614 and core/problem.py:133 still log `exc_info`, and no `hide_parameters` is set on the engine. An unexpected database error in POST assist/requests (the inline path) or in the specialist's run can still log request text as SQL parameters. The NUL fix removes the known trigger; `hide_parameters=True` would close the class. Worth a follow-up issue.
6. assist/schemas.py:100: `ContextIn` stays a plain strict BaseModel; its fields are a Literal and a UUID, so no NUL path.
