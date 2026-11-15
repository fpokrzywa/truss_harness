# models_admin isolation review 1 (part 1, data layer): APPROVED

Diff `9cfad3e..4b08a71`. Reviewer ran `pytest -n 4 tests/models_admin tests/schema`: 203 passed.

Checked and found sound: RLS enabled and forced on all eight tables; `write_own` USING + WITH CHECK, NULL-tenant branch ANDed with `is_platform_admin_ctx()`; `shi_provider_access` readable by its client only, INSERT/UPDATE need the platform-admin context inside that client, no DELETE grant; `tenant_id`/`provider_id` not updatable; the 14 `deliberate_changes.yaml` entries justified; the new `PLAIN_FOREIGN_KEYS` entry (vendors) justified, guard fires before the key with the same 23514 for B's and unknown vendors; guards are invoker rights with schema-qualified tables and hold as owner; composite keys give the same error for B's and unknown ids; `pricing.model_keys` gives None for B's model.

Findings (not blocking part 1; 1, 2, 5 and 6 must be closed before part 2 merges):

1. `models_admin_0001_models.py:152-154`, `repository.py:148-152`: a client row can take `SHI_PROVIDER_ID` if inserted before `ensure_shi_provider` runs; `ON CONFLICT DO NOTHING` then hides it. Fix: check `(tenant_id IS NULL) = (id = SHI_PROVIDER_ID) AND (tenant_id IS NULL) = (connection_id IS NULL)`; `ensure_shi_provider` reads back and confirms `tenant_id IS NULL`; test a client row with the SHI id refused when no SHI row exists.
2. `ledger/pricing.py:109-125`, `:183-196`: a client rate card can price shared SHI models for itself (e.g. zero), bypassing budgets and charge-back. Fix: price shared models only from global cards, with a test.
3. `limiter.py:27-38, 83-85`: buckets are per client, so SHI's upstream quota is not enforced across clients. Part 2: platform-level bucket for shared models, or record acceptance.
4. Migration `:84-86, 138`: every client reads the shared provider's `settings` and `health_detail`. Part 2 must keep endpoint, deployment and key reference out; test the shared row's columns are client-safe.
5. `repository.py:425-446`, migration `:282`: switch `changed_by` is caller-supplied (tests pass None). Fix: set from `shifde.current_principal_id()` (trigger or WITH CHECK), NOT NULL.
6. Migration `:240`: `fallback_model_ids uuid[]` has no key. Part 2 validates fallbacks with `get_models(tenant_id=caller)` (SETH `_validate_route`), applies the SHI call-time gate to every fallback; shared models in `get_models`/`_VISIBLE` give a key violation instead of 422 for allowlists and routes.
7. Migration `:225`: `PRIMARY KEY (allowlist_id, model_id)` lacks `tenant_id`; holding two of B's uuid7 ids gives 23505 vs 23503. Server-minted ids keep it safe; `(tenant_id, allowlist_id, model_id)` closes it.
8. Migration `:101, 117`: guard functions set no `search_path` (low risk, invoker rights); add `SET search_path = pg_catalog, shifde`, also `connections_guard`.
