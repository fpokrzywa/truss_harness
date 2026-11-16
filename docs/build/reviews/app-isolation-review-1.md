# app isolation review 1 (APP-1c OIDC mount, `secrets_0003`; `git diff 2682cb9 77ad459`)

Reviewer: `isolation-reviewer` (opus, fresh), 2026-11-16. Read-only; saved by the lead. Finding 1 goes to the APP-1 fix round.

**Verdict: APPROVED.** No way for one client to reach another's data; nothing loosens an existing check or allowlist entry. `shifde_installation_idp_secret(uuid)` refuses with 42501 whenever a client context is set, before reading anything; answers only for an active installation IdP (tenant_id NULL) whose secret is an active installation secret; returns the encrypted envelope only. EXECUTE revoked from PUBLIC, granted to `shifde_app` only; `search_path` pinned to `pg_catalog, pg_temp`; sets no `app.*` setting. Three layers stop an installation IdP pointing at a client's secret: `trg_idp_configs_secret_scope` (`identity_0004_oidc.py:174-184`), `shifde_resolve_idp` (`:62-63`), the new definer's `s.tenant_id IS NULL` (the composite FK does not check NULL-tenant rows). The callback pins a client IdP's session to that IdP's own client, only for an active member who is not the platform administrator. A flow started at A's IdP cannot be consumed at B's callback (consume definer matches IdP id and IdP's client).

Checked: `pytest -n 8 tests/app tests/identity tests/secrets tests/schema` in verify2: 435 passed; `tests/audit/test_cross_client.py -k pre_tenant`: 7 passed (parametrized over every pre-tenant action, covers `secret.idp_client_secret.resolved`).
- Allowlist edit in `test_function_schema.py`: new entry, docstring, one line in the exact-equality signature set; no rule or existing entry changed.
- New audit action: `audit.record` reads `current_tenant_id()` from the database, so client code cannot write a NULL-tenant row; one call site; readable only with the platform-administrator flag.
- B's active IdP still redirecting on login: not a leak; `/api/auth/config` lists installation IdPs only and IdP ids are uuid7.
- B-2262 (IdP-sync self-add audit already closed by `identity_0006`): holds.
- B-2264's three changed assertions: two equivalent under platform-level groups (B-928), one (404 instead of 200 `[]`) stricter.
- Removal spot-checks through a plugin outside the repo: without the session pin and without `_require_tenant_member`, `test_bs_idp_never_signs_a_person_into_a` fails; both match the builder.

## Findings

1. **Low, would cross clients only if two other layers also failed.** `secrets_0003_installation_idp_secret.py:48-53`: no test fails if `s.tenant_id IS NULL` or `s.status = 'active'` alone is removed. Fix: in `tests/app/test_oidc_cross_client.py`, as schema owner in a rolled-back transaction: disable the installation IdP's secret, assert no row; with `trg_idp_configs_secret_scope` disabled, point an installation IdP at B's secret, assert no row.
2. **Info, inside client B (accepted in identity review 4 item 7).** `identity/oidc.py:255`: anyone with B's IdP login URL can create rows in B's `oidc_auth_requests` (rate-limited); expired rows never cleared. Fix: retention job, carry_forward 92.
3. **Info, inside one client.** `identity/oidc.py:565-575`: client-IdP sign-in requires an active membership, not a role binding; a member with no binding gets a session pinned to their own client. No fix needed.
