# Core: isolation review, round 1

Branch `core` at `0d51e95` vs main `c925793`. Reviewer: isolation-reviewer (opus), 2026-11-13. **Verdict: APPROVED** (0 blocking, 6 follow-up). `tests/core tests/schema -n 8`: 232 passed.

No cross-client path found and no approved safeguard weakened: `dbrole.py`, `migrations.py` unchanged; `Database()` still runs `check_app_role` and the `_clean_checkout` hook (`DISCARD ALL` + preset `app.*` refusal); `create_engine` only in `db.py` and `dbrole.py`; no new `set_config`, session-level `SET` or `app.platform_admin`; `options` adds only `-c statement_timeout=<int>`; tenant never read from a header (`deps.get_request_ctx`); other clients' objects → 404 (`problem.py`).

## Follow-up

1. `tests/core/test_cross_client.py:17-50`: also set principal and admin in transaction A; pool_size=1 two-request test through `deps.get_session`. Owner: core (fixed before merge).
2. `core/db.py:79-99`: savepoint desync (B-116 item 6): restore context on nested rollback. Owner: tenancy, before the first `set_tenant` caller.
3. `core/hooks.py:21-31`: `run_close_hooks` must clear `platform_admin`. Owner: core (fixed before merge).
4. `core/ratelimit.py:73-103`, `deps.py:28-33`: loopback peer always trusted for `X-Forwarded-For` (audit accuracy, not isolation). Owner: app (frontend proxy appends/overwrites; setting to disable loopback trust in production).
5. `core/sqlutil.py:13-41`: identifiers unchecked; `tenant_id`/`id` updatable; same response for not-found and version mismatch. Owner: core (fixed before merge) + each port using it.
6. `core/deps.py:45-52`: client-chosen `X-Request-ID` would set `app.request_id`. Owner: app (validate or generate server-side).
