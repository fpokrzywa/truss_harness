# Isolation review 3: job 036 (#83 SP3-6 Entra, PR #100), round-3 fix

**Verdict: APPROVED; findings of reviews 1 and 2 closed.** Reviewer: `isolation-reviewer`, 2026-11-20. Scope: `git diff 4107d57 e494ca9` plus `_entra_email` in full.

1. `identity/oidc.py:426-429`: member only when `(type(acct) is int and acct == 0) or acct == "0"`; absent, True/False, 0.0, "", "2", 1/"1" are not members; `#EXT#` in `preferred_username`/`upn` (any case) still marks a guest; a non-member always gets `verified=False`.
2. `oidc.py:495-503` is the only derivation of an Entra IdP's address, chosen by the IdP row's `profile`, not the request; the generic `email_verified` path cannot run for an entra IdP.
3. `oidc.py:678-707`: `link_email` passed only when verified; JIT refuses unverified.
4. `tests/identity/test_entra.py:369-409`: cross-client cases (no acct with `preferred_username=<victim>`; no acct with `xms_edov=True`, `email=<victim>`; acct False, "", "2", True): failed sign-in, no session, no identity, B's link still armed, no new user, `jit_requires_verified_email` audited; the fake really omits the claim; `:412-424` positive control for 0 and "0"; `:345` tests the `#EXT#` guard alone.
5. Runbook says `acct` is required.
6. No new route, query, grant, policy or organization-context change.
