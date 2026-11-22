APPROVED

Isolation review of `factory/083-oidc-jwks-an-over` (PR #181, issue #139), by `isolation-reviewer`; saved by the lead (2026-11-22). 0 blocking findings.

1. `identity/oidc.py:141-154` (`cache_max_age`): the change only adds `try: int(value) except ValueError: return None`. An over-long max-age falls back to the default `oidc_jwks_cache_seconds`, still clamped at :214 to [JWKS_MIN_TTL, oidc_jwks_max_cache_seconds]. It fails closed: no unbounded lifetime, an expired set is still dropped, no new parsing path.
2. Cache keying (`_jwks`/`_last_forced` by jwks_uri, the issuer check at :182) is unchanged, and so is every table, policy, grant, route, job and role binding. Two IdP rows that name the same URI share one entry, as they did before this branch.
3. Tests: `tests/identity/test_oidc_jwks_cache.py` adds the 5000-digit case (default 900 s). No new route.
