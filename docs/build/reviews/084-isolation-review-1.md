APPROVED

Isolation review of `factory/084-oidc-rate-limit-jwks` (PR #187, issue #140), by `isolation-reviewer`; saved by the lead (2026-11-22). 0 blocking findings.

1. `identity/oidc.py:246`: `run_checks` now calls `_load_jwks`, which stores nothing. A test never touches `_jwks` or `_last_forced`. An admin who tests an IdP that shares a `jwks_uri` with another organization's IdP can no longer reset the lifetime of that shared entry, install a key set in it, or use up the other organization's refresh window. Covered by three new tests in `tests/identity/test_oidc_jwks_cache.py`.
2. Fails closed: errors are still `OidcFailure("jwks_unavailable")`, an expired entry is removed before the refetch, and a failed test changes nothing.
3. Existing behaviour, not blocking: `run_checks` (:239) still rewrites the shared `_discovery[discovery_url]` entry. That forces an extra fetch, but it cannot poison the cache, because the issuer check still applies. Follow-up #189.
4. The test fetch is not rate-limited (B-2891): it adds outbound load and leaks nothing. No route, table or authorization change.
