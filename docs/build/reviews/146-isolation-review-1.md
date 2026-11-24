# 146 (#300, PR #309) isolation review 1: APPROVED

Reviewer: isolation-reviewer (opus), 2026-11-23, diff origin/main...origin/factory/146-sp6-2-follow-up.

- Credential binding (`snowflake/clients.py:101-119`, copier :293): refused before any request or subprocess unless host, tenant_id, account and user match the target; account/user fit the ACCOUNT pattern. Tested (org B credential on same host refused).
- Claim key (`addresses.py:124-137`): every URL form of an org-account reduces to one key; a collision only refuses a claim.
- Locator URLs refused for claims: correct (avoids two keys for one account).
- JWT redaction (`clients.py:156`): each signed token registered before use.
- Fake's looser JWT check (`fake.py:276`): acceptable; dev/test only, not imported by src.

Non-blocking follow-ups:
1. SP6-3 `register_target` must claim on `address_key(snowflake_claim_identity(address))` and refuse a target whose egress host does not reduce to the claimed identity; cross-client route test (B gets 409/404 on A's account in any URL form).
2. `clients.py:113` tenant_id comes from the credential file: consistency check, not authorization; fill it from the single organization owning the executor's credentials file.
3. Per-request JWTs fill the 2048-entry redaction LRU; before this runs in a multi-client shifde-worker, stop registering short-lived JWTs or give them their own registry.
