# Isolation review 1: job 029 (SP3-1 packaging, PR #87, head 17cd2b85)

Reviewer: `isolation-reviewer` (opus), 2026-11-19. Filed by the lead from the agent's reply (the agent is read-only).

**APPROVED.** No path from one client to another client's data found.

Checked and correct:
- Migration `backend/migrations/versions/packaging_0001_solutions.py`: both tables RLS enabled + forced, one `tenant_isolation` policy (USING and WITH CHECK on `current_tenant_id()`), no permissive policy. Composite `(tenant_id, ...)` FKs to tenants, workspaces, projects, solutions, artifact_versions (entrypoint FK ON DELETE RESTRICT). `shifde_app` grants: solutions SELECT, INSERT, UPDATE(status); solution_versions SELECT, INSERT. No app-owned objects, no SECURITY DEFINER. Guard trigger SECURITY INVOKER, fixed search_path, reads the entrypoint under caller RLS with `tenant_id = NEW.tenant_id`. Permission seeds are global templates (`tenant_id` NULL); `packaging:release:withdraw` privileged.
- `packaging/repository.py`: every statement keyed on `tenant_id`, including the `list_versions` cursor subquery.
- `packaging/service.py`: `_solution` checks tenant, workspace, project, then the permission check with `hide_unless=READ`, one 404 either way; `get_version` also checks `solution_id`; `list` filters by tenant and project.
- Pins: `direct_pins` (`build/repository.py:352`) joins on `tenant_id`; build pin and pipeline-ref keys are composite, so a pin cannot reach another client's version, transitively either.
- `version_access`, `ConnectionService.readable`: within-client lookup, one answer for unknown and foreign ids.
- Tests: `tests/packaging/test_routes_cross_client.py` covers all 7 routes, checked against the router; refusals byte-identical to unknown id/org; B's rows unchanged. All 7 routes classified SCOPED in `test_workspace_routes.py` with W1/W2 cases.

Non-blocking findings (follow-ups):
1. `packaging/service.py:835-855` `_version_out`, within one client: a solution reader sees the entrypoint's and every transitive pin's `version_id` and `spec_hash`, even for artifacts the reader cannot see (build's `_pin_out` redacts hidden pins and never returns `spec_hash`). Fix: redact for pins not `version_access(...).visible`, or accept and record.
2. `service.py:857-888` `_check_entrypoint`, `_check_connections`: entrypoint and connections need only be readable by the caller, not in the solution's workspace/project. Design should state whether cross-workspace entrypoints are allowed; issue #80+ enforce it.
3. `schemas.py:621` / `service.py:891`: `evaluation.dataset_id` not validated; any UUID stored, including another client's. Nothing revealed today. Evaluation and seal issues must resolve it within client and workspace/project (one answer for unknown and foreign) with a cross-client test.
4. `build/repository.py:352` `direct_pins`: pipeline `connection` and `model` refs dropped by the join. Not a leak; when seal adds them, keep the tenant-keyed join and refuse shared (`tenant_id` NULL) model refs per call 15.
5. Test gap: no test has a W1-only user name a W2 project-private agent as a W1 solution's entrypoint and expect 422.
