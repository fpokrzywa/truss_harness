# 056 (PR #137, issue #62) isolation review 1 — APPROVED

Reviewer: isolation-reviewer (opus), 2026-11-21, on `factory/056-oidc-expire-the-cached` head `2373662` vs origin/main. Read-only; no tests run by the reviewer.

No blocking findings.

1. Cache keying (`identity/oidc.py:166-167, 213-226`): still keyed by `jwks_uri` as on main; keys only check signatures. `iss` must equal the discovery issuer and the configured `idp.issuer`, `aud`/`azp` must match `idp.client_id` (`oidc.py:261-268`); identity maps by `(iss, sub)`. A JWKS URL shared by two IdP rows (Entra) cannot make a token cross clients. Unchanged by this PR.
2. Expiry and failed refetch (`oidc.py:216-219, 200-211`): an expired set is popped before the refetch; a failed fetch raises `jwks_unavailable` and writes nothing; no stale or other-IdP fallback. Fail-closed over repeated calls tested (`test_oidc_jwks_cache.py:174-200`).
3. Lifetime bounds (`oidc.py:141-149, 207-209`): `min(cap, max(60 s, ttl))`, cap 3600 s default, `max-age` digits only (`test_oidc_jwks_cache.py:151-171`).
4. SSRF guard (`core/ssrf.py:95-132`): `get_json_with_headers` uses the same `_request` (scheme check, `EgressGuard`, 200-only, JSON object, no redirects); `get_json`/`post_form` behave identically. Private address still refused (`test_oidc_jwks_cache.py:203-215`).
5. Tests: none removed or weakened; `fake_oidc.py` adds an optional `Cache-Control` header defaulting to none; no RLS or cross-client file in the diff; no new routes.
6. Non-blocking, pre-existing: `run_checks` (IdP `test` endpoint, `oidc.py:228-243`) calls `_fetch_jwks` directly, overwriting the cache entry for that URL and skipping the kid-miss rate limit; only stores what that URL serves. Follow-up #140.
7. Note: the injectable clock covers cache ages only; token `exp`/`iat`/`nbf` use `time.time()` (`oidc.py:260`); production uses `time.monotonic` (`app.py:102`).
