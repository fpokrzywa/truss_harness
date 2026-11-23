# Isolation review: job 098 (PR #221, CM-3 Databricks adapter)

Reviewer: isolation-reviewer (lead session 6975aeb6, 2026-11-22), round 2 head 231a5f5. Verdict: APPROVED, 0 blocking.

Checked: credential resolution scoped to the connection row's organization and the egress guard's organization check unchanged; OAuth token cache keyed on (tenant, connection, secret hash, token URL, client id, scope), no cache without ids, two-org test present; per-call `DatabricksSession`, no global state; `normalize_config`/`validate_credential` are pure and run after the permission/org checks; stored hosts not in normalized form are refused before any request; dev fake switch refused outside development/test (`test_dev_fake_switch.py`); cross-client 404 for get/PATCH/test/discover/invoke (`test_adapter_hooks.py`); startup role check, RLS schema test, cross-client modules, migrations and `tests/schema/*` untouched; no assertion weakened.

Note (non-blocking): `_DevFakes` keeps one process-wide fake workspace per host shared by all organizations; acceptable only because it is a development/test fake.
