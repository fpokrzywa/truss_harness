# Tenancy part 1 (data layer, deferred FKs, B-116 hardening): isolation review, round 1

Branch `tenancy` `09599bf..07556f2` vs main `851b940`. Reviewer: isolation-reviewer (opus), 2026-11-14.

**Verdict: APPROVED** (0 blocking, 7 follow-up).

The reviewer ran 265 tests (`tests/tenancy`, `tests/schema`, identity cross-client and platform groups); all passed. Probes ran as `shifde_app` on a scratch database, dropped afterwards.

## What holds

**Changed existing tests.** Every one refuses at least as strictly as before:
- "permission denied" is an earlier refusal than the RLS error it replaces.
- The two "fails closed until tenancy" tests now assert the trigger message.
- Fixture edits only create the client row first.

**Schema tests and deliberate changes.**
- The 21 `deliberate_changes.yaml` removals match the 20 tenant keys plus the sessions pin.
- `test_rls_schema.py` and `test_function_schema.py` only add rules, and the rules have probes.

**Plain foreign keys.**
- Every `PLAIN_FOREIGN_KEYS` entry has the BEFORE INSERT guard it claims: `check_role_binding`, `check_role_permission_tenant` or `usage_events_version_scope`.
- Each guard refuses another client's id with the same message as an unknown id.
- The only FK with a delete action is the `role_permissions` cascade, and it stays inside one client.

**Admin flag on `tenants`.** It reaches only organization rows and nothing in workspaces or projects. A test pins this.

**Tenancy tables.**
- RLS is enabled and forced, and policies have both USING and WITH CHECK.
- The owner is `shifde_owner`, and grants are narrow.
- `installation_settings` holds no client data.

**`shifde_my_memberships()`.**
- It is locked down like the identity definers.
- It lists a client only with an active membership plus a live direct or group binding.
- Role names come from that client's bindings.

**`trg_secrets_scope`.** Correct.

## Follow-ups

1. **Tenant rows can be created by any client context.** Owner: tenancy part 2.
   - `tenancy_0001_tenancy.py:226-228`: the WITH CHECK `id = current_tenant_id() OR admin` lets a context naming a not-yet-existing id insert that tenant. The probe did this with slug `ghost`.
   - This allows slug squatting, and `uq_tenants__slug` becomes an existence check on other clients' slugs.
   - Fix: add a RESTRICTIVE FOR INSERT policy WITH CHECK (`is_platform_admin_ctx()`), as B-910 did for memberships.
2. **Route tests still run against a memberships stub.** Owner: tenancy part 2.
   - `tests/identity/harness.py:131-140` still replaces `service.my_memberships` with a stub. Route tests, including `test_cross_client.py:880`, therefore run against the stub, while production calls the real definer.
   - Fix: drop the stub, rerun the route tests, and add a test that a member with no binding sees no client.
3. **Role-name join ignores the role's client.** Owner: tenancy part 2. Only a hostile owner could exploit it.
   - `tenancy_0001_tenancy.py:186`: the role-names subquery joins `roles` on id only.
   - Fix: add `AND (r.tenant_id IS NULL OR r.tenant_id = rb.tenant_id)`.
4. **Organization listing trusts the admin flag alone.** Owners: tenancy part 2 and admin.
   - `list_tenants` and `tenant_slug_exists` return every client whenever the flag is set, and some non-admin paths raise `platform_admin_context` (OIDC `_client_secret`, `add_member`).
   - Fix: the org-listing service must also check `principal.is_platform_admin`, and must leave the client-written `settings` and `description` out of the list shape.
   - The `ADMIN_FLAG_CATALOGS` reason, "holds no client content", is inaccurate for those two columns.
5. **Unknown-cost policy fails open.** Owners: ledger and runtime.
   - `ledger/enforcement.py:140-145`: `unknown_cost_policy` falls back to "allow_unmeasured" when the tenant row is not visible, which silently ignores a client's "deny".
   - Fix: refuse when the row is missing.
6. **A staff user id is readable by every client.** Owner: admin.
   - `installation_settings.updated_by` (`tenancy_0001_tenancy.py:54`) is readable through `read_all`.
   - Fix: keep it out of client shapes, or say so in the `global_tables.txt` reason.
7. **Raw SQL can tell whether a client id exists.** Owners: identity and tenancy part 2, with the session pin check.
   - `sessions_active_tenant_id_fkey` and the `tenant_id -> tenants` keys reveal this to raw-SQL `shifde_app`.
   - The ids are server-minted uuid7, so the risk is low.
