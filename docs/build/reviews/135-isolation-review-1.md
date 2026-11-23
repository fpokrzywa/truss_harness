# Job 135 (PR #297, SP6-2 Snowflake native adapter) isolation review 1

Reviewer: isolation-reviewer (opus), 2026-11-23. Head `04b60c0` vs origin/main `3788f01`. Written by the lead from the reviewer's reply (the reviewer is read-only).

**APPROVED.** Nothing blocks the merge; findings 1-5 must be fixed before the adapter is wired into target registration or the executor (follow-up issue).

## Clean
- Scope: no routes, migrations, RLS policies, grants, services or worker paths; nothing outside `adapters/native/snowflake` uses the adapter yet.
- JWT refactor: existing Snowflake connections unchanged (same `iss`/`sub`, header, claim order, token cache key (tenant_id, conn.id, fingerprint), `register_secret` still called).
- Credential bound to host: SQL API (`clients.py:76-85`, `:129`, `:148`) and `snow stage copy` (`clients.py:255`) refuse a host mismatch before any request or subprocess; redirects not followed; poll handle regex-checked.
- Private key: 0600 file in a fresh temp dir, deleted after; only the path on argv; minimal child environment; CLI output never returned; key not in `repr`.
- Adapter checks artifact, plan and receipt against tenant and target; never adopts unowned objects (`test_snowflake_isolation.py`).
- Supply chain: `snowflake-cli==3.33.0` matches the SDK at `1fd902b`; `tools/snowflake-cli.txt` is 3 comment lines plus the SDK's `infra/containers/snowflake-cli.requirements.txt`, byte-identical ignoring line endings; 66 packages, same versions, every requirement hashed (836 sha256), no index or URL requirements.

## Findings (non-blocking now)
1. `addresses.py:99-117`, `:128`: one Snowflake account has several URLs (plain, privatelink, org-account, locator.region), each a different claim key; two organizations could claim one account once Snowflake target registration uses it. Key on account identity (or at least fold privatelink), with a two-org test.
2. `clients.py:76-85`: credential `account` not checked against the host, yet signed into the JWT and passed as `--account`; validate `account` and `user` with the connection's account-identifier pattern (no leading `-`).
3. Wiring: `CredentialProvider` takes no tenant; when wired into `executor/credentials.py` it must check the entry's `tenant_id` against `ctx.tenant_id` as Databricks does.
4. `clients.py:119-126`: native SQL API JWT not passed to `register_secret`.
5. `clients.py:257-293`: CLI egress also reaches the account's stage cloud storage and OCSP; document in the egress allow-list and docstrings. 0600 is a no-op on Windows (relies on `%TEMP%` ACL).
6. `fake.py:192`: fake copier skips the host check; test/dev only, no action.
