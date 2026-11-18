APPROVED

Isolation review 2 of PR #42 (`factory/009-f-09-rest-adapter` @ 582eb7e vs origin/main), isolation-reviewer (opus), read-only. `uv run pytest tests/connections tests/adapters -n 4` in the worktree: 377 passed.

Round-1 findings, both closed:
1. C2 / FR-CON-011 (auth_method-only PATCH): `connections/service.py:515-526` re-checks `secret:use` when `config` or `auth_method` is present, comparing the effective destination `AdapterDescriptor.destination(auth_method, config)` (`adapters/base.py:76-83`) of the old row against the merged new values. Bearer→OAuth2 adds `oauth2_url` (even None) so the check fires; OAuth2→bearer removes it, check fires; bearer/api_key/basic swaps keep the secret on base_url only (no check, correct). `_validate_definition` (`service.py:245-262`) rejects `oauth2_url`/`client_id`/`scope` under non-OAuth2 methods on create and PATCH (merged config), also covering legacy rows. A newly bound secret still passes `_check_secret` (`secret:use`).
2. Route-level tests: `tests/connections/test_host_change.py:270-405`: oauth2_url added/changed/removed, auth_method-only flip, flip with token URL, flip away from OAuth2; each run without and with `secret:use`. Refused cases assert 403 mentioning secret:use, row unchanged, attacker server received nothing (would be 200 and fail with the check removed); with_secret_use cases assert 200 (positive control). Plus base_url-only swap needs no secret:use, and each OAuth2 field refused 422 under bearer on PATCH and create.

Other checks:
- Token cache not shareable: key `(tenant_id, connection_id, sha256(secret, oauth2_url, client_id, scope))` (`adapters/oauth2.py:101-107`); no caching without both ids; tenant and connection ids from the row (`service.py:638`, `:645-646`). Cached until 30 s before expiry, max 1024 entries, repr hides tokens.
- Token URL under egress policy: `egress.check_url` then POST through the connection's `egress.client()` (`oauth2.py:131-147`); connection test checks both base_url and oauth2_url (`rest_http.py:200-204`); oauth2_url absolute http(s), no userinfo, same `check_base_url` (`rest_http.py:39-49`, `:180-181`).
- No leaks: token-endpoint errors are fixed text; every token registered for redaction.
- Nothing weakened: no migration, no RLS/grants/roles/startup-check change.

No open findings.
