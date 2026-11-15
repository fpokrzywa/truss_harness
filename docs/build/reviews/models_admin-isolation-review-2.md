# models_admin isolation review 2 (part 2a, `8517e31..ba349a6`): APPROVED

Reviewer: `pytest -n 4 tests/models_admin tests/schema` 221 passed at `ba349a6`, plus two probes (B's private vendor; owner-role switch insert) in a scratch worktree, since removed.

Checked and sound: all 10 `{org}` routes through `resolve_org`, every repository call keyed by `self.sc.tenant_id`, B's provider/model/connection answered with the unknown-id 404 body; `ProviderPatch` has no `connection_id`/`vendor_id`; vendor gate answers B's private vendor (active or disabled) like unknown; B-1715 with the switch off answers every SHI path like unknown, `_shared()` queried every request; switch on: reads allowed, writes 403 before any adapter call; 4 platform routes check `require_platform_admin` first, same detail for any slug; installation-level audit rows readable by the platform administrator only and writable only with the admin context and no client set; `ShiAccessOut` carries no person id; `models_admin_0002` reserves the SHI id without a SHI row, keeps shared `settings`/`health_detail` empty (holds for the owner), `changed_by` NOT NULL with WITH CHECK `= current_principal_id()`, `search_path` pinned on the three guards incl. `connections_guard`; `ROUTE_TESTS` matched exactly against `router.routes` (15). Review 1 items 1, 4, 5 (for `shifde_app`) and 8 closed.

Non-blocking follow-ups (to part 2b):
1. `repository.py:99,116,200,235`: `shared: bool = True` default, and `get_models`/`_VISIBLE`/`read_global_or_own` show SHI rows to every client, so B-1715 lives only in the service. Make `shared` required; gate 2b paths on `shi_enabled`.
2. `models_admin_0002_shi_provider.py:60-63`: author check is RLS-only; the BYPASSRLS owner can insert any `changed_by`. Trigger setting it from `current_principal_id()`, or record the owner as trusted.
3. `tests/models_admin/test_api.py:21`: add a route test for B's private vendor (active, disabled) answered like unknown.
4. `deliberate_changes.yaml:390-392`: `ck_model_providers__platform` should cite `models_admin_0002`.
5. Review 1 items 2, 3, 6, 7 remain for 2b; the 2b platform health check of the shared row records status only (B-1712).
