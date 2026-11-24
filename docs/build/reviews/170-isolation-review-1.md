# Isolation review 1, job 170 (#280 SP7-4, PR #350) at 4fb8c25

APPROVED (isolation-reviewer, 2026-11-24). Read through git (`origin/main...4fb8c25`); no tests run; the merge commit is not covered.

Checked: assist internal tools (scope only from `call`, every id bound to the run's project, foreign and unknown ids `not_found`), `bound_request`/`_propose` (server state only), `run_binding`, `stored_run(s)`, `PlanService.readable`, `project_cost_rows`, proposal accept/dismiss (one 404, tenant-filtered claim, payload from the claimed row, release and target re-checked), retention (tenant on every statement, legal-hold skip, delete guard), worker wiring, import contracts, tests (not vacuous, nothing loosened).

Advisories (not blocking; 1-4 in #351):
1. `assist/service.py` PAYLOAD_READ checks project-level permissions; optionally also require the named connection/target readable.
2. `tests/assist/test_proposals.py`: tampered payload test covers only `release_id`; add `target_id`, `connection_id` (B, P2).
3. `tests/assist/test_tools.py`: add `seth.evaluations.read` with P1's release and a foreign `evaluation_run_id`.
4. `assist_0001` delete guard weaker than the run guard (follow-up #351).
