# admin isolation review 3 (part 2: service accounts, API keys, platform group-binding route): APPROVED

Diff `9cfad3e..dfa2f0b`, checked at `688d08b`. Reviewer: `pytest -n 4 tests/admin` 38 passed.

Checked and found sound: a key's client comes from the key (`resolve_org`), a person in A and B with A's key resolves only A (`test_a_key_or_service_account_of_a_never_acts_in_b`); service-account and key lookups filter on `tenant_id` under RLS; B's service account, workspace or project gives the same 422 as unknown, B's key id the same 404; `_validate_scopes` refuses `PLATFORM_ONLY_ACTIONS`, `guard_grant` runs on the caller's own (scope-limited) set, rotation re-checks; `created_by` is always the caller; id-only mutators reached only after a load in the caller's client; refusals compared field by field; `POST /platform/orgs/{org}/group-bindings` checks the platform administrator first (one 403 whatever the slug), B's role 422 like unknown, trigger and `ck_role_bindings__tenant_scope` refuse B's scopes with one 403, audit only in the target client, self-adds recorded; `guard_bindings` requires every permission the service account's bindings grant; B-1603 test adaptations dropped or loosened no SETH assertion.

Non-blocking follow-ups:
1. `admin/credentials.py:121-130`: re-enable race (same client, as SETH): with no `row_version` the update is unconditioned, so "active" on an active account can land just after another admin disables it, skipping the guard. Keys stay revoked. Fix: always condition on the read `row_version` (B-1515 pattern).
2. `tests/admin/test_cross_client.py` group-binding route test: also assert the `tenancy.platform_admin.self_added` row in A and the generic 403 for an org-level binding naming B's id.
3. `admin/iam.py:516-545`: archived-client and role checks run in a separate transaction before the write; under the admin flag a disabled group can be bound (grants nothing). Re-check both inside `tenancy.bind_group_as_platform_admin`.
4. `identity/repository.py:644,656,686`: `touch_api_key`, `revoke_api_key`, `shorten_api_key_expiry` filter by id only; add a `tenant_id` filter.
