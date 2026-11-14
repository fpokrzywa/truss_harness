# Carry-forward: obligations one port leaves for a later one

Each later module's brief must include its section here. Strike an item (move it to "Done") only when the module that owns it is merged with the item pinned by a test.

## Deferred SETH tests (move with the module named; assertions unchanged)

| SETH test (3ba0e06 `backend/tests/`) | What it covers | Moves with | Deferred by |
|---|---|---|---|
| `test_core.py::test_log_redaction_masks_secrets` | log redaction | observability | core |
| `test_p3_egress.py` | EgressGuard / SSRF (core.egress, core.ssrf) | adapters (`testservers`) | core |
| `test_p6_units.py` (cron tests) | core.cron | build/runtime | core |
| `test_hardening.py`, `test_hardening2.py` (limiter, IP, proxy) | core.ratelimit, deps | app/identity | core |
| `test_p5_core.py` (safeexpr) | core.safeexpr | runtime | core |
| `test_p9_config.py` (settings, configtools) | core.settings, core.configtools | cli | core |

## Wiring and fixes owed

| Owner | Obligation | From |
|---|---|---|
| app | Add `MaxBodySizeMiddleware(max_bytes=settings.max_request_body_bytes)`; do not stack uvicorn `ProxyHeadersMiddleware` on loopback (client IP comes from `core.ratelimit.resolve_client_ip`, B-201) | core (Items 3, 5b) |
| app, runtime (worker) | Pass `db_connect_timeout_seconds` / `db_statement_timeout_ms` to `Database` | core (Item 18) |
| frontend (`proxy.ts`) | Append to `X-Forwarded-For` | core (Item 3) |
| identity | Rate-limit API-key failures and the OIDC callback with `SlidingWindowLimiter` | core (Item 5b) |
| identity | `PRINCIPAL_TABLES` entry that exists must be in `ported_tables.txt` (test that a non-SETH `shifde.sessions` with `own_row` fails) | skeleton review 6 item 4 |
| identity, tenancy | `sessions.active_tenant_id` pin checked against current bindings on every request before `app.tenant_id` is set; cross-client test for a revoked binding | skeleton review 5/6, B-116 |
| tenancy | B-116 items: composite `(tenant_id, id)` FKs between client tables; unique constraints include `tenant_id`; `shifde_app` has no TRUNCATE/TRIGGER/REFERENCES on client tables; no admin-flag OR in client-content policies; savepoint desync in `set_tenant`; optional `uuid` type for key columns | skeleton reviews 1–4 |
| every module | Cross-client tests assert `current_user = 'shifde_app'` | skeleton review 1 item 12 |
| governance | `rate_card_versions_guard` `set_config('shifde.ratecard_close', …)` vs catalog scan: allowlist with reason or another signal (B-117) | skeleton review 3/4 |
| tenancy | Savepoint desync: restore context on nested-transaction rollback in `core/db.py` (`after_soft_rollback` or per-savepoint snapshot), with a test, before the first `set_tenant` caller | core review 1 item 2 |
| app | Loopback trust for `X-Forwarded-For`: frontend proxy appends/overwrites with a test; setting to disable loopback trust in production | core review 1 item 4 |
| app | `X-Request-ID`: validate format/length or always generate server-side (it feeds `app.request_id` and audit correlation) | core review 1 item 6 |
| every port using `core.sqlutil` | Same response for not-found and version mismatch | core review 1 item 5 |
| observability | Readiness uses `core.health.migration_state` (multi-head, B-111/B-203) | core |
| ports of SETH policies on B-112's list | Settle each rejected SETH policy (USING (true) tables → `global_tables.txt` with reason, or tenant-keyed) under isolation review | skeleton |

## Done

(none yet)
