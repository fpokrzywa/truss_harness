# Authz fix: isolation review, round 2

Branch `authz-fix` at `a8da7f8` vs `main` `d6a10e9`. Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: APPROVED** (0 blocking, 2 follow-up). `tests/authz tests/schema tests/core/test_skeleton.py`: 202 passed.

Checked: `pdp._load` (`pdp.py:189`) is the single grant source for `authorize`, `effective_permissions`, `missing_permissions`, `visible_scopes`, `ancestor_workspace_ids`; filter runs before the API-key scope mask; execution principals force `is_platform_admin=False`; a client binding the shared `platform_administrator` template now gains nothing; `authz_0002` CHECK key list equals `PLATFORM_ONLY_ACTIONS`, `permission_key` NOT NULL, `shifde_app` cannot write a NULL-tenant `role_permissions` row; upgrade validates, downgrade drops only the constraint; single revision chain. No protected test weakened (only `deliberate_changes.yaml` gains one entry).

## Follow-up

4. Owner admin/tenancy (role create/update): a platform admin's `effective_permissions` includes `platform:admin`, so the escalation guard lets them try to add it to a client custom role; the DB refuses with IntegrityError (a 500). Refuse `PLATFORM_ONLY_ACTIONS` keys with 422 before the insert, with a test.
5. Owner tenancy: `authz/service.py:94` `grant_role` and `check_role_binding` still let a client bind the `platform_administrator` template; harmless now but reads as admin. Refuse binding it, or hide it from client `list_roles`.
