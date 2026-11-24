# Isolation review 1, job 175 (#288 SP8-3, PR #357) at a86fa42

APPROVED (isolation-reviewer, 2026-11-24; `tests/mcp_access/test_sp7_tools.py` and `test_isolation.py` run: 32 passed; saved by the lead from the reviewer's report, shortened). No migration, policy, grant or definer in the diff. No blocking finding.

1. Client scope: the three tools take no organization, workspace, actor or cap argument; project resolved by tenant id and visibility rechecked; board reads keyed by tenant and `visible_scopes`.
2. `not_found` uniform; `tool_not_allowed` decided before arguments are parsed; `assist_ask` not in the default set.
3. Spend cap covered inline, in the job and on recovery; only ever lowered; payload cap server-written, tenant-checked and immutable; cannot be charged to another client.
4. Tool-failure log narrower than before (type and frames, no message). 5. Audit carries no arguments or results.
6. Removed lines in existing tests: the nine-to-twelve name and count, the 422 case, one import, one comment. No cross-client test weakened.
7. Cross-client and P1/P2 cases exist for every id. 8. B-4022 fails closed.
9.-10. Not blocking: recovery recomputes the router worst case at the rate then in force (B-4020); upgrade workers before allowing `assist_ask` (an older worker ignores `payload.max_cost`).
11. Not blocking (not in the diff): a plan or deployment on a workspace-level target counts as "in the project" even when its release is another project's (`assist/service.py:817-819`).
12. Not blocking (not in the diff): `runtime/worker.py:412` logs an unexpected `assist.route` error with `exc_info=True`.
13. Not blocking: no MCP case passes a P2 object as context of a P1 request with a key that reads both.
