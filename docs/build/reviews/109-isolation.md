# Isolation review: job 109 (PR #211, IdP test leaves the sign-in caches untouched), head 8427c38

Reviewer: isolation-reviewer (lead session after 6975aeb6, 2026-11-22). Verdict: APPROVED, 0 blocking.

1. `identity/oidc.py` `run_checks` now uses only `_fetch_discovery` and `_load_jwks`, which neither read nor write the caches. Before, one organization's IdP test popped and rewrote the shared `_discovery[url]` entry and wrote `_jwks[jwks_uri]`. Issuer check, endpoint egress checks and error mapping unchanged.
2. Caches stay process-wide and keyed by URL (public IdP metadata, not introduced here). A hit on another IdP row's entry fails `issuer_mismatch_discovery`, and the token's `iss` must match the configured and discovery issuers.
3. `admin/providers.py` `test_idp` (unchanged) calls `sc.require()` and reads the IdP row in the organization's `db_context` before `run_checks`. No new route.
4. Merge condition with PR #187 (084): keep this branch's `run_checks` (no pop, `_fetch_discovery`, JWKS never stored). Done in the lead's batch merge: 084's `_load_jwks -> (lifetime, keys)` kept, `run_checks` uses `_, keys = self._load_jwks(...)`; `tests/identity` 274 passed.
5. Only test change is the new `tests/identity/test_oidc_discovery_cache.py`. No cross-client, RLS schema or startup role check test touched; no migration, grant or RLS change.
