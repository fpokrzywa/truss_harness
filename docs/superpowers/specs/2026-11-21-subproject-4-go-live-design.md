# Sub-project 4: go-live — short design

Date: 2026-11-21. Status: settled by the lead under charter section 3 (owner delegated). Contract: unified design sections 3, 5.1, 5.3, 5.4, 6 (row 4), 9; charter sections 2, 3, 7.

## 1. Intent and "done when"

**Done when (design section 6):** "Every remaining major audit finding is closed or accepted in writing; production mode, trusted-proxy and header settings, backup and restore, and process control are in place and tested. Only after this may real client data enter SETH."

The owner input "Where SETH will be hosted" is blank (charter 2). Its fallback applies: build the parts that do not depend on the host and record the hosting target as **waiting on owner input**. So this sub-project can end in two states:

- **Met locally:** everything below works and is tested on the local cluster and loopback, in production mode, and `python scripts/golive_check.py --local` ends with `GO-LIVE CHECK PASSED (local)`, listing any `pending-owner` rows of the register.
- **Go-live itself** (real client data allowed) waits on three owner inputs: the hosting target, TLS on the hosted PostgreSQL (STATUS "Waiting on owner input", B-009), and the written acceptances of section 7. `python scripts/golive_check.py --final` passes only when no register row is `pending-owner`. Until then STATUS says "sub-project 4 met locally; real client data still not allowed".

## 2. Scope and non-goals

In scope: production-mode refusals and a preflight report; API and front-end security headers; the supported proxy topology and its proof; `shifde-admin backup create|verify|restore` on `pg_dump`/`pg_restore`; health, signal and exit-code contracts for `shifde-api`, `shifde-worker` and `shifde-executor`, with a reference supervisor; the remaining retention gap of Item 20; the audit-finding register and the go-live check; runbooks.

Non-goals: choosing or provisioning a host; container images, systemd units, Windows services or cloud templates (written once the host is known, from the contract in 6.2); any deploy; DNS, certificates and TLS termination (they belong to the host's edge proxy); point-in-time recovery and WAL archiving (host-specific); encrypting backup files inside SETH (call 6); changing the hosted database.

## 3. What exists and what is reused

| Need | Already in this repo (reuse) | Gap closed here |
|---|---|---|
| Production mode | `core/settings.py` `_production_guards`: `SHIFDE_ENV` defaults to `production`; refuses local login without `SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION`, non-https `SHIFDE_ALLOWED_ORIGINS`/`SHIFDE_PUBLIC_BASE_URL` (loopback excepted), over-broad `SHIFDE_TRUSTED_PROXIES`; requires `SHIFDE_API_KEY_PEPPER`, `SHIFDE_SECRET_KEYS`; `__Host-` secure cookies; `shifde-admin config validate` (`core/configtools.py`); dev-only commands refuse outside development; startup role check (exit 3). `/api/metrics` already answers 403 in production without `SHIFDE_METRICS_TOKEN` (`observability/api.py` `_authorize_metrics`) | `/api/docs` and `/api/openapi.json` public (ARCH m9); `fake` model adapter registered in every environment (ARCH m10); missing metrics token, console logs and missing public base URL are not startup refusals; no go-live preflight |
| Proxy | `resolve_client_ip` believes `X-Forwarded-For` only from `SHIFDE_TRUSTED_PROXIES` peers (B-201, B-2280); `main.py` `proxy_headers=False`; `proxy.ts` (matcher `/api/:path*`) drops inbound `X-Forwarded-For`; carry_forward line 253 | No supported topology written down or tested end to end; web-proxied `/api` traffic all shares one address |
| Headers | none: no HSTS, CSP, frame or content-type header in `backend/src`, `frontend/src` or `next.config.ts` | Item 5a (F-14) and ARCH m8, open although findings.md lists them as SP1 scope |
| Backup | SETH `3ba0e06` `cli/backup.py` (`pg_dump -Fc`, manifest with digests, empty-target restore in one transaction), not ported (B-2221). `shifde_owner` has BYPASSRLS on both clusters (`scripts/dev_db.py`, `scripts/hosted_db.py`) | Port the idea; add catalog and isolation verification; target password never in argv (ARCH m2) |
| Process control | Worker: SIGINT/SIGTERM/CTRL_BREAK graceful stop (`runtime/worker.py` `_install_signals`), `workers` heartbeat rows, `/platform/status` worker window, exit 3 on an unsafe role. API: `/healthz` (`app.py`) and `/api/health` (`observability/api.py`), both without database access; `/api/ready` (503 under 5 s, B-2523). Executor: `last_seen_at` on every gateway call; stops only on `KeyboardInterrupt` (`executor/main.py`) | Worker has no health port (ARCH m16) and does not check the schema revision at start (ARCH m5); executor has no signal handling; no documented exit codes or restart policy |

## 4. Production mode (SP4-1)

All refusals are in `core/settings.py` `_production_guards`; each is one redacted line naming the variable; every process exits 2 on it (`settings_or_exit`).

1. **Docs.** New `SHIFDE_API_DOCS_ENABLED` (bool; default false when `SHIFDE_ENV=production`, true in development and test). When false, `create_app` passes `docs_url=None`, `openapi_url=None`; both paths answer 404. `tests/_support/openapi_snapshot.py` builds the schema from `app.openapi()` with `env="test"`, so `backend/openapi.json` does not change.
2. **Fake adapter.** `AdapterRegistry.load(include_fake: bool)`; its three callers (`app.py`, `runtime/worker.py`, `cli/main.py` `adapters verify`) pass `settings.env != "production"`. Creating a provider with `adapter_key="fake"` in production is then refused by the existing check in `models_admin/service.py` (`ValidationFailed`, 422 "not a model provider adapter"). For a provider row that already names it, `ModelsService.invoke_at` (beside the provider status checks) raises the existing `ModelUnavailable` (409 `model_unavailable`) "the model provider's adapter is not available in this environment"; a run whose step calls it ends `failed` without any provider call. `--fake-platform` is already refused outside development and test (`executor/main.py`).
3. **Metrics token.** `SHIFDE_METRICS_TOKEN` is required in production at startup (the request-time 403 stays).
4. **Logs.** `SHIFDE_LOG_FORMAT=console` is refused in production (`json` is the default).
5. **Public base URL.** `SHIFDE_PUBLIC_BASE_URL` is required in production (no fallback to the first allowed origin). Existing tests that build production settings (`tests/core/test_core.py`, `tests/adapters/test_p3_egress.py`, `tests/app/test_p8_review_r1.py`, `tests/cli/test_dev_rest_sink.py`, `tests/cli/test_sampledata.py`, `tests/executor/test_main.py`, `tests/runtime/test_service.py`) add the newly required values; no assertion changes.
6. **HSTS setting.** `SHIFDE_HSTS_MAX_AGE_SECONDS` (int, default 31536000, 0 = off) is defined here; SP4-2 consumes it.
7. **Register.** `docs/build/golive-register.md`: one table, columns `id | state | evidence`, `state` one of `closed`, `accepted`, `pending-owner`. `closed` names the issue, PR or test; `accepted` quotes the owner's words, date and where given; `pending-owner` says what the owner must decide. SP4-1 creates it with the rows of section 7.
8. **Preflight.** `shifde-admin config preflight [--env-file F] [--register PATH]`: runs `config validate`, then one line per check (`ok: ...` or `refused: ...`, never a value): at least one `SHIFDE_TRUSTED_PROXIES` entry unless `SHIFDE_TRUST_LOOPBACK_PROXY=true`; `SHIFDE_HSTS_MAX_AGE_SECONDS >= 15552000`; `SHIFDE_API_DOCS_ENABLED=false`; `SHIFDE_AUTH_LOCAL_ENABLED=false` unless the register row for REQ F-22 is `accepted`; through `SHIFDE_DATABASE_URL` (the app's normal setting) the role passes the startup role check and `migration_state` is `ok`; an unreachable database gives `refused: database unreachable`; the register parses and lists each `pending-owner` row as `pending: <id>`. Exit 0 only with no `refused` line.

Tests: `backend/tests/core/test_settings_production.py` (each refusal and message, no secret in output), `backend/tests/app/test_production_mode.py` (docs and openapi 404; registry without `fake`; 422 on creating a `fake` provider; 409 `model_unavailable` and no usage event on invoking a stored `fake` provider), `backend/tests/cli/test_config_preflight.py` (each line, unreachable database, register parsing, exit codes).

## 5. Trusted proxy and headers (SP4-2, SP4-3)

**Supported topology (call 2).** One edge reverse proxy terminates TLS for the SETH host name, routes `/api/*` straight to `shifde-api` and everything else to the Next server, and overwrites `X-Forwarded-For` with the real client address. `SHIFDE_TRUSTED_PROXIES` lists exactly the edge proxy's addresses. Next's own `/api` proxy stays for development; front-end setting `SHIFDE_WEB_API_PROXY` (`on`|`off`, default `on`, read per request in `proxy.ts`; `off` required in production by the go-live runbook) makes `proxy.ts` answer 404 with JSON `{"detail":"not found"}` for `/api/*`.

**API headers** (new `core/securityheaders.py`). Middleware order, outermost first: `RequestIdMiddleware`, `SecurityHeadersMiddleware`, `CORSMiddleware`, `MaxBodySizeMiddleware` (added in `create_app` between the CORS and request-id lines). Starlette sends a 500 from the catch-all `Exception` handler (`core/problem.py` `_unhandled`) from its outermost error middleware, outside this stack, so `_unhandled` adds the same headers through the shared `security_headers(settings)` function. Headers on every response:
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. In development only (docs are off in production), `/api/docs` and the openapi route get no CSP at all, so Swagger UI loads.
- `Cache-Control: no-store` only when the response has no `Cache-Control` already.
- `Strict-Transport-Security: max-age=<SHIFDE_HSTS_MAX_AGE_SECONDS>; includeSubDomains` in production when the value is above 0.

**Front-end headers** (`proxy.ts`). Matcher widened to `'/((?!_next/static|_next/image|favicon.ico|.*\\.(?:png|jpg|jpeg|gif|svg|ico|webp|css|js|map|txt|woff2?)$).*)'`. Per request it makes a nonce, passes it on the request as `x-nonce` and as the `Content-Security-Policy` request header (so Next applies it to its scripts), and sets on the response `Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-<n>' 'strict-dynamic'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, plus the nosniff, referrer, frame and COOP headers. HSTS is set by the edge or the API, never by Next.

Tests: `backend/tests/app/test_security_headers.py` (through the real `create_app` stack: every header on a 200, 401, 404, 422, 500 and a 413 from an oversized body; existing `Cache-Control` kept; HSTS absent outside production); `backend/tests/app/test_edge_proxy.py` (httpx `ASGITransport(app, client=("10.9.9.9", 50000))` as the edge peer listed in `SHIFDE_TRUSTED_PROXIES`, runs on Windows: the edge-supplied address reaches the audit row and the limiter key; an unlisted peer's header is ignored; a client-chosen value placed before the edge's hop is not believed); `frontend/src/proxy.test.ts` (headers, nonce differs per request, `x-nonce` request header, `SHIFDE_WEB_API_PROXY=off` gives the 404 JSON, matcher skips static assets); `frontend/e2e/headers.spec.ts` (sign in and open the main pages; collects `securitypolicyviolation` events through an init script and console errors; passes with none).

## 6. Backup, restore and process control

### 6.1 Backup and restore (SP4-4, SP4-5; `shifde.cli.backup`, `shifde-admin backup ...`)

Runs as the schema owner (`SHIFDE_MIGRATION_DATABASE_URL`), the only role that owns the schema and bypasses row-level security, so `pg_dump` sees every client's rows without disabling a policy. The application role never runs it. `pg_dump` and `pg_restore` get host, port, user and database as arguments and the password only as `PGPASSWORD` in the child's environment (`sslmode` as `PGSSLMODE`); never in argv. `SHIFDE_PG_BIN_DIR` points at the binaries when they are not on `PATH`; `pg_dump`'s major version must be at least the server's.

- `backup create --out DIR`: opens a repeatable-read transaction, exports its snapshot (`pg_export_snapshot()`), runs `pg_dump -Fc --no-owner --snapshot=<id>` (privileges kept), and in the same snapshot takes `count(*)` per table as the owner. Writes `manifest.json` as sorted-key JSON: app version, Alembic heads, `pg_dump` and server versions, created at, the dump's sha256, the key ids of `SHIFDE_SECRET_KEYS` (ids only), row counts per table, the excluded volatile tables (`workers`, `sessions`, `login_lockouts`, `oidc_auth_requests`: counted but not compared, since they change with any sign-in or worker start), and a **catalog fingerprint**: per table `relrowsecurity`, `relforcerowsecurity`, each policy's name, command, roles and expression, the grants to `shifde_app`, and each SECURITY DEFINER function's signature and `search_path`. The manifest carries its own digest. DIR must be new or empty; files are written owner-only.
- `backup verify --from DIR`: digests match, revision known to this code and not newer than its head, `pg_restore` present. No database needed.
- `backup restore --from DIR --target-url-env NAME`: the target URL comes from the variable named (or `NAME_FILE`). Refused unless verify passes; the target is **empty** (no `shifde` schema and no relation outside the system schemas; the `public` schema of `template0` exists and stays; no migration creates an extension, so restore creates none); it is owned by `shifde_owner`; roles `shifde_owner` and `shifde_app` exist on the target cluster (roles are cluster-level, never in the dump). Runs `pg_restore --single-transaction --exit-on-error --no-owner` as `shifde_owner`. Then checks, each reported: catalog fingerprint equal; row counts equal for every non-volatile table; `migration_state` ok; `shifde_app` passes the startup role check against the target; for every organization slug (listed as the owner) the existing `shifde.audit.service.verify_chain` passes; `secrets check` reports 0 failed. Prints `RESTORE VERIFIED` only after all pass; otherwise exit 1 naming what differs, and the target is left for inspection, never reported healthy (call 5).
- `secrets check` (new `shifde-admin secrets check`, function `check_all(session, keyring)` in `shifde.secrets.service` beside `rewrap_all`): as the owner, decrypts each stored secret envelope with the current key ring, discards the plaintext, prints `ok <n>, failed <m>` and the ids of failed secrets; exit 1 if any failed.

Tests (`backend/tests/cli/test_backup.py`, `test_restore.py`, local cluster): the scratch target is made with `_create_database(c, name, owner, app_role, "template0")` from `backend/tests/conftest.py` on the `admin_url` fixture (`SHIFDE_TEST_ADMIN_DATABASE_URL`) and dropped with `_drop_database`. Two seeded organizations with runs, artifacts, secrets and audit rows; backup, restore, all checks pass; as `shifde_app` on the restored database through the mounted app, A's user sees only A's rows and B's object answers 404; a tampered dump, a tampered manifest, a non-empty target, a target owned by another role, a missing `shifde_app` role and a too-new revision are each refused; a policy dropped on the target after restore fails the fingerprint check; captured subprocess argv holds no password; key ids appear and key material does not; `secrets check` with a wrong ring reports failures and no plaintext.

### 6.2 Process control (SP4-6, SP4-7)

| | `shifde-api` | `shifde-worker` | `shifde-executor` |
|---|---|---|---|
| Liveness | `/healthz` and `/api/health` (both exist, no database) | `GET /healthz` on `SHIFDE_WORKER_HEALTH_PORT` (default 0 = off), host `SHIFDE_WORKER_HEALTH_HOST` (default 127.0.0.1): 200 while the heartbeat loop ticked within 2 heartbeat intervals (`_heartbeat_loop`: `SHIFDE_WORKER_LEASE_SECONDS`/4, default 60/4 = 15 s) | `GET /healthz` on `SHIFDE_EXECUTOR_HEALTH_PORT` (default 0), host `SHIFDE_EXECUTOR_HEALTH_HOST`: 200 while the run loop ticked within 3 x the poll (`runner.py` `POLL_SECONDS` = 2.0) |
| Readiness | `/api/ready` (exists) | `GET /readyz`: database reachable and a claim tick within 3 x `SHIFDE_WORKER_POLL_MS` (default 1000) | `GET /readyz`: a successful gateway call within 3 x the poll |
| Stop | SIGTERM/SIGINT: `main.py` passes `SHIFDE_API_SHUTDOWN_GRACE_SECONDS` (default 30) to `uvicorn.run(timeout_graceful_shutdown=...)` | existing graceful stop; grace `SHIFDE_WORKER_SHUTDOWN_GRACE_SECONDS` (default 30) | SIGTERM/SIGINT/CTRL_BREAK call `Runner.stop()`: no new lease; within `SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS` (default 60) the in-flight step finishes. Exit 0 if drained; exit 1 if the deadline passes with a platform write in flight, left to sub-project 2's unknown-outcome path. SETH never cuts a write mid-call |
| Start checks | role check (3); schema mismatch gives readiness 503 | role check (3); `core.health.migration_state` must be `ok`, else exit 4 (ARCH m5) | config (2); `whoami` answering 401 or 403 gives exit 5; other gateway errors retry |

Exit codes for all three: 0 stopped on request; 1 crashed; 2 configuration refused; 3 unsafe database role; 4 schema revision mismatch; 5 cannot authenticate. A supervisor restarts on 1 with backoff and does **not** restart on 2-5.

Reference supervisor `scripts/procctl.py` (stdlib, not product code; `scripts/supervisor/` is the build supervisor and is not touched): `procctl.py up --env-file F [--with-executor] [--with-web]` starts the processes, waits for readiness, restarts on exit 1 with backoff 1, 2, 4 ... 60 s (reset after 60 s up), stops on 2-5 with the reason; `--with-web` runs `pnpm build` then `pnpm exec next start -p <SHIFDE_WEB_PORT> -H 127.0.0.1` (default 3710, the port of `pnpm start` and of the default `SHIFDE_ALLOWED_ORIGINS`), readiness `GET /`. On Windows children start with `CREATE_NEW_PROCESS_GROUP` and are stopped with `CTRL_BREAK_EVENT`; elsewhere SIGTERM. `down` signals and waits for the graces, then hard-kills (`proc.kill()`, TerminateProcess on Windows); `status` prints liveness and readiness. A host maps the same contract onto its own supervisor once known.

Tests: `backend/tests/runtime/test_worker_health.py` (endpoints, off by default, schema mismatch exit 4 via a scratch database at an older head), `backend/tests/executor/test_process_control.py` (SIGTERM during a fake-platform step finishes it and exits 0; deadline with a write in flight exits 1; exit 5 on 401), `backend/tests/app/test_api_shutdown.py` (grace passed to uvicorn), `scripts/tests/test_procctl.py` (in child processes on a scratch database: a hard-killed worker is restarted and readiness returns; an exit-2 child is not restarted; `down` exits 0 within the grace). `scripts/tests` runs in the scripts unittest gate.

## 7. Audit-finding register (major findings still open or narrowed)

Closed majors (C1, C2, Items 1, 3, 4, 5b, 5c, 6-16, 18, 19, 22, F-03, F-10, F-12, J-M2) need nothing new. Open follow-ups that must close before go-live (checked with `gh issue list`, all open, label `factory`): #33, #56, #60, #62 also labeled `known-defect`; #46, #47, #58 not. Item 3 is closed in code; SP4-2 adds its go-live proof. Dropped findings (Items 2, 17, 23 J-M1 and F-19) stay dropped. ARCH M5/F-18 was dropped "to be revisited at go-live": revisited, its security and limits settings are now `SHIFDE_*` settings refused at startup and in preflight (closed by SP4-1).

| Finding | State now | Plan (register state) |
|---|---|---|
| Item 5a (F-14) + ARCH m8: no security headers | open, no issue | **New:** SP4-2, SP4-3 (`closed` when merged) |
| Item 20 (ARCH M4; F-11): retention | narrowed: content purge only | **New:** SP4-8 deletes old ended runs; #46 closes the purge notes. Partitioning and never deleting audit events: **`pending-owner`** written acceptance (the audit hash chain is kept for life; `docs/build/perf.md` shows no need to partition at test scale) |
| Item 21 (ARCH M2): adapters inline in the API | narrowed | **Closed by #47** |
| Item 23 (F-23): traceability | narrowed: design 5.3 matrix | **`pending-owner`**: SETH's PRD ids belong to a document the unified design replaced; mapping them adds no assurance; the 5.3 matrix stays enforced by `traceability.py --check` |
| ARCH M11: `data:export` | narrowed | **Closed by #56** |
| REQ F-08: RLS tenant-only | open, B-2655 | **`pending-owner`** acceptance of B-2655's residual risk; #58 adds the run-time refusal checks first |
| REQ F-22: no real IdP sign-in | narrowed | `closed` once `tests/identity/test_entra_live.py -m live` passes with `.devdata/entra.env`; otherwise **`pending-owner`** acceptance to go live on local login with `SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION=true` |

`accepted` requires the owner's own words; the supervising session's delegated approval does not cover risk acceptance (memory "supervisor is owner approval": approvals, not risk; open question 1).

## 8. Calls made

1. **Hosting-neutral, local proof first.** The owner input is blank; charter 2's fallback says build what does not depend on it. Go-live is not declared until the host, hosted TLS and acceptances are recorded.
2. **Edge proxy routes `/api` straight to the API.** Next 16 cannot read the socket address (`proxy.ts` comment), so only a hop that can is trusted; `resolve_client_ip` is unchanged.
3. **CSP allows inline styles, not inline scripts.** Next and Tailwind emit inline styles; scripts use a nonce.
4. **Backup as the schema owner, no new role.** It owns the schema and has BYPASSRLS on both clusters; a third privileged role adds a credential, not separation.
5. **Restore verifies before it reports.** SETH's restore scripts reported falsely (ARCH M7); this one ends `RESTORE VERIFIED` only after the catalog, counts, role, audit and secrets checks.
6. **No encryption inside SETH for backups.** The dump holds client data as the database does; encryption at rest, off-site copies and backup retention are host storage settings, listed as host prerequisites in the runbook.
7. **Exit codes 2-5 are not restarted.** A restart loop on a refused configuration hides the refusal.
8. **Worker refuses a schema mismatch; the API reports it.** The worker writes run state; the API's readiness 503 already keeps traffic away.
9. **Audit events are never deleted by retention.** Deleting breaks the hash chain `audit verify` checks.
10. **The executor never interrupts a platform write.** An unknown outcome must stay unknown, never reported as a clean stop.

## 9. Open questions

Settled by the lead (B-2723..B-2725):

1. Only the owner records a written risk acceptance; the supervising session approves merges but not residual risk. Such rows stay `pending-owner` (B-2723).
2. For local go-live, logical `backup create` runs (nightly by the host's scheduler) are the baseline; point-in-time recovery is decided with the hosting target and recorded as waiting on owner input (B-2724).
3. An edge proxy is assumed. Without one, the degraded mode (one limiter key for web traffic) is a `pending-owner` register row, not silently accepted (B-2725).

## 10. Factory issues (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`; listed below are the issue-specific commands. No issue adds an API route or schema, so `backend/openapi.json` is expected unchanged; any issue that does change it refreshes it (`uv run python -m tests._support.openapi_snapshot`). **C** = isolation-critical (lead isolation review). Alembic migrations are factory-protected, so SP4-8 is merged by hand by the lead after the full gate set and `isolation-reviewer` (as B-2705).

1. **SP4-1 core/app/cli: production mode, register and `config preflight`.** Section 4 items 1-8. Checks: `cd backend; uv run pytest tests/core/test_settings_production.py tests/app/test_production_mode.py tests/cli/test_config_preflight.py tests/core tests/adapters tests/models_admin tests/executor`. Depends: none.
2. **SP4-2 app: API security headers and the edge-proxy proof.** Section 5 API part. Checks: `cd backend; uv run pytest tests/app/test_security_headers.py tests/app/test_edge_proxy.py tests/core tests/identity`. Depends: 1.
3. **SP4-3 front end: security headers, CSP nonce, `SHIFDE_WEB_API_PROXY`.** Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build; pnpm e2e`. Depends: 2.
4. **SP4-8 retention: delete old ended runs per client (C).** Runs first among the database issues so the backup tests cover the final schema. Eligible: the purge's own rule (`cli/maintenance.py` `_ELIGIBLE`: run and root ended, `finished_at` before the cutoff of `effective_days`), per client under its own context as `shifde_app`, skipping a client on legal hold (`shifde.retention_policies.legal_hold` true for any data class, as the purge does, B-2566 in STATUS), and skipping runs referenced by a `held` budget reservation or by a non-withdrawn release's passing evaluation that a delivery plan pins. Delete order: `tool_invocations`, `run_artifacts`, `run_events`, `approvals` (decided only), `run_steps`, `jobs` (terminal only), `ontology_query_lineage`, non-`held` `budget_reservations`, then `runs`; `usage_events` keep their `run_id` (no foreign key) and audit events are untouched. Migration `runtime_0005` (the next free number of the runtime branch today; the builder uses the next free one at build time) grants DELETE on those tables to `shifde_app` behind guard triggers that refuse any row whose run is not ended and past a cutoff set in a transaction-local setting. `shifde-admin maintenance delete --older-than <days> [--dry-run]`, one audit event per client. Cross-client test: B's rows byte-identical after A's delete. Checks: `cd backend; uv run pytest tests/cli/test_maintenance_delete.py tests/cli/test_maintenance_purge.py tests/runtime tests/ledger tests/schema`. Depends: none.
5. **SP4-4 cli: `backup create` and `backup verify` (C).** Section 6.1. Checks: `cd backend; uv run pytest tests/cli/test_backup.py tests/schema`. Depends: SP4-8.
6. **SP4-5 cli: `backup restore` and `secrets check` (C).** Section 6.1. Checks: `cd backend; uv run pytest tests/cli/test_backup.py tests/cli/test_restore.py tests/secrets tests/audit tests/schema`. Depends: SP4-4.
7. **SP4-6 worker and executor: health ports, signals, exit codes, schema check; API grace.** Section 6.2 table. Checks: `cd backend; uv run pytest tests/runtime/test_worker_health.py tests/runtime/test_worker.py tests/executor tests/app/test_api_shutdown.py`. Depends: none.
8. **SP4-7 scripts: `procctl.py` and `docs/runbooks/process-control.md`.** Checks: `python -m unittest discover -s scripts/tests`. Depends: SP4-6.
9. **SP4-9 go-live check and runbooks.** `docs/runbooks/backup-restore.md`, `docs/runbooks/go-live.md` (host prerequisites, edge proxy, env checklist). `scripts/golive_check.py --local` (cluster running, loopback, scratch databases made as the cluster superuser): `config preflight` passes on a production env file and refuses each bad variant; headers and docs 404 through an in-script edge proxy; a spoofed `X-Forwarded-For` ignored and the real address audited; `procctl.py up`, hard kill of the worker restarted, exit-2 child not restarted, `down` clean; on a scratch database `db upgrade` and `dev-seed` (keeping existing passwords in `.devdata/dev-users.txt`), `backup create`, `backup restore` into a second scratch database ending `RESTORE VERIFIED`; then `shifde-api` started with `SHIFDE_ENV=development`, `SHIFDE_DATABASE_URL` and `SHIFDE_MIGRATION_DATABASE_URL` pointing at the restored database and `SHIFDE_API_PORT=8792`, and `python -I scripts/two_org_proof.py --base-url http://127.0.0.1:8792 --origin http://localhost:3710 --users .devdata/dev-users.txt` must print `TWO-ORG PROOF PASSED`; the register has only `closed`, `accepted` or `pending-owner` rows, and the `pending-owner` ones are printed. Ends `GO-LIVE CHECK PASSED (local)`. `--final` also requires no `pending-owner` row and a register entry for the hosting target. Checks: `python scripts/golive_check.py --local; python -m unittest discover -s scripts/tests`. Depends: 1-8, #33, #46, #47, #56, #58, #60, #62.

## 11. Calls settled after builder read (lead, 2026-11-21)

1. Metrics: the request-time 403 exists (`observability/api.py` `_authorize_metrics`); new work is the startup refusal and a preflight line (4.3, 4.8).
2. Production refuses an explicit `SHIFDE_LOG_FORMAT=console` (4.4).
3. Missing `SHIFDE_PUBLIC_BASE_URL` in production is a new startup refusal; tests that build production settings set it (4.5).
4. Register format defined in SP4-1, read by preflight (4.7, 4.8).
5. Preflight uses `SHIFDE_DATABASE_URL`; unreachable gives `refused: database unreachable`, nonzero exit (4.8).
6. Fake adapter: `AdapterRegistry.load(include_fake)`, existing `ValidationFailed` 422 on create, `ModelUnavailable` 409 `model_unavailable` in `invoke_at`; test conditions in 4 (4.2).
7. Docs off in production; in development the docs and openapi routes are exempt from the strict CSP, no extra policy (5).
8. `Cache-Control: no-store` only when none is set (5).
9. Middleware order stated; 413 and 500 tested through the real stack (5).
10. `SHIFDE_HSTS_MAX_AGE_SECONDS` defined in SP4-1, consumed by SP4-2 (4.6, 5).
11. `SHIFDE_WEB_API_PROXY` per request, `on`|`off`, `off` for production, 404 `{"detail":"not found"}` (5).
12. Matcher regex, nonce via `x-nonce` and CSP request headers, e2e collects `securitypolicyviolation` events and console errors (5).
13. No `SHIFDE_WEB_HSTS_MAX_AGE_SECONDS`; edge or API sets HSTS (5).
14. Edge peer faked with `ASGITransport(client=...)`, no 127.0.0.2 (5).
15. `PGPASSWORD` in the child environment for `pg_dump` and `pg_restore`, never argv (6.1).
16. `count(*)` as the owner in the dump's exported snapshot, sorted-key JSON, volatile tables listed and not compared (6.1).
17. Empty target defined; `public` exists; no extensions are created (6.1).
18. Restore lists organization slugs as the owner and runs `audit.verify_chain` per organization (6.1).
19. `secrets check` in `shifde.secrets.service` `check_all`, counts only, never plaintext (6.1).
20. Scratch databases via `tests/conftest.py` `_create_database` / `_drop_database` on `admin_url` (6.1).
21. `main.py` passes the grace to uvicorn; both `/healthz` and `/api/health` exist (6.2).
22. Poll settings named: `SHIFDE_WORKER_POLL_MS` (1000), `SHIFDE_WORKER_LEASE_SECONDS` (60), executor `POLL_SECONDS` 2.0 (a constant, no setting) (6.2).
23. Worker reuses `core.health.migration_state`; executor exit 5 only on 401/403 (6.2).
24. and 25. Executor exits 0 if drained, 1 if the deadline passes with a write in flight (6.2).
26. `--with-web`: `pnpm build`, then `next start` on `SHIFDE_WEB_PORT`, readiness `GET /`; `CREATE_NEW_PROCESS_GROUP` and `CTRL_BREAK_EVENT` on Windows; backoff reset after 60 s up. Default port is 3710, not 3000: `frontend/package.json` `start` and the default `SHIFDE_ALLOWED_ORIGINS` use 3710 (6.2).
27. Hard kill by `proc.kill()` (TerminateProcess), tested in child processes on a scratch database (6.2).
28. SP4-8: legal hold as the purge defines it, tables, delete order, guards, `runtime_0005` as next free, hand merge with isolation review, ordered before SP4-4/5 (10.4).
29. Referenced issues checked with `gh`: all open and `factory`; `known-defect` on #33, #56, #60, #62 only (7).
30. `--local` allows and prints `pending-owner` rows, `--final` refuses them; API on the restored database and `two_org_proof.py` arguments stated (10.9).
31. No issue is expected to change `openapi.json`; `scripts/tests` run in the scripts unittest gate (6.2, 10).
