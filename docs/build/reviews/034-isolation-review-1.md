# Isolation review 1: job 034 (#80 SP3-3 evaluation, PR #97)

**Verdict: APPROVED, 0 blocking findings, 2 notes.** Reviewer: `isolation-reviewer`, 2026-11-20. Scope: `git diff origin/main...HEAD`, commits 7c3be45..31bcb29. Code read; tests not run by the reviewer (the gate runs them).

## Checked

1. Migration `evaluation_0001_datasets_runs.py`: both tables RLS enabled and forced, one `tenant_isolation` policy (USING and WITH CHECK on `tenant_id = shifde.current_tenant_id()`); every foreign key includes `tenant_id`; `shifde_app` gets SELECT/INSERT on datasets, SELECT/INSERT/UPDATE of result columns on runs, no DELETE, owns nothing; guard functions SECURITY INVOKER, fixed search_path, EXECUTE revoked from PUBLIC. `test_rls_schema.py` covers both tables.
2. Repository and service code: every statement in `evaluation/repository.py` filters on `tenant_id`; so do `packaging.repository.get_dataset_placement`, `models_admin.repository.own_model_id_by_key`, `runtime.repository.family_cost`, `policies.run_cost`; `release_record`, `dataset_in_project`, `run_outcome` re-check the row's `tenant_id`.
3. Routes (six): another client's dataset, release or evaluation run gives the same 404 as an unknown id; lists filter by organization; error bodies reveal nothing.
4. Solution versions' `dataset_id`: checked at create, seal and manifest against this client, workspace and project; digest from the checked row (closes 029 finding 3, B-2703).
5. Run overrides: service-only argument, targets from `own_model_by_key` only, child runs read the root payload by `tenant_id`; model governance gate still applies.
6. Family cost cap: tenant- and root-filtered.
7. `require_passing_evaluation`: organization from the session setting; reads and writes only that organization's rows.
8. Case runs started as the requester through `RunService.create`; `case_runs` writable only on queued -> running.
9. Audit records carry the release's tenant, workspace and project.
10. Cross-client tests `tests/evaluation/test_routes_cross_client.py` (with a meta-test requiring one per route) and `tests/packaging/test_routes_cross_client.py` for `dataset_id`.

## Notes (same organization only, not blocking)

- N1 `evaluation/service.py` `_score_values` / `CaseResult`: a user with `evaluation:run:read` on the release's project sees per-assertion pass/fail, latency and cost of case runs without a run-read check on those runs (documented as intended); output text is never returned.
- N2 `runtime/steps.py` `_run_overrides`: if a case run's root input payload is scrubbed while the run is live, its cost cap and model overrides stop applying. Current retention purge only touches ended runs.
