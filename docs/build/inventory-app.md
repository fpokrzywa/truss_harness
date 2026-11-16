# Inventory: SETH `app`, `main`, `cli` for the unified port

- Source: SETH `3ba0e06` (read via `git show` / `ls-tree`, read-only).
- Unified: `S:/Development_2026/shi_work/agt_trus_harness`, branch `main` (read-only).
- Produced by the scout; no code changed; not committed.
- Line numbers: SETH file lines unless marked "carry_forward" (`docs/build/carry_forward.md`) or "unified".

## 1. SETH files in scope

| File (under `backend/src/shifde/`) | Lines | shifde.* imports (module level unless marked lazy) |
|---|---|---|
| `__init__.py` | 28 | none (reads `backend/pyproject.toml` for `__version__`) |
| `app.py` | 137 | `shifde` (`__version__`), `adapters.registry`, `adapters.runtime`, `admin.api`, `build.api`, `connections.api`, `core.db`, `core.deps`, `core.features`, `core.problem`, `core.ratelimit`, `core.settings`, `core.ssrf`, `governance.api`, `identity.api`, `identity.dependencies`, `identity.oidc`, `identity.passwords`, `installation.api`, `models_admin.api`, `observability.api`, `observability.tracing`, `observability.logging`, `observability.middleware`, `ontology.api`, `runtime.api`, `runtime.governance`, `tenancy.api`, `vendors.api` |
| `main.py` | 20 | `shifde.app` (`create_app`), `shifde.core.settings` (`settings_or_exit`) |
| `cli/__init__.py` | 0 | none |
| `cli/main.py` | 811 | `shifde` (`__version__`), `adapters.conformance`, `adapters.evidence`, `adapters.registry`, `audit.service`, `cli.backup`, `cli.demo.commands`, `cli.sampledata`, `cli.seeding`, `core.configtools`, `core.context`, `core.crypto`, `core.db`, `core.devenv`, `core.errors`, `core.health`, `core.migrations`, `core.settings`, `identity.service`, `identity.passwords`, `installation.profile`, `installation.service`, `secrets.service`, `tenancy.service`; lazy (inside command bodies): `adapters.testservers`, `cli.sample_pipeline`, `cli.sample_ontology`, `cli.governance_seed` |
| `cli/backup.py` | 335 | `shifde`, `core.db`, `core.migrations` |
| `cli/seeding.py` | 160 | `audit.service`, `authz.service`, `core.clock`, `core.context`, `core.ids`, `identity.service`, `identity.passwords`, `tenancy.models`, `tenancy.repository` |
| `cli/governance_seed.py` | 164 | `shifde` (`ledger`), `core.errors`, `core.ids`, `ledger`, `ledger.pricing` |
| `cli/profile_tenant.py` | 597 | `adapters.registry`, `adapters.runtime`, `admin.providers`, `admin.schemas`, `audit.service`, `authz.service`, `connections.schemas`, `connections.service`, `core.context`, `core.db`, `core.errors`, `core.settings`, `core.ssrf`, `installation.profile`, `models_admin.schemas`, `models_admin.service`, `tenancy.schemas`, `tenancy.scope`, `tenancy.service` |
| `cli/sample_pipeline.py` | 457 | `adapters.testservers`, `core.devenv`, `core.settings` |
| `cli/sample_ontology.py` | 421 | `cli.sample_pipeline`, `core.devenv`, `core.settings` |
| `cli/sampledata.py` | 218 | none |
| `cli/demo/__init__.py` | 0 | none |
| `cli/demo/commands.py` | 489 | `adapters.conformance`, `adapters.evidence`, `adapters.registry`, `adapters.testservers`, `cli` (`profile_tenant`), `cli.demo.config`, `cli.demo.corpus`, `cli.demo.golden`, `cli.demo.loader`, `cli.demo.source`, `core.context`, `core.db`, `core.devenv`, `core.migrations`, `core.settings`, `installation` (`profile`, `service`) |
| `cli/demo/api.py` | 146 | `cli.demo.config`, `core.settings` |
| `cli/demo/config.py` | 119 | `core.crypto`, `core.devenv`, `core.settings` |
| `cli/demo/corpus.py` | 422 | `cli.demo.config` |
| `cli/demo/golden.py` | 189 | `cli.demo.config`, `core.migrations`, `runtime.state` |
| `cli/demo/loader.py` | 657 | `adapters.testservers`, `cli.demo.api`, `cli.demo.config`, `cli.demo.corpus`, `cli.demo.source`, `core.db`, `core.settings`, `identity` (`passwords`, `service`), `runtime.worker` |
| `cli/demo/source.py` | 428 | `cli.demo.config`, `cli.sampledata` |
| `backend/pyproject.toml` | n/a | `[project.scripts]` lines 25-28 (see section 5) |

Totals: 20 Python files (3 in `shifde/`, 17 under `cli/`), 5,613 lines under `cli/`, 137 + 20 + 28 for the top-level three. Root `pyproject.toml` does not exist at `3ba0e06` (only `backend/pyproject.toml`).

## 2. `app.py`: routers, middleware, handlers, lifespan

### 2a. Mounted routers

| # | Router expression | SETH import (app.py line) | Prefix | Tags | Dependencies applied |
|---|---|---|---|---|---|
| 1 | `health_api.router` | `shifde.observability.api` (l.33) | `/api` | `health` | global only |
| 2 | `auth_api.router` | `shifde.identity.api` (l.27) | `/api/auth` | `auth` | global only (no `require_auth`) |
| 3 | `installation_api.public_router` | `shifde.installation.api` (l.31) | `/api/installation` | `installation` | global only (public; branding routes) |
| 4 | `tenancy_api.router` | `shifde.tenancy.api` (l.40) | `/api/v1` | none | `require_auth` |
| 5 | `installation_api.router` | `shifde.installation.api` (l.31) | `/api/v1/platform` | `platform` | `require_auth` |
| 6 | `admin_api.router` | `shifde.admin.api` (l.16) | `/api/v1` | none | `require_auth` |
| 7 | `vendors_api.router` | `shifde.vendors.api` (l.41) | `/api/v1` | none | `require_auth` |
| 8 | `connections_api.router` | `shifde.connections.api` (l.18) | `/api/v1` | none | `require_auth` |
| 9 | `models_api.router` | `shifde.models_admin.api` (l.32) | `/api/v1/orgs/{org}` | none | `require_auth` |
| 10 | `build_api.router` (includes `org_router` `/orgs/{org}`; kind routers tagged by kind) | `shifde.build.api` (l.17) | `/api/v1` | per kind (`pipelines`, etc.) | `require_auth` |
| 11 | `runtime_api.router` | `shifde.runtime.api` (l.38) | `/api/v1/orgs/{org}` | `operate` | `require_auth` |
| 12 | `ontology_api.router` | `shifde.ontology.api` (l.37) | `/api/v1/orgs/{org}` | `ontology` | `require_auth` |
| 13 | `governance_api.router` | `shifde.governance.api` (l.26) | `/api/v1` | none | `require_auth` |

Prefixes and tags are the values in each module's `router = APIRouter(...)` line (SETH). `build/api.py` line 33 builds kind routers `APIRouter(prefix=f"/{kind}", tags=[kind])`.

### 2b. Middleware, handlers, lifespan, app state (in order of execution)

| Step | SETH app.py line | Detail |
|---|---|---|
| `configure_logging(settings.log_level, settings.log_format)` | 54 | `shifde.observability.logging` |
| `tracing.configure_from_settings(settings, "shifde-api")` | 55 | `shifde.observability.tracing` |
| `Database(settings.database_url)` | 56 | `shifde.core.db` |
| lifespan: `yield`, then `db.dispose()` | 58-61 | only lifespan step |
| `FastAPI(title="SHIFDE API", version, openapi_url="/api/openapi.json", docs_url="/api/docs", redoc_url=None, lifespan, dependencies=[reject_basic_auth, check_origin], responses=PROBLEM_RESPONSES)` | 63-72 | global dependencies on every route |
| `app.state`: `settings`, `db`, `login_ip_limiter` (`login_rate_per_minute`), `login_account_limiter` (`login_rate_per_account_per_minute`), `passwords` (`PasswordService(fast=env=="test")`), `oidc` (`OidcClient` + `OutboundHttp`), `integration` (`IntegrationRuntime`, `AdapterRegistry.load()`), `governance` (`TokenBudgetHooks`), `features` (`FeatureRegistry`) | 73-114 | no `oidc_limiter`, no `api_key_failure_limiter` (see section 6) |
| `install_error_handlers(app)` | 98 | `shifde.core.problem` |
| `CORSMiddleware` (origins `settings.allowed_origins`; credentials; methods GET/POST/PATCH/PUT/DELETE/OPTIONS; headers Content-Type, X-CSRF-Token, X-Request-ID; expose X-Request-ID) | 99-106 | |
| `ProxyHeadersMiddleware(trusted_hosts=settings.trusted_proxies)` | 107-110 | only when `settings.trusted_proxies` is set |
| `RequestIdMiddleware` | 111 | outermost; comment says every response gets X-Request-ID |
| `features.register(...)`: `tenancy` (121), `security` (124), `integrate` (127), `build` (129), `operate` (131), `pipelines` (132), `ontology` (134), `governance` (136) | 121-136 | `operate` and `pipelines` have no router of their own |

No `MaxBodySizeMiddleware` in SETH `app.py`.

### 2c. `main.py` (`shifde-api`)

`run()` (l.11-20): `settings_or_exit()`, then `uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, proxy_headers=False, log_config=None, access_log=False)`.

## 3. Mounted routers present in the unified repo

Check: module file under `backend/src/shifde/` exists, and the router object name is defined there (grep for `router = APIRouter`).

| SETH import | Router | Unified file exists | Router name found | Unified prefix (line) | Status |
|---|---|---|---|---|---|
| `observability.api` | `router` | yes | yes (l.25) | `/api` (same) | present |
| `identity.api` | `router` | yes | yes (l.39) | `/api/auth` (same) | present |
| `installation.api` | `public_router` | yes | **no** | n/a | **missing**: unified `installation/api.py` defines only `router` (l.15, `/api/v1/platform`) |
| `tenancy.api` | `router` | yes | yes (l.35) | `/api/v1` (same) | present |
| `installation.api` | `router` | yes | yes (l.15) | `/api/v1/platform` (same) | present |
| `admin.api` | `router` | yes | yes (l.38) | `/api/v1` (same) | present |
| `vendors.api` | `router` | yes | yes (l.23) | `/api/v1` (same) | present |
| `connections.api` | `router` | yes | yes (l.19) | `/api/v1` (same) | present |
| `models_admin.api` | `router` | yes | yes (l.32) | `/api/v1` (**SETH: `/api/v1/orgs/{org}`**) | present, prefix differs |
| `build.api` | `router` (+ `org_router`) | yes | yes (l.21, l.22) | `/api/v1` (same) | present |
| `runtime.api` | `router` | **no** (`runtime/` has no `api.py`) | no | n/a | **runtime branch** |
| `ontology.api` | `router` | yes | yes (l.20) | `/api/v1/orgs/{org}` (same) | present |
| `governance.api` | `router` | yes | yes (l.27) | `/api/v1` (same) | present |

Result: 13 mounted routers in SETH; 11 present; 1 missing name (`installation.api.public_router`); 1 on the runtime branch (`runtime.api.router`).

Unified `app.py` is a skeleton (its docstring: "health only; module ports add their routers") and mounts none of the above. Its `main.py` is the SETH entry plus `refuse_unsafe_role` (exit 3, `shifde.core.dbrole`; `dbrole` existence not checked).

## 4. CLI commands

Root app: `shifde.cli.main:app` (typer). Sub-typers added at `cli/main.py` l.57-75. `demo_app` is defined in `cli/demo/commands.py` l.66.

Unified: `backend/src/shifde/cli/` contains only `__init__.py`. Every CLI module below is missing unless marked "yes".

| Command (group) | SETH location | Module(s) called | Module exists in unified | Flag |
|---|---|---|---|---|
| `version` | main.py:154 | `shifde.__version__` | yes | |
| `db upgrade` | main.py:101 | `core.migrations` | yes | |
| `db downgrade` | main.py:112 | `core.migrations` | yes | |
| `db current` | main.py:125 | `core.migrations` | yes | |
| `db check` | main.py:135 | `core.health`, `core.migrations` | yes | |
| `config schema` | main.py:165 | `core.configtools` | yes | |
| `config validate` | main.py:181 | `core.configtools` | yes | |
| `profile validate` | main.py:234 | `installation.profile`, `cli.profile_tenant` | profile.py yes (2-line placeholder); `cli.profile_tenant` no | **profile: drop (B-1101; carry_forward l.168)** |
| `profile diff` | main.py:305 | `installation.service.diff_profile`, `cli.profile_tenant` | service yes; profile functions not in unified | **profile: drop** |
| `profile apply` | main.py:311 | `installation.service.apply_profile`, `cli.profile_tenant.apply_tenant_sections` | service yes; profile functions not in unified (UNSURE) | **profile: drop** |
| `backup create` | main.py:320 | `cli.backup` | no | **backup: no DECISIONS entry found (UNSURE)** |
| `backup verify` | main.py:337 | `cli.backup` | no | **backup** |
| `backup restore` | main.py:355 | `cli.backup` | no | **backup** |
| `bootstrap` | main.py:383 | `identity.service` (`platform_admin_exists`, `find_user_id_by_email`, `create_user`, `create_setup_token`, `normalize_email`), `identity.passwords` | yes (function names not verified) | none (no profile/branding call) |
| `secrets rewrap` | main.py:436 | `secrets.service` | yes | needs migration URL (carry_forward l.80) |
| `audit verify` | main.py:453 | `audit.service.verify_chain` (UNSURE), `tenancy.service.tenant_id_by_slug` | yes (`tenant_id_by_slug` at `tenancy/service.py:744`) | |
| `dev-seed` | main.py:488 | `cli.seeding.seed_dev_data`, `cli.seeding.USERS`, `identity.passwords` | seeding no | **dev data** (carry_forward l.67-68 names cli for rate-card seed) |
| `adapters verify` | main.py:526 | `adapters.registry`, `adapters.conformance.run_suite`, `adapters.evidence.record_run` | yes | |
| `dev-sample-data` | main.py:578 | `cli.sampledata.build_sample_database`, `core.devenv` | sampledata no | **sample/demo data: no DECISIONS entry found (UNSURE)** |
| `dev-sample-pipeline` | main.py:626 | `cli.sample_pipeline.seed_sample_pipeline` | no | **sample data** |
| `dev-sample-ontology` | main.py:659 | `cli.sample_ontology.seed_sample_ontology`, `cli.sample_pipeline` | no | **sample data** |
| `dev-sample-rates` | main.py:695 | `cli.governance_seed.seed_sample_rates` | no | **sample data** |
| `usage record-build` | main.py:740 | `cli.governance_seed.record_build_op` | no | |
| `dev-rest-sink` | main.py:780 | `adapters.testservers.RestTestServer`, `cli.sample_pipeline.dev_rest_token` | testservers yes; sample_pipeline no | **sample data** |
| `demo load` | cli/demo/commands.py:318 | `cli.demo.*` (loader, corpus, source, golden, config, api), `installation`, `runtime.worker` | no | **demo data: no DECISIONS entry found (UNSURE)** |
| `demo reset` | commands.py:386 | `cli.demo.golden`, `cli.demo.source` | no | **demo data** |
| `demo wipe` | commands.py:401 | `cli.demo.config`, `cli.demo.golden` | no | **demo data** |
| `demo status` | commands.py:432 | `cli.demo.golden`, `cli.demo.config` | no | **demo data** |
| `demo env` | commands.py:456 | `cli.demo.config` | no | **demo data** |
| `demo sink` | commands.py:466 | `adapters.testservers.RestTestServer` | testservers yes; `cli.demo.*` no | **demo data** |

Totals: 30 commands (8 root + 22 in sub-typers: db 4, config 2, profile 3, backup 3, secrets 1, audit 1, adapters 1, usage 1, demo 6). Commands that touch profile, branding, backup/restore or demo/sample data: profile 3, backup 3, demo 6, sample/dev 6 (`dev-seed`, `dev-sample-data`, `dev-sample-pipeline`, `dev-sample-ontology`, `dev-sample-rates`, `dev-rest-sink`). Branding: no CLI command references branding (grep of `cli/` for "branding" returned nothing).

Decisions: `DECISIONS.md` B-101 (entry points for `shifde-admin`, `shifde-worker` pending), B-1101 (profile, branding, `profile` CLI not ported; design decision 15), B-1102..B-1106 (installation overview details). No DECISIONS entry was found for backup/restore or demo/sample data (grep for "backup", "demo", "dev-sample", "dev-seed" found none relevant). carry_forward l.39 and l.168 cover profile/branding CLI as not ported.

## 5. Deferred SETH tests owned by `app` (carry_forward)

Rule used: a test is listed under "Explicit" when the carry_forward text names `app` as the owner or move target for that test, or for its section. "Qualified" = `app` is one of several owners. Tests whose only owner is another module (build part 4b, runtime, ontology, admin, governance, cli-only) are excluded and listed in 5d.

### 5a. Explicit (144 named + 20 file-level = 164)

| carry_forward line | SETH file | Tests |
|---|---|---|
| 11 | `test_api_auth.py` (remainder) | `test_every_v1_route_denies_unauthenticated_requests`, `test_health_and_ready`, `test_ready_reports_503_when_database_is_down`, `test_request_id_is_echoed_or_generated`, `test_problem_json_shape_for_404_and_422`, `test_logs_are_structured_and_never_contain_secrets`, `test_openapi_and_docs_are_served` (7) |
| 12 | `test_p2_oidc.py` | `test_idp_groups_are_synced_on_login_and_removed_when_the_claim_shrinks`, `test_idp_group_grants_flow_through_to_permissions` (2; "moves with" lists app first, admin part 1 also) |
| 14 | `test_p2_review.py` | `test_suspension_revokes_unbound_and_same_tenant_sessions_but_not_other_tenant_pins`, `test_pinned_sessions_never_carry_platform_admin_and_promotion_revokes_sessions` (2; names were truncated with "..." in the text, resolved against SETH) |
| 20 | `test_p3_models.py` | `test_features_advertise_integrate_and_core_roles_hold_the_p3_permissions` (1) |
| 28 | `test_p8_observability.py` (first four only) | `test_trace_ids_propagate_from_the_api_request_through_the_job_to_the_worker_and_adapter`, `test_logs_are_correlated_and_never_carry_prompts_replies_or_secrets`, `test_metrics_need_the_bearer_token_or_a_loopback_client`, `test_metrics_expose_route_templates_queue_and_outcomes_and_leak_nothing` (4; "app and runtime") |
| 32 | `test_api_tenancy.py` (remainder) | `test_admin_creates_workspace_and_project_with_audit`, `test_validation_and_conflicts`, `test_archived_workspace_is_read_only_hidden_by_default_and_restorable`, `test_archived_project_and_org_semantics`, `test_members_listing`, `test_dashboard_counts_scope_and_activity`, `test_platform_admin_creates_org_and_lists_all` (7) |
| 36 | `test_p2_iam.py` (whole file; UNSURE whether all 20 still pending, see 6) | `test_the_viewer_test_table_covers_every_mutating_admin_route`, `test_viewer_cannot_use_any_p2_admin_mutation`, `test_viewer_cannot_read_admin_resources`, `test_p2_admin_routes_require_authentication`, `test_permission_catalog_lists_all_later_phase_keys_for_any_user`, `test_admin_adds_user_with_setup_token_suspends_and_reactivates`, `test_cannot_suspend_self_or_more_privileged_user`, `test_duplicate_email_is_a_conflict`, `test_group_membership_grants_exactly_the_role_permissions_and_removal_revokes`, `test_group_crud_conflicts_and_row_versions`, `test_custom_roles_templates_readonly_and_privileged_assignment`, `test_cannot_create_or_edit_role_granting_permissions_you_do_not_hold`, `test_cannot_bind_roles_you_do_not_hold_including_to_yourself`, `test_scoped_role_manager_cannot_bind_at_a_broader_scope`, `test_adding_group_members_cannot_launder_privileges`, `test_binding_expiry_and_hard_delete_are_audited`, `test_ancestor_visibility_for_project_grantee`, `test_me_permissions_reflect_effective_scope`, `test_session_advertises_exactly_the_implemented_features`, `test_feature_registry_is_per_instance_sorted_and_validates_keys` (20) |
| 37 | `test_p8_policies.py` (model policy only) | `test_a_model_policy_writes_through_to_the_p3_allowlist_and_blocks_other_models` (1) |
| 38 | `test_p2_secrets.py` | `test_resolve_is_pdp_checked_audited_and_logged`, `test_resolve_denied_for_principals_without_secret_use_and_for_disabled_or_foreign_secrets`, `test_ciphertext_moved_to_another_secret_row_does_not_decrypt`, `test_secrets_never_appear_in_responses_or_log_lines` (4) |
| 202 | `test_p4_prompts.py` (part 2a) | `test_create_prompt_with_typed_variables_and_composition`, `test_variable_validation_errors_carry_paths`, `test_prompt_test_runs_the_governed_model_evaluates_criteria_and_stores_the_run`, `test_prompt_test_needs_a_model`, `test_publish_diff_rollback_and_state_chain`, `test_metadata_patch_optimistic_locking_filters_and_key_uniqueness`, `test_untrusted_content_cannot_escape_its_block`, `test_template_language_has_no_logic_and_undeclared_variables_are_rejected`, `test_composition_rules` (9) |
| 202 | `test_p4_agents.py` (part 2b) | `test_skill_lifecycle_dependencies_and_cycles`, `test_agent_pins_components_and_is_reproducible`, `test_agent_assembly_test_renders_the_full_context`, `test_publish_rejects_models_outside_the_allowlist_and_unknown_models`, `test_agent_validation_rules`, `test_retired_and_restricted_components_are_not_referenced`, `test_agent_activation_and_permissions`, `test_cross_tenant_references_and_ids_are_not_visible` (8) |
| 202 | `test_p4_tools.py` (part 2b) | `test_executor_catalog_and_builtin_tool`, `test_tool_spec_validation_codes`, `test_rest_tool_runs_through_the_connection_and_guards_side_effects`, `test_rest_tool_timeout_and_retry`, `test_mcp_tool_snapshot_is_taken_at_publish`, `test_sql_tool_binds_parameters_and_stays_read_only`, `test_viewer_cannot_author_tools_and_only_publishers_publish` (7) |
| 202 | `test_p4_review1.py` (part 2b; 2 further tests moved out: `test_path_encoding_helpers`, `test_side_effects_are_never_retried_at_runtime`) | `test_publish_waits_for_a_concurrent_draft_writer_and_publishes_what_it_wrote`, `test_spec_hash_is_the_hash_of_the_stored_spec_and_approval_still_works`, `test_untrusted_variables_never_reach_the_system_message`, `test_run_input_cannot_feed_a_trusted_variable`, `test_pins_of_invisible_artifacts_are_redacted`, `test_connection_ids_are_no_oracle_and_approval_does_not_use_the_approvers_grants`, `test_approval_requires_an_active_artifact_and_private_requests_are_decidable`, `test_retry_idempotency_keys_and_fresh_keys_per_test_call` (8) |
| 202 | `test_p4_review2.py` ("still deferred to app") | `test_json_that_postgres_cannot_store_is_a_422_everywhere`, `test_nul_in_a_decision_comment_is_rejected`, `test_concurrent_publish_and_deprecate_keep_latest_pointing_at_a_published_version`, `test_an_approver_of_another_workspace_neither_sees_nor_decides_the_request`, `test_approval_revalidation_does_not_name_artifacts_the_approver_cannot_read`, `test_path_templates_reject_dot_segments_and_backslashes` (6) |
| 203 | `test_p6_build.py` | `test_node_type_registry_lists_only_real_nodes`, `test_unimplemented_node_types_are_declared_planned_and_cannot_be_used`, `test_valid_reference_pipeline_reports_topological_order`, `test_every_contract_error_code_is_reported_with_its_node_or_edge`, `test_references_must_be_published_visible_active_and_usable`, `test_publish_is_blocked_by_validation_errors_and_stores_pins_refs_and_report`, `test_pipeline_artifact_lifecycle_draft_diff_rollback_and_states`, `test_concurrent_publish_and_draft_saves_are_serialized_by_row_version`, `test_viewer_reads_but_cannot_author_or_execute`, `test_operator_may_execute_published_but_not_test_drafts`, `test_other_tenants_get_404_and_never_learn_anything`, `test_unstorable_json_is_a_422_never_a_500`, `test_run_params_with_unstorable_json_are_422`, `test_run_params_are_validated_against_the_spec_schema`, `test_schedules_are_not_enabled_in_the_mvp_and_work_when_the_flag_is_on` (15) |
| 203 | `test_p6_exec.py` | `test_reference_pipeline_runs_end_to_end_with_approval_before_the_side_effect`, `test_condition_false_skips_the_approval_and_the_side_effect`, `test_rejecting_the_approval_fails_the_run_and_nothing_is_sent`, `test_published_version_is_pinned_and_draft_runs_are_test_runs` (4) |
| 203 | `test_p6_exec2.py` | `test_every_enabled_node_type_executes_with_real_data_passing`, `test_fail_fast_cancels_the_open_sibling_and_fails_with_the_nodes_error_class`, `test_continue_runs_independent_branches_skips_dependants_then_fails`, `test_node_retry_repeats_a_read_but_a_side_effect_is_never_retried`, `test_write_node_failure_that_may_have_been_delivered_is_indeterminate_never_retried`, `test_killed_worker_resumes_from_completed_nodes_and_repeats_only_the_interrupted_read`, `test_killed_worker_around_a_side_effect_never_repeats_it_without_an_operator`, `test_revoked_permission_stops_the_pipeline_at_the_next_node_gate`, `test_pipeline_data_reaches_models_only_as_escaped_untrusted_user_content`, `test_cancel_idempotency_and_status_of_a_waiting_pipeline_run` (10) |
| 203 | `test_p6_exec3.py` | `test_db_query_arguments_are_bound_never_interpolated_and_datasets_are_identifiers`, `test_node_outputs_are_redacted_and_follow_the_capture_mode`, `test_publish_approval_applies_to_pipelines_and_stores_refs_on_approval`, `test_the_dev_sample_pipeline_spec_validates_and_publishes` (4) |
| 203 | `test_p6_exec4.py` | `test_a_write_tool_node_waits_for_the_tool_approval_and_runs_exactly_once`, `test_max_parallel_bounds_the_concurrent_child_runs` (2) |
| 203 | `test_p6_exec5.py` | `test_agent_nodes_that_fail_their_prechecks_do_not_stall_a_continue_pipeline`, `test_a_failed_attempt_with_retries_left_is_not_a_finished_node` (2) |
| 203 | `test_p6_hostile.py` | `test_hostile_specs_validate_to_an_error_report_not_a_500`, `test_oversized_params_and_bad_run_bodies_are_4xx` (2) |
| 203 | `test_p6_review1.py` | `test_f1_an_unguarded_edge_into_a_side_effect_node_does_not_validate`, `test_f1_a_side_effect_runs_only_when_every_upstream_approval_was_granted`, `test_f2_a_continue_failure_survives_later_advances`, `test_f3_a_child_finishing_before_the_parent_parks_does_not_strand_the_run`, `test_f4_an_unrelated_approval_node_does_not_waive_a_tool_nodes_own_approval`, `test_f5_a_revoked_pipeline_permission_stops_the_next_agent_node`, `test_f6_a_parked_test_run_keeps_its_snapshot_and_test_gate_when_the_draft_is_published`, `test_f7_rejecting_a_pipeline_approval_cancels_the_running_agent_children`, `test_f8_non_finite_numbers_from_a_database_do_not_crash_the_run`, `test_f8_sanitize_json_and_a_delivered_write_is_never_failed_for_its_size`, `test_f10_a_recovered_write_step_whose_gate_is_denied_is_indeterminate_not_failed`, `test_f11_huge_numbers_are_data_errors_not_internal_errors`, `test_f12_schedules_validate_params_and_the_service_account_privileges`, `test_f13_a_run_cannot_pair_a_pipeline_with_another_pipelines_version`, `test_f14_a_finished_child_wakes_a_pipeline_parent_parked_on_an_approval` (15) |
| 203 | `test_p6_review2.py` | `test_a_side_effect_without_model_output_upstream_still_runs_on_the_unguarded_branch`, `test_b_the_live_approved_branch_delivers_exactly_once`, `test_a_rejection_under_continue_ends_the_run_before_dependants_run`, `test_a_child_of_an_ended_pipeline_is_cancelled_when_it_is_claimed`, `test_a_child_finishing_before_the_parent_parks_on_an_approval_wakes_it`, `test_an_indeterminate_write_step_stays_the_record_when_the_node_gate_is_denied` (6) |
| 203 | `test_p6_review3.py` | `test_a_bypass_through_a_pass_through_node_does_not_validate`, `test_model_output_laundered_through_a_pass_through_node_is_not_delivered_unapproved`, `test_cancelling_an_orphaned_child_is_audited_and_journaled` (3) |
| 208 | `test_p5_graph.py` (3 of 6; other 3 are runtime) | `test_orchestration_validation_rules`, `test_orchestration_is_a_p4_versioned_artifact_with_pins_and_ui_positions`, `test_orchestration_permissions_follow_the_artifact_pattern` (3) |
| 208 | `test_p7_units.py` (1 of 5) | `test_dev_sample_ontology_seeds_a_working_idempotent_example` (1) |
| 208, 218 | `test_p7_build.py` (all 11; line 211 says "to `ontology`", see 6) | `test_create_define_map_validate_publish_and_version_history`, `test_mapping_check_compares_with_discovery_and_copies_no_rows`, `test_mapping_is_draft_only_needs_a_saved_target_and_permissions`, `test_publish_freezes_mappings_in_the_api_and_the_database`, `test_logical_model_validation_codes`, `test_mapping_validation_at_publish_codes`, `test_capability_and_connection_state_are_checked_at_publish`, `test_action_tool_validation`, `test_publish_approval_path_freezes_and_records_the_mappings`, `test_ontology_feature_permissions_and_isolation`, `test_other_tenant_cannot_use_a_published_version_in_a_pipeline_or_mapping` (11) |

Count check: 7+2+2+1+4+7+20+1+4 (=48) + 9+8+7+8+6 (=38) + 15+4+10+4+2+2+2+15+6+3 (=63) + 3+1+11 (=15) = 164.

### 5b. Qualified (`app` is one of several owners; 68 tests)

| carry_forward line | SETH file | Other owner(s) | Tests |
|---|---|---|---|
| 29 | `test_p2_audit.py` (10) | `cli` (`audit verify`) | `test_every_privileged_operation_is_audited_and_the_chain_verifies`, `test_audit_list_filters_and_cursor`, `test_tampering_with_an_event_is_detected`, `test_deleting_or_reordering_events_is_detected`, `test_hash_is_the_chain_of_prev_hash_and_canonical_event`, `test_concurrent_appends_keep_one_linear_chain`, `test_audit_details_are_scrubbed_and_installation_events_are_unchained`, `test_denials_and_privileged_allows_are_recorded`, `test_key_scope_denial_is_logged_with_its_reason`, `test_cli_audit_verify_reports_ok_then_broken` |
| 21 | `test_p3_connections.py` (15 of 16; `test_new_connections_and_providers_naming_a_non_active_vendor_are_refused` is owned by vendors/models, line 171) | `connections follow-up unit` | `test_rest_mcp_and_postgres_connections_with_secret_references`, `test_database_invoke_is_read_only_unless_read_write_and_connection_write`, `test_rest_writes_and_mcp_side_effects_need_write_permission`, `test_failed_tests_are_classified_and_never_leak_the_secret`, `test_config_must_match_the_adapter_schema_and_never_hold_credentials`, `test_secrets_are_attached_by_reference_same_tenant_and_with_secret_use`, `test_activation_needs_a_fresh_passing_test_and_re_enable_needs_another`, `test_failed_test_on_active_connection_degrades_it_and_a_pass_restores_it`, `test_planned_adapters_cannot_be_tested_discovered_or_invoked`, `test_unauthorized_use_is_prevented_and_tenants_are_isolated`, `test_workspace_scoped_connections_follow_workspace_grants`, `test_egress_policy_api_shapes_and_enforcement`, `test_platform_egress_default_is_platform_admin_only`, `test_open_for_use_enforces_pdp_status_and_audits`, `test_audit_records_cover_the_connection_lifecycle` |
| 21 | `test_p3_review.py` (connection tests, 6) | `connections follow-up unit` | `test_model_connections_cannot_be_invoked_as_raw_connections`, `test_open_for_use_refuses_model_connections`, `test_malformed_invoke_bodies_are_422_and_audited`, `test_database_and_mcp_bodies_are_validated`, `test_degraded_connections_are_denied`, `test_activation_requires_the_test_to_cover_the_current_definition` |
| 15 | `test_hardening.py` (7) | `identity` | `test_parallel_bad_logins_lock_the_account_exactly_at_threshold`, `test_nine_failures_do_not_lock`, `test_login_rate_limit_per_ip_returns_429`, `test_rate_limiter_window_slides`, `test_login_revokes_previous_session_of_a_different_user`, `test_platform_org_create_slug_race_returns_409_not_500`, `test_dashboard_member_count_is_null_without_member_read` |
| 15 | `test_hardening2.py` (8) | `identity` | `test_spoofed_xff_from_untrusted_peer_changes_neither_key_nor_recorded_ip`, `test_trusted_proxy_xff_is_honoured_for_ip_and_limiter`, `test_loopback_and_trusted_proxy_peers_skip_the_per_ip_limit`, `test_trusted_proxies_setting_parsing_and_validation`, `test_per_account_limit_trips_for_one_email_without_affecting_another`, `test_correct_password_is_rejected_while_locked_even_when_racing`, `test_race_around_threshold_never_admits_after_lock`, `test_revoked_session_cannot_be_unrevoked` |
| 218, 226, 227 | `test_p7_query.py` (15) | `runtime` (those that run actions or agents; split not enumerated, UNSURE) | `test_authorized_query_with_traversal_matches_the_source_data`, `test_reverse_traversal_and_numeric_date_types`, `test_compiled_sql_is_parameterized_and_filter_values_are_data`, `test_pagination_with_cursor_and_limits`, `test_traversal_depth_hop_and_row_caps`, `test_unauthorized_queries_are_rejected_and_audited`, `test_restricted_properties_need_an_explicit_allow`, `test_row_filters_use_principal_attributes_and_fail_closed`, `test_overlapping_policies_combine_per_property`, `test_policy_management_is_privileged_validated_and_audited`, `test_draft_unpublished_and_retired_versions_cannot_be_queried`, `test_tampered_frozen_mappings_are_refused`, `test_object_resolution_and_relationship_summaries`, `test_lineage_records_hashes_and_counts_not_values_and_no_data_is_copied`, `test_failed_source_queries_are_recorded_and_materialization_is_not_enabled` |
| 218, 226, 227 | `test_p7_review1.py` (7) | `runtime` (UNSURE split) | `test_action_run_must_stay_inside_the_ontologys_workspace`, `test_object_view_and_actions_work_for_types_with_many_relationships`, `test_agent_tool_cannot_read_an_ontology_the_principal_cannot_see`, `test_traversal_and_counts_work_for_numeric_decimal_keys`, `test_object_view_returns_the_properties_each_grant_admits_for_that_row`, `test_lineage_counts_only_the_links_the_principal_can_see`, `test_idempotent_action_replay_answers_before_the_object_is_resolved_again` |

Unnamed qualified item: line 226 "a route test of an object read with ids 9007199254740992 and 9007199254740993 against a live source" (app and/or runtime; no SETH test name given).

### 5c. Unnamed remainder items that point at app

- Line 149 and line 152: "remainder deferred to app, above" refers to the line 32 list (already counted).
- Line 9: `test_p8_audit.py` audit-route tests "admin or app" (carry_forward line 9); line 30 assigns `test_p8_audit.py` (4) to `governance`. Not counted.
- Line 15: `test_hardening*.py` "app/identity" (counted in 5b).

### 5d. Excluded (not owned by app, for reference)

- Line 202 / 211, build part 4b: `test_p4_governance.py` (7 tests: `test_publish_request_flow_with_separation_of_duties`, `test_approval_publishes_exactly_what_was_requested`, `test_visibility_private_scope_and_tenant`, `test_viewer_is_read_only_everywhere_in_the_build_area`, `test_every_create_publish_and_state_change_is_audited`, `test_rls_isolates_artifact_tables_and_the_app_role_is_column_restricted`, `test_database_enforces_one_open_draft_and_same_kind_versions`).
- Line 208: `test_p5_graph.py` other 3 (runtime); `test_p7_units.py` other 4 (ontology).
- Line 211: `test_p7_build.py` "to ontology" (conflicts with line 208/218, see 6).
- Line 9: `test_p8_scoping.py`, `test_p8_cost.py`, `test_p8_budgets.py`, `test_p8_review_r1.py`, `test_p8_review_r2.py`, `test_p8_policies.py` other 5 (governance, runtime).
- Line 14: other `test_p2_review.py` tests (identity part 3 / admin part 2).
- Line 21: `test_p3_connections.py::test_new_connections_and_providers_naming_a_non_active_vendor_are_refused` (line 171, vendors/models).
- Line 39: `test_p9_installation.py` (runtime).
- Line 28: `test_p8_observability.py` last three (runtime).
- Line 17: `test_p9_config.py` (owner cli). Line 18: `test_p3_adapters.py` CLI tests (owner cli / adapters). Neither is app.

### 5e. Totals

| Set | Count |
|---|---|
| Explicit `app` (named + file-level) | 164 (144 named; 20 `test_p2_iam.py` file-level) |
| Qualified (`app` plus another owner) | 68 |
| Unnamed items pointing at app | 2 (line 226 route test; line 9 audit routes, excluded) |

## 6. Carry-forward wiring rows owned by `app` (obligations, not tests)

| carry_forward line | Obligation (short) | Owner text |
|---|---|---|
| 45 | Add `MaxBodySizeMiddleware(max_bytes=settings.max_request_body_bytes)`; do not stack uvicorn ProxyHeaders on loopback | app |
| 46 | Pass `db_connect_timeout_seconds` / `db_statement_timeout_ms` to `Database` | app, runtime (worker) |
| 50 | Loopback trust for X-Forwarded-For | app |
| 51 | X-Request-ID validation or server-side generation | app |
| 56 | Mount `observability.api.router`, `RequestIdMiddleware`, `configure_logging`, `tracing.configure_from_settings` (B-501) | app |
| 81 | Redaction registry process-wide | core / app |
| 88 | Mount `identity.api.router` with `reject_basic_auth`/`check_origin`; `app.state.passwords`, `login_ip_limiter`, `login_account_limiter`, `api_key_failure_limiter`, `features` | app |
| 91 | `app.state.oidc` and `app.state.oidc_limiter` | app |
| 95 | Identity review 4 item 5 | identity / app |
| 96 | Identity review 3 item 5 remainder (`register_denial_audit`, client-scoped PDP) | app |
| 103 | Promotion path audit | whoever adds promotion (admin part 2, identity or app) |
| 104 | Tenancy review 3 items 2-4 | tenancy (follow-up, with app) |
| 106 | IdP group sync self-add audit before OIDC routes are mounted | identity, app |
| 167 | Mount `installation.api.router` (`/api/v1/platform/installation`); `app.state.features` | app |
| 170 | Mount `vendors.api.router`; `app.state.integration` | app |

## 7. Surprising or conflicting items

| # | Item | Where |
|---|---|---|
| 1 | Unified `app.py` mounts no routers; SETH `app.py` mounts 13 | unified `backend/src/shifde/app.py` vs SETH `app.py` |
| 2 | Unified `installation/api.py` has no `public_router` (SETH `/api/installation` branding routes); B-1101 says branding not ported, but SETH `app.py` l.118 still mounts it | unified `installation/api.py`; DECISIONS B-1101; carry_forward l.167 |
| 3 | Unified `runtime/` has no `api.py`, so a SETH-shaped `app.py` import of `shifde.runtime.api` fails (runtime branch) | unified `runtime/` |
| 4 | Unified `runtime/worker.py` has no top-level `def run`; `shifde-worker` target `shifde.runtime.worker:run` would not resolve even if re-added | unified `runtime/worker.py` |
| 5 | Unified `cli/` contains only `__init__.py`; `shifde-admin` target `shifde.cli.main:app` does not exist | unified `cli/` |
| 6 | `models_admin.api` prefix differs: SETH `/api/v1/orgs/{org}`, unified `/api/v1` | unified `models_admin/api.py` l.32 |
| 7 | Conflict: carry_forward l.211 says `test_p7_build.py` owner is `ontology`; l.208 and l.218 say `app` | carry_forward |
| 8 | Conflict: l.202 lists `test_p4_review1.py` under "deferred to app" heading, l.211 lists the same tests under build part 4b | carry_forward |
| 9 | `test_p2_iam.py` (20): l.36 names `app`, but the same row says each route's isolation is pinned in `tests/admin/` (UNSURE whether still pending) | carry_forward l.36 |
| 10 | SETH `app.py` has no `oidc_limiter` or `api_key_failure_limiter` in `app.state`, and no `MaxBodySizeMiddleware`, although carry_forward l.88, l.91 and l.45 cite them as the required app wiring | SETH `app.py` l.73-114 |
| 11 | Unified `main.py` adds `refuse_unsafe_role` (exit 3, imports `shifde.core.dbrole`, existence not checked) | unified `main.py` |
| 12 | No DECISIONS entry found for backup/restore or demo/sample data (B-1101..B-1106 cover profile and branding only) | DECISIONS.md |
| 13 | Root `pyproject.toml` absent at SETH `3ba0e06`; scripts are in `backend/pyproject.toml` only | SETH ls-tree |
| 14 | Unified `pyproject.toml` registers only `shifde-api`; `shifde-admin` and `shifde-worker` are commented out, citing DECISIONS B-101 | unified `backend/pyproject.toml` l.25-28 |

## 8. Unified `backend/pyproject.toml` `[project.scripts]` (current)

| Script | Target | Target module exists in unified | Registered |
|---|---|---|---|
| `shifde-api` | `shifde.main:run` | yes; `main.py` l.37 `def run()` | yes |
| `shifde-admin` | `shifde.cli.main:app` | **no** (`cli/` has only `__init__.py`) | no (commented out) |
| `shifde-worker` | `shifde.runtime.worker:run` | module yes; **no top-level `run`** | no (commented out) |

SETH `backend/pyproject.toml` (3ba0e06) `[project.scripts]` l.25-28: `shifde-api = "shifde.main:run"`, `shifde-admin = "shifde.cli.main:app"`, `shifde-worker = "shifde.runtime.worker:run"`.

## 9. Totals

| Measure | Value |
|---|---|
| SETH files in scope | 20 Python files + `backend/pyproject.toml` |
| SETH lines (app 137, main 20, `__init__` 28, cli 5,613) | 5,798 Python lines |
| SETH routers mounted | 13 (in 13 include_router calls) |
| Routers present in unified (file and name) | 11 (missing name: `installation.api.public_router`; runtime branch: `runtime.api.router`) |
| SETH CLI commands | 30 (8 root, 22 sub-typer) |
| CLI modules present in unified | `cli` package: 0 of 17 modules (only `__init__.py`). Commands whose called modules all exist in unified: 11 (version, db x4, config x2, bootstrap, secrets rewrap, audit verify (`audit.verify_chain` UNSURE), adapters verify). The other 19 call a missing `cli.*` module or a dropped profile/backup/demo path |
| Deferred SETH tests owned by app | 164 explicit + 68 qualified |
| Unified scripts registered | 1 of 3 |
