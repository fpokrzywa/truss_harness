# Carry-forward: obligations one port leaves for a later one

Each later module's brief must include its section here. Strike an item (move it to "Done") only when the module that owns it is merged with the item pinned by a test.

## Deferred SETH tests (move with the module named; assertions unchanged)

| SETH test (3ba0e06 `backend/tests/`) | What it covers | Moves with | Deferred by |
|---|---|---|---|
| `test_p6_units.py` (cron tests) | core.cron | build/runtime | core |
| `test_hardening.py`, `test_hardening2.py` (limiter, IP, proxy) | core.ratelimit, deps | app/identity | core |
| `test_p5_core.py` (safeexpr) | core.safeexpr | runtime | core |
| `test_p9_config.py` (settings, configtools) | core.settings, core.configtools | cli | core |
| `test_p3_adapters.py` (postgres suite on the sample database, 3 `shifde-admin adapters verify`/`dev-sample-data` CLI tests, sample database test, `user_identities` idp test) | adapters CLI verify, sample data, identity schema | cli (sample data, CLI); identity (`user_identities` test) | adapters |
| `test_p3_review.py` (all but the 6 adapters-only tests moved to `tests/adapters/test_p3_review_adapters.py`) | connections, models invoke, postgres caps on the sample database | connections / models_admin / cli (`sample_db_url`) | adapters |
| `test_p3_connections.py`, `test_p3_models.py` | connection and model-provider services and routes | connections, models_admin | adapters |
| `test_p4_review1.py::test_path_encoding_helpers`, `::test_side_effects_are_never_retried_at_runtime` | `build.tool_runtime` over the REST adapter | build | adapters |
| `test_r1_round3.py::test_a_tool_argument_with_a_space_still_reaches_the_target` | `build.tool_runtime.fill_path` + `join_url` | build | adapters |
| `test_r1_round4.py::test_a_malformed_base_url_is_a_config_error_not_a_crash` | `runtime.steps.classify_tool_failure` on an adapter config failure | runtime | adapters |
| `test_p7_units.py` (all but `test_postgres_adapter_compiles_filter_groups_and_offset_with_quoted_identifiers`) | ontology compiler | ontology | adapters |
| `test_p4_review2.py`, `test_p4_tools.py`, `test_p5_approvals.py`, `test_p5_reliability.py`, `test_p5_review.py`, `test_p6_*.py`, `test_p7_exec.py`, `test_p7_query.py`, `test_p8_cost.py`, `test_r1_critic.py` (all but 3 moved), `test_r1_host_change.py`, `test_r1_round2.py` / `test_r1_round3.py` / `test_r1_round4.py` (the rest), `test_r1_unknown_outcome.py` | use `shifde.adapters.testservers` or adapters through runtime/build/app fixtures | the module each one tests; the servers come from `tests/_support/servers.py` (rest_server, mcp_server, oai_server) | adapters |
| `test_p8_observability.py`, the 7 tests that need the app or the database (not moved): `test_trace_ids_propagate_from_the_api_request_through_the_job_to_the_worker_and_adapter`, `test_logs_are_correlated_and_never_carry_prompts_replies_or_secrets`, `test_metrics_need_the_bearer_token_or_a_loopback_client`, `test_metrics_expose_route_templates_queue_and_outcomes_and_leak_nothing` (these four need the app's `/api/metrics` mount, the worker and priced adapter stacks), `test_the_ops_stats_function_returns_only_aggregates` (needs governance's `shifde_ops_stats` migration), `test_performance_reports_percentiles_and_error_rates_by_agent_and_model` and `test_platform_health_details_are_for_platform_admins_only` (routes `/orgs/{org}/observability/performance` and `/platform/health-details` are in SETH `governance/api.py`) | app and runtime (first four); governance (last three) | observability |

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
| vendors, cli | Conformance `checks.detail` can carry a client DB's table names/SQL if `SHIFDE_SAMPLE_DATABASE_URL` points at one, and SETH `vendors/service.py:66-90` returns it to every client: refuse non-sample URLs in the CLI or drop `detail` from the client API | adapters review 1 |
| vendors, connections | `ExecContext.tenant_id` (`adapters/base.py:116`) must carry the organization | adapters review 1 |
| authz, admin | **Before any consumer of `effective_permissions`/`missing_permissions`/`visible_scopes` lands:** drop `PLATFORM_ONLY_ACTIONS` from grants in `pdp._load`, and add a DB check on `role_permissions` (`tenant_id IS NULL OR permission_key <> 'platform:admin'`), with tests | authz review 1 item 1 |
| identity, tenancy | Authz binding cross-client tests must match the trigger's specific messages (not bare `DBAPIError`) and exercise group/user/service-account/scope branches once `memberships` exists | authz review 1 item 2 |
| tenancy | Global PKs on groups/roles/role_bindings leak existence of another client's UUID via duplicate-key error (low risk) | authz review 1 item 4 |
| observability | Readiness uses `core.health.migration_state` (multi-head, B-111/B-203). The ported `api.py` already does; no test pins it until the app mounts the router | core |
| app | Mount `shifde.observability.api.router` (`/api/health`, `/api/ready`, `/api/metrics`), add `RequestIdMiddleware`, call `configure_logging` and `tracing.configure_from_settings` at startup (B-501). Not wired by the observability port | observability (B-501) |
| governance | Migration `shifde_ops_stats(int)` (SETH p08_0001 `OPS_STATS`, grant to `shifde_app`): observability's `/api/metrics` ops gauges call it and report "unavailable" until it exists (B-501) | observability (B-501) |
| runtime, ledger, models_admin | Item 9 (R3): send a `max_tokens` the budget covers on every model call (`runtime/steps.py`, `models_admin/service.py`); adapters now refuse a non-positive or non-integer `max_tokens` and send what they are given | adapters (Item 9) |
| runtime, ledger | Item 11 (R3): `adapters.base.Usage` fields are `int | None` (None = provider did not report, never 0) plus `cached_input_tokens` and `.reported`; the runtime must estimate unreported usage and label it estimated, not calculated, and the ledger must write the row even if the run crashes after the provider answered | adapters (Item 11, ARCH m13) |
| runtime (worker), cli | Entry points: `shifde-worker` and `shifde-admin` (with `adapters verify`) are re-added by their ports (B-101) | adapters |
| ports of SETH policies on B-112's list | Settle each rejected SETH policy (USING (true) tables → `global_tables.txt` with reason, or tenant-keyed) under isolation review | skeleton |

## Done

- observability: `test_core.py::test_log_redaction_masks_secrets` moved to `tests/observability/test_log_redaction.py` (passes). The readiness obligation stays open in the Wiring table until a test covers the mounted route.
