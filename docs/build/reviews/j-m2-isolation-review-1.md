# Isolation review 1: PR #37 (job 002, issue #2, Item 13 / J-M2 capture_content setter)

Reviewed `231d3c4` (diff against origin/main). Reviewer: isolation-reviewer (opus). Saved by the lead.

**APPROVED.** No client can read or change another client's tenants row or settings.

Why it holds:
- `shifde.tenants`: RLS enabled and forced; `tenant_isolation` covers all commands with the same rule in USING and WITH CHECK (row id = client in context, or platform-admin flag) (`tenancy_0001_tenancy.py:226`). The grant covers only the `settings` column (`tenancy_0006_settings_grant.py:25`); `shifde_app` owns nothing.
- The route uses ScopeDep (`tenancy/api.py:84`): client only from the caller's memberships or API key, never the path; an invisible client answers as unknown (404); suspended/archived answer 409 before the service; context set without the admin flag.
- The service (`tenancy/service.py:331-367`) requires `tenant:admin`, re-checks suspended/archived, changes only `capture_content` keeping other keys, guarded by row_version, audited in that client only (old, new, actor).
- Cross-client test (`tests/tenancy/test_cross_client.py:369-418`) is not vacuous: attempts on B use B's real row_version; B's settings, row_version and audit count unchanged; platform administrator with no binding gets 404 (`test_capture_content.py:121-153`).

Non-blocking findings (filed as a follow-up issue):
1. `tenancy_0006_settings_grant.py:25`: a column grant cannot be limited to one key; future app-role code in a client's context could rewrite that client's whole `settings` (incl. `unknown_cost_policy`, `ledger/enforcement.py:142`). Within one client. Fix: BEFORE UPDATE OF settings trigger refusing changes to keys other than `capture_content` unless the platform-admin flag is on.
2. No direct database-level test of the grant: as `shifde_app` with A in context, `UPDATE shifde.tenants SET settings = ... WHERE id = B` changes 0 rows; same with no client in context.
3. The schema comparison fails until the lead adds the deliberate_changes.yaml entry (gate item; added by the lead).
