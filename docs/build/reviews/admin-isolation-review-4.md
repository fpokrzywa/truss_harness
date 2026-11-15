# admin isolation review 4 (part 3a: IdP administration, `identity_0006`, IdP-sync audit; plus tenancy B-1020a): APPROVED

Diffs `ce61324..c192ecb` and `883ea12..64a5f5c -- backend/src`. Reviewer: `pytest -n 4 tests/admin tests/identity tests/schema` 416 passed at `c192ecb`.

Checked and sound: `/orgs/{org}/idps*` take the client from `resolve_org`, check `idp:manage` before any load, filter `tenant_id` plus RLS; installation or B's id under A answers like missing; `/platform/idps*` check `require_platform_admin` first and read only NULL-tenant rows; `update_idp` only on a row loaded in scope, `tenant_id` not updatable; secrets created in the IdP's own scope, composite key + `trg_idp_configs_secret_scope` stop cross-scope secrets; `uq_idp_configs__scope_issuer` per scope, every IntegrityError one `IDP_CONFLICT`; SSRF via `OutboundHttp`/`EgressGuard`, discovery cache keyed by URL with issuer check; JIT and group sync refused on client IdPs (service 422 + check constraints); `identity_0006` definer SECURITY DEFINER, `search_path = pg_catalog, pg_temp`, qualified, EXECUTE to `shifde_app` only, same signature allowlist, refuses a client context; `bound_tenant_ids` server-side only; `_audit_group_sync` rows carry group name/id and counts, NULL actor, self-adds written; NULL-tenant audit rows need the admin flag with no client; B-1622 adaptations weaken no SETH assertion; B-1020a exposes nothing new.

Non-blocking follow-ups (sent to part 3b):
1. `identity/oidc.py:503-527`: bound-client `members_changed` rows store the signing-in person's `actor_ip`; NULL it for `idp_sync` rows.
2. `oidc.py:536`: self-add `target_label` from the IdP claim email; use `users.primary_email`.
3. `tests/identity/test_platform_groups.py`: bind in A and B and assert each gets only its own row; non-administrator sync gives no self-add, NULL actor, no user id/email.
4. `admin/providers.py:180`: `list_idps` cursor outside `db_context` (B-1020a pattern); move inside, page-2 test.
5. `core/ssrf.py` `OutboundHttp._request` reads the body without a size cap; `/idps/{id}/test` is client-reachable. Add a byte limit.
6. Open by decision (B-1625): identity review 4 item 5, installation-level sync row, carry_forward 11 and 86.
