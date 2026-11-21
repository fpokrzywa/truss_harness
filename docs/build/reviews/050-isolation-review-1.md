# Isolation review 1: job 050 (#47, PR #122), branch head f5e9959

**APPROVED** (isolation-reviewer, opus, 2026-11-21; recorded by the lead, the reviewer is read-only).

No way found for one client to reach another's data:
1. `audit/repository.py:94` `find_event`, `audit/service.py:438` `find_details`: required `tenant_id`, explicit filter on top of forced RLS; callers pass server-side tenants only (caller's client in `_job_details`, job row's tenant in `run_provider_job`).
2. `models_admin/repository.py:164,:204`: `enqueue_provider_job`/`get_provider_job` filter on tenant, kind and `payload->>'provider_id'`; B's job, B's provider and unknown all give the same 404.
3. `_job_target`/`_enqueue`: `model:manage`; another client's provider 404, shared provider 403; `ConnectionService._own` checks the connection's tenant.
4. `run_provider_job`, `runtime/worker.py:281`: job re-read under its own tenant; requester re-derived by `authz.execution_principal`; non-members fail closed.
5. `models_admin/api.py:122,:134`: org-scoped dependency, `model:read`, provider checked against the caller's client first.
6. No migration, grant, role or RLS change.
7. Tests: `tests/models_admin/test_routes_cross_client.py:745` covers both new GET routes; scoping table `tests/authz/scoping/test_workspace_routes.py:582`; worker cross-client test in `tests/runtime/test_worker.py`.

Notes (non-blocking, follow-up issue filed): (8) the result lookup keys on correlation id `model-health-check-<job_id>`/`model-discover-<job_id>`, and inbound `X-Request-ID` may take those values; only the worker writes those actions today, so within one client only and not exploitable now. (9) No worker-level test of a forged `shifde.jobs` row naming another client's provider or principal. (10) Discovered models and health detail are now kept in the client's own audit details (scrubbed).
