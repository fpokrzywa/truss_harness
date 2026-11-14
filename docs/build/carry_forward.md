# Carry-forward: obligations one port leaves for a later one

Each later module's brief must include its section here. Strike an item (move it to "Done") only when the module that owns it is merged with the item pinned by a test.

## Deferred SETH tests (move with the module named; assertions unchanged)

| SETH test (3ba0e06 `backend/tests/`) | What it covers | Moves with | Deferred by |
|---|---|---|---|
| `test_core.py::test_log_redaction_masks_secrets` | log redaction | observability | core |
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
| `test_p4_review2.py`, `test_p4_tools.py`, `test_p5_approvals.py`, `test_p5_reliability.py`, `test_p5_review.py`, `test_p6_*.py`, `test_p7_exec.py`, `test_p7_query.py`, `test_p8_cost.py`, `test_p8_observability.py`, `test_r1_critic.py` (all but 3 moved), `test_r1_host_change.py`, `test_r1_round2.py` / `test_r1_round3.py` / `test_r1_round4.py` (the rest), `test_r1_unknown_outcome.py` | use `shifde.adapters.testservers` or adapters through runtime/build/app fixtures | the module each one tests; the servers come from `tests/_support/servers.py` (rest_server, mcp_server, oai_server) | adapters |
| `test_pdp_and_cli.py` (8 CLI tests: dev-seed, bootstrap, db commands) | `shifde-admin` CLI | cli | authz |
| `test_p2_execution.py` (7) | `authz.execution_principal` (reads users, memberships, service_accounts, api_keys) incl. `test_a_run_naming_another_tenant_is_rejected` | identity (with tenancy for memberships) | authz |
| `test_p2_isolation.py` (9) | cross-tenant P2 surface via API and raw SQL (`b_data` built through admin routes), incl. the integrity-trigger and append-only tests | admin (with identity, tenancy, secrets) | authz |
| `test_rls.py` (all but `test_seeded_catalog_and_templates`, moved to `tests/authz/test_catalog.py`) | world-based RLS, grant matrix, definers, migration round trip | tenancy / identity / audit | authz |
| `test_p2_iam.py`, `test_p2_review.py::test_reenabling_a_role_user_or_service_account_requires_holding_its_permissions`, `::test_synced_groups_are_keyed_by_idp_and_sync_touches_only_that_idps_memberships` | role, group and binding admin routes; IdP group sync | admin / identity | authz |
| `test_api_tenancy.py` (scoped roles, cross-tenant 404) | PDP through tenancy routes | tenancy | authz |
| `test_p8_policies.py::test_the_decision_log_lists_denials_with_reason_codes_policy_ids_and_run` | `policy_decisions` through governance | governance | authz |

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
| observability | Readiness uses `core.health.migration_state` (multi-head, B-111/B-203) | core |
| runtime, ledger, models_admin | Item 9 (R3): send a `max_tokens` the budget covers on every model call (`runtime/steps.py`, `models_admin/service.py`); adapters now refuse a non-positive or non-integer `max_tokens` and send what they are given | adapters (Item 9) |
| runtime, ledger | Item 11 (R3): `adapters.base.Usage` fields are `int | None` (None = provider did not report, never 0) plus `cached_input_tokens` and `.reported`; the runtime must estimate unreported usage and label it estimated, not calculated, and the ledger must write the row even if the run crashes after the provider answered | adapters (Item 11, ARCH m13) |
| runtime (worker), cli | Entry points: `shifde-worker` and `shifde-admin` (with `adapters verify`) are re-added by their ports (B-101) | adapters |
| ports of SETH policies on B-112's list | Settle each rejected SETH policy (USING (true) tables → `global_tables.txt` with reason, or tenant-keyed) under isolation review | skeleton |
| tenancy | Add `tenant_id` -> `tenants (id)` FKs on `roles`, `role_permissions`, `role_bindings`, `groups`, `policy_decisions` (SETH names `<table>_tenant_id_fkey`) and remove their `deliberate_changes.yaml` entries | authz (B-402) |
| identity | Add `groups.idp_config_id` -> `idp_configs (id)` (`groups_idp_config_id_fkey`) and `group_members (tenant_id, user_id)` -> `memberships (tenant_id, user_id)` (`fk_group_members__membership`); remove their `deliberate_changes.yaml` entries | authz (B-402) |
| identity, tenancy | `check_role_binding()` works only once `memberships`, `service_accounts`, `workspaces`, `projects` exist (it fails closed until then): add tests that the app binds users, groups and service accounts at org/workspace/project scope, and that another client's user, group, service account, workspace or project is refused (SETH `test_p2_isolation.py::test_database_integrity_triggers_reject_cross_tenant_references_even_for_the_owner`); move `tests/authz` fixtures from `owner_insert_bindings` (superuser, replica mode) to the real seeded world; test `PDP.ancestor_workspace_ids` with project grants (reads `projects`) | authz (B-402, B-405) |
| identity (Task 9) | SHI-wide groups part 1: make `groups`/`group_members` platform-level (identity migration altering authz tables, `depends_on` authz; deliberate changes with reasons; RLS settled under isolation review: no `tenant_id`, so `global_tables.txt` or a reviewed `PRINCIPAL_TABLES` entry); supply platform membership through `authz.GroupMembership` (replace the default `tenant_group_membership` / `repository.active_group_ids`); change `check_role_binding()`'s group branch (group exists at platform level, not in the tenant) and the tenant-keyed group repository functions; keep `tests/authz/test_groups.py` passing | authz (B-403) |
| tenancy (Task 10) | Group -> role bindings at org/workspace/project: only a platform administrator or the client's owner may bind; platform-admin self-add audited and shown to the owner; organization visibility from bindings, including group-held ones (SETH `shifde_my_memberships()` reads direct user bindings only); with no binding the organization is invisible; the platform-admin flag grants no client permission (pinned in `tests/authz/test_cross_client.py::test_platform_admin_flag_gives_no_client_permission`) | authz (B-403) |
| ontology | `authz.subject_ids_covering(..., group_membership=)` takes the same seam as `new_pdp`; pass identity's membership once groups are platform-level | authz (B-403) |
| every port | The permission catalog and system role grants are complete in `authz_0001` (SETH final, B-402): do not re-insert SETH's p04-p08 permission rows | authz |
| every port | `Database` has psycopg auto-prepare off (B-404); an engine built outside `Database` that also runs `DISCARD ALL` needs the same | authz (core fix) |

## Done

(none yet)
