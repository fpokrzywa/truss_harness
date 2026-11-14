# Connections follow-up (review 2 items 1-3): isolation reviews 3 and 4

Branch `connections-fu`. Reviewer: isolation-reviewer (opus, resumed), 2026-11-14. Saved by the lead.

## Round 3 (`4d86a02..a19c1f1`): BLOCKED

`pytest tests/connections -n 6` 49 passed.
1. **Blocking:** the C2 race stayed open when the caller sends `row_version`. `_expected_version` conditioned the write on the caller's version, but the checks ran on the row as read (R). Probe: a manager without `secret:use` read R of a secretless connection; the owner bound a secret; the manager's PATCH `{base_url: evil, row_version: R+1}` returned 200 and `/test` delivered the secret to `evil`. Same for `set_status`.
2. Item 2 holds: hidden connections answer `NotFound("connection not found")` on all 8 routes; `test_routes_cross_workspace.py` compares full 404 bodies, own 200 first. The remaining 403 (`connection:write` after `connection:use`) is acceptable.
3. Item 3 holds: B has its own listener.
4. No other write path binds a secret or changes a destination (`create` checks `_check_secret`; `record_test_outcome` writes health, status, `last_tested_at` only).
5. B-1514 (`record_test_outcome` without a version check) is an acceptable follow-up: no secret reaches an unauthorized destination.

## Round 4 (`a19c1f1..4ddce65`, B-1515): APPROVED

`update` and `set_status` always condition the write on the read row's version; a differing caller `row_version` gets 409 after authorization and before any other check. The R+1 probe now gets 409, the host is unchanged with the secret bound, and `/test` makes 0 deliveries to `evil`. Both race tests pass in `[none]` and `[after_concurrent_write]`; `pytest tests/connections -n 6` 51 passed. No new findings.
