# Isolation review: job 099 (PR #227, CM-4 Snowflake adapter), head cd2ff32

Reviewer: isolation-reviewer (lead session 6975aeb6, 2026-11-22). Verdict: APPROVED, 0 blocking.

Checked: JWT cache key (tenant, connection, sha256 of key+account+user) in the shared token cache, nothing cached without ids, 401 drops only its own entry; bearer token never cached; only module state is the patchable `clock`; per-session egress client; host built from a strict account pattern under `.snowflakecomputing.com` and the organization's egress guard; credential checks with fixed messages; dev fakes static and refused outside development/test; cross-client 404s (`tests/connections/test_snowflake.py`) and two-organization cache tests (`test_token_cache.py`); no schema, migration, RLS, grant or startup-role change; edited planned-adapter tests only move cases from snowflake to fabric with the same assertions.

Note (non-blocking): the cache key part is an unsalted sha256 of the private key (process memory only); a keyed hash would be stronger if keys ever reached logs.
