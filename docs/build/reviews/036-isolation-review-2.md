# Isolation review 2: job 036 (#83 SP3-6 Entra, PR #100), round-2 fix

**Verdict: BLOCKED, finding 1 of review 1 narrowed but not closed.** Reviewer: `isolation-reviewer`, 2026-11-20. Scope: `git diff 44afc70 4107d57`.

1. `identity/oidc.py:416-431` (`_entra_email`): a guest is detected only by `acct` 1 or `#EXT#` in `preferred_username`/`upn`. `acct` and `upn` are optional claims; a token with neither is treated as a member. A real B2B guest's `preferred_username` is their home address, so without `acct` a guest gets a verified `link_email` and can complete another client's armed link (or JIT). Fix: fail closed: verify only when `acct` is present and exactly 0 (int or "0"); anything else (absent, 1, unknown) is unverified; keep `#EXT#` as a second guard.
2. `tests/_support/fake_oidc.py:127` defaults `acct: 0`, hiding finding 1; `test_entra.py:345` `ext_username` case is not what Entra sends. Add cross-client cases with `acct` absent and plain `preferred_username=<victim>`, and with `xms_edov=True`, `email=<victim>`: no link, no identity, no user, link stays armed.
3. `docs/runbooks/entra-live-validation.md:23`: state that `acct` is required.

Checked fine: `xms_edov` must be `is True`; `#EXT#` check case-insensitive; unverified addresses never reach `link_email` or JIT; `acct` True counts as guest; no other cross-client path in the delta.
