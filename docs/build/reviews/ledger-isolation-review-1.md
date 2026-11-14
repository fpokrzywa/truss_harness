# Ledger: isolation review, round 1

Branch `ledger` at `31dae44` vs base `d256dd2`. Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: BLOCKED** (2 blocking, 8 follow-up). `tests/schema tests/core tests/ledger -n 8`: 302 passed. Probes as `shifde_app` with tenant A, rolled back.

Fine: protected tests unchanged in effect (`test_rls_schema.py` and the role check unchanged; the scratch-fixture DROP affects only probes building their own `zz_*` function; the real guard is still checked on an untouched owner connection). `core/db.py`: role check, `DISCARD ALL`, preset `app.*` refusal intact; `prepare_threshold=None` only removes client-side statement caching.

## Blocking

1. `backend/migrations/versions/ledger_0001_ledger.py:95-98`: the SECURITY DEFINER `rate_card_versions_guard` (owner `shifde_owner`, BYPASSRLS) trusts caller-supplied `NEW.tenant_id` and runs before the INSERT's WITH CHECK, so the allowlist reason at `backend/tests/schema/test_function_schema.py:50` ("the insert policy already pins that") is false. With A's context, inserting a version naming B's card and B's tenant returns 23P01 "cannot start in the past"/"overlaps" instead of 42501 (random card → 23514): an oracle on B's card and its version ranges. The guard also took B's advisory lock and ran an owner UPDATE closing B's open version before the abort rolled it back. Fix: first statement raises 42501 unless `(NEW.tenant_id IS NOT NULL AND NEW.tenant_id = shifde.current_tenant_id()) OR (NEW.tenant_id IS NULL AND shifde.is_platform_admin_ctx())`, before the lock and lookup; the seed CLI sets the admin context; correct the allowlist reason; cross-client test (item 5).
2. `ledger_0001_ledger.py:358,363`: `budget_alerts` FK and `UNIQUE (budget_id, period_start, threshold)` ignore `tenant_id` (constraints bypass RLS). A inserted an alert naming B's budget id (OK; random id → 23503; B's existing slot → 23505). SETH `runtime/governance.py` inserts alerts with `ON CONFLICT DO NOTHING`, so a slot A takes first silently stops B's alert. Fix: `UNIQUE (tenant_id, id)` on budgets, composite FK `(tenant_id, budget_id) → budgets(tenant_id, id)`, `UNIQUE (tenant_id, budget_id, period_start, threshold)`, `deliberate_changes.yaml` entries.

Both need B's uuid7 ids, which no ledger path exposes to A; if that precondition is discounted they are "fix in this branch before the governance and runtime ports".

## Follow-up

3. `ledger_0001_ledger.py:234`: `usage_events.rate_card_version_id` accepts B's version id (FK existence oracle). Trigger: version must be global or the event's tenant's. Owner: ledger/governance.
4. Guard depth > 1 bypass (line 136): not reachable now (`shifde_app` 42501 on UPDATE, no CREATE, TEMP triggers run as invoker); any future owner-run code updating this table reopens it.
5. `backend/tests/ledger/test_cross_client.py:162`: add `budget_alerts` and `rate_card_versions` write cases and a version on another tenant's card, asserting 42501.
6. RLS-only lookups needing `tenant_id = :t` or a carry_forward note: `ledger/usage.py:103` (`_fill_attribution`: artifact_versions, runs by id), `ledger/pricing.py:183` (model_keys), `ledger/enforcement.py:102,126` (budgets_in_scope, quotas_in_scope), `ledger/budgeting.py:109` (runs join).
7. `usage.py:249-269`: `record_model_call_durably` writes under its `tenant_id` argument (no principal, no admin flag); a differing `values["tenant_id"]` is refused by RLS. Runtime must pass the run's tenant, never request input (carry_forward).
8. Missing runs/tenants FKs: no cross-client effect now; runtime adds the composite FK.
9. Downgrade correct.
10. Definer allowlist keyed by name: an overload of `rate_card_versions_guard` would pass. Owner: core/schema test.
