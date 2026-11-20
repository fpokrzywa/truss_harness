# Isolation review 2: job 034 (#80 SP3-3 evaluation, PR #97), round-2 delta

**Verdict: APPROVED, 0 findings.** Reviewer: `isolation-reviewer`, 2026-11-20. Scope: `git diff 31bcb29 ceb5ccc` only (review 1 covered `origin/main...31bcb29`).

1. `evaluation/service.py`: the unlocked read and the locked re-read both use `repository.get_run(..., tenant_id=tenant_id)`, with `tenant_id` from the server-resolved release; nothing locks or updates a row without the organization.
2. `evaluate_cases` receives only outcomes and dataset cases loaded under the same `tenant_id`; one child process sees one organization's data from one evaluation.
3. `evaluation/assertions.py`: one child process, queue and threads per scoring pass, killed in `finally`; no module-level shared state; fixed argv (`-I` plus constant script), data on stdin, stderr discarded; only per-assertion pass/fail comes back.
4. `list_runs` scores only rows already on the organization-filtered page; rows past the budget stay `running` unscored.
5. The race check compares `case_runs` from two reads of the same organization's row.
6. No new routes, so no new cross-client tests are needed.
