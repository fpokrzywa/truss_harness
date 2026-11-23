# 145 (#270, PR #311) lead isolation review 1 — 2026-11-23

APPROVED, no cross-organization path found (diff review).

1. Migration `delivery_0007_snowflake_kinds.py` only widens three CHECK constraints to allow `snowflake`; policies, forced RLS, grants, definer untouched; downgrade precheck counts per client and reports a number only.
2. Claim key (146 follow-up 1): `claim_key` = `address_key(snowflake_claim_identity(address))`; plain/privatelink/upper/`_`/`:443` forms collide → fixed 409. Definer keys on kind + address key.
3. Locator refusal (146 follow-up 2): non-`<org>-<account>` addresses refused 422 before any claim for every org; depends only on the address string. B-3947 (422 not 409) accepted.
4. Races: `lock_claim` after definer holding same xact advisory lock; `snowflake_binding_in_use` filters tenant + claim; `archive_target` takes same lock.
5. Plans refuse non-Databricks targets before writing rows; `gateway.job_context` takes platform kind from the leased job's own org's target.
6. Executor credentials: one org per file; `_checked` verifies job, executor, target, expiry, entry tenant = target org, platform, host = grant audience; fixed `CredentialUnavailable`; key file perms on POSIX.
7. Executor fake defaults to `production` env unless `SHIFDE_ENV` set, so refuses outside dev/test.
8. Cross-client tests for Snowflake targets on existing routes, cross-org URL forms, locator refusals, credential tenant/platform mismatches.

Non-blocking: N1 B-3934/B-3936 superseded by B-3947 (note at merge). N2 `register_target` ignores `lock_claim`'s boolean (harmless).
