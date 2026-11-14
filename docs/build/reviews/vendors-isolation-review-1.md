# Vendors: isolation review 1

Diff `280474f..ccb64c7` (branch `vendors`). Reviewer: isolation-reviewer (opus), 2026-11-14. Saved by the lead from the reviewer's report.

**Verdict: APPROVED** (0 blocking, 3 follow-up). Clean detached worktree at `ccb64c7`: `tests/vendors tests/audit tests/schema -n 8` 251 passed; a temporary probe ran as `shifde_app` on per-worker scratch databases (55450), deleted afterwards.

## What holds

- **RLS and grants** (probed as `shifde_app`): RLS enabled and forced, owner `shifde_owner`. `read_global_or_own` (SELECT: NULL or own tenant) and `write_own` (FOR ALL, same expression in USING and WITH CHECK: own tenant, or NULL only with the platform-admin context). Grants SELECT, INSERT, column-wise UPDATE; DELETE and TRUNCATE refused; `key`, `created_by`, `tenant_id`, `category` not updatable. A sees 8 global rows plus its own; A's UPDATE on B's or a global row affects 0 rows; A's INSERT for B, an unknown tenant or NULL is refused by RLS before the FK or unique check (no existence oracle). The platform-admin context does not open private rows.
- **Route scoping:** every `/orgs/{org}/vendors*` route takes its client only from `ScopeDep` (`tenancy.dependencies.resolve_org`); `_require` runs before any lookup; another client's id gets the same 404 as a made-up id; `update` rechecks the tenant (service.py:263). Platform routes require `platform:admin`, which the PDP never grants to an API key (authz/pdp.py:200); `_get_row` hides tenant rows in the platform context.
- **Global catalog:** a client cannot write it (403 at the route, refused by the database).
- **Audit allowlist (B-1304):** a NULL-tenant `vendor.created`/`vendor.updated` row is accepted only with the platform-admin flag (audit/service.py:239), enforced again by the `audit_events` policy; the only caller is the platform path behind `platform:admin`; client changes are filed under the client's tenant; a client context without the flag gets `NotInstallationAction` (tested).
- **Conformance evidence (B-1302):** `checks.detail` stays inside the service; `list_conformance_runs` passes runs through `client_checks` (service.py:51, :97), an allowlist of name, status and capabilities; `/adapters` and `/capabilities` expose only `run_id`, `finished_at`, `suite_version`. The regression test fails if `detail` is let through.
- **Downgrade:** `DROP TABLE` removes policies and trigger; covered by `tests/core/test_skeleton.py::test_every_migration_downgrades_and_upgrades_again`.
- **Cross-client tests:** `test_every_vendor_route_has_a_cross_client_test` pins all 12 routes; own object 200 first, then B's id under A's and B's org gives the unknown-id 404 body; B's row checked unchanged in the database.

## Follow-ups

1. **Column grant wider than the code** (owner: vendors). `vendors_0001_vendors.py:98-99`: `GRANT UPDATE` includes `capabilities`, which no code path updates; A could change its own row's capabilities by raw SQL (no cross-client effect; SETH parity). Fix: drop it from the grant, or record the parity reason in B-1301.
2. **Installation-action branch skips the no-client-context check** (owner: audit). `audit/service.py:239`: for `ADMIN_INSTALLATION_ACTIONS` only the Python-side flag is checked, unlike `PRE_TENANT_ACTIONS`. No such caller today. Fix: also require `_has_no_client_context(session)`, with a test in a client context with the flag set.
3. **Defence in depth in `_get_row`** (owner: vendors). `vendors/service.py:164` relies on RLS alone in the tenant context. Fix: refuse a row whose `tenant_id` is neither None nor `self.tenant_id`, with the same 404.
