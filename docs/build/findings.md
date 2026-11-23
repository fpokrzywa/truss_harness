# SETH audit findings mapped to modules

Source: SETH at `3ba0e06` (`docs/audit/*`, `docs/planning/REMEDIATION_PLAN.md`). Inventory by scout, saved by the lead. "Item n" = P10 final report section 8 number. Paths are under `backend/src/shifde/` in SETH unless prefixed.

C2 is critical in P10 and REQ F-01; SEC calls the same defect Major-1 (recorded as critical). ARCH C1 = J C1. SEC Major-2 = ARCH M9.

Counts: critical 2, major 36, minor 30. Critical/major by sub-project-1 scope: C1/C2 3 (C1, C2, item 1); R2 8; R3 cost 3; known defect 19; dropped 5.

## Critical and major

| Id | Sev | Batch | Summary | Owner | Key files (SETH) | SP1 scope |
|---|---|---|---|---|---|---|
| C1 (ARCH C1; J C1) | critical | R1 | Worker crash during approved write, then cancel or deadline: run reported cancelled/timed_out with the write already delivered; no operator prompt | runtime | runtime/settle.py:54-61; runtime/lifecycle.py:441-453; runtime/service.py:690-701; runtime/steps.py:659-661; runtime/pipeline.py:1042-1048; migrations p05_0003 | in scope: C1/C2 |
| C2 (REQ F-01; SEC Major-1) | critical | R1 | Role with connection:manage+test but no secret:use repoints base_url and receives the bound secret; update does not re-check on host change | connections | connections/service.py:364, 398, 403 | in scope: C1/C2; fixed and pinned (B-1506, `tests/connections/test_host_change.py`) |
| Item 1 (J-M3) | major | R1 | Cancel and deadline close "outcome unknown" decisions; indeterminate approval cancelled/expired at once | runtime | runtime/service.py:678-707; runtime/settle.py:46; runtime/repository.py:729; runtime/steps.py:820 | in scope: C1/C2 |
| Item 2 (ARCH M6) | major | R2 | Profile validate prints secret under secret_refs; inline-secret scan misses pwd, accesstoken, URL query | installation | installation/profile.py | dropped: code not ported |
| Item 3 (ARCH M9; SEC Major-2) | major | R2 | Empty default TRUSTED_PROXIES: all peers 127.0.0.1; per-IP limit inert, audit actor_ip wrong | core, app, frontend proxy | core/ratelimit.py:47-58; core/deps.py:28-30; app.py:107-110; frontend/src/proxy.ts | in scope: R2 |
| Item 4 (ARCH M10) | major | R2 | No API-key pepper rotation (PEPPER_ID "p1" only); same pepper derives OIDC PKCE | identity | identity/apikeys.py:40,144; identity/oidc.py:94-95 | in scope: R2 |
| Item 5a (REQ F-14) | major | R2 | No security headers (HSTS, CSP, frame, content-type) | app, frontend | app.py; frontend/next.config.ts; frontend/src/proxy.ts | in scope: R2 |
| Item 5b (REQ F-15) | major | R2 | API-key failures and OIDC callback not rate limited; no global body cap | core, identity, app | core/ratelimit.py; identity/api.py:141; identity/oidc.py; app.py | in scope: R2 |
| Item 5c (REQ F-07) | major | R2 | Rejected credentials (revoked/garbage key) get 401 with no audit row | identity | identity/apikeys.py:147-151 | in scope: R2 |
| Item 6 (REQ F-20) | major | R2 | Pipeline test mode runs real writes | runtime, build | runtime/pipeline.py:68, 232; build/pipeline_nodes.py | in scope: R2 |
| Item 7 (REQ F-04) | major | R2 | Numeric keys above 2^53 coerced to float: wrong object and links | ontology | ontology/engine.py:124-131, 504, 510; ontology/service.py:460 | in scope: R2 |
| Item 8 (ARCH M8) | major | R2 | No startup check that the runtime DB role lacks BYPASSRLS/superuser | core | core/db.py; main.py | in scope: R2 (skeleton) |
| Item 9 (ARCH M1) | major | R3 | Hard budgets overshoot: max_tokens not sent; reservation below actual usage | runtime, adapters, ledger, models_admin | runtime/steps.py:228, 266-269; adapters/model_providers.py:295-296, 402; models_admin/service.py:427-455; ledger/budgeting.py; ledger/enforcement.py | in scope: R3 cost |
| Item 10 (REQ F-05) | major | R3 | limits.max_cost never enforced | runtime, build | runtime/agent.py:266-280; build/agents.py:281 | in scope: R3 cost |
| Item 11 (ARCH M12) | major | R3 | Omitted usage becomes 0 tokens labelled calculated; crash after provider answer leaves no ledger row | adapters, runtime, ledger | adapters/model_providers.py:303-307, 417-420; runtime/steps.py:130-141, 212-222 | in scope: R3 cost |
| Item 12 (REQ F-02) | major | R3 | Archived workspace/project still accepts runs and connections | runtime, connections | runtime/service.py:85, 194; connections/service.py:117 | known defect (runs); connections half fixed (B-1509, `tests/connections/test_regressions.py::test_archived_workspaces_and_projects_take_no_new_connection`) |
| Item 12 (REQ F-03) | major | R3 | Orgs cannot be suspended; archived org principals still log in | tenancy, identity | tenancy/schemas.py:12; identity/service.py | known defect; fix in review (factory job 001-f-03-suspend-a, issue #1, PR #29, B-2511, B-2512): `suspended` client status (`tenancy_0005`), platform-administrator suspend/unsuspend routes; a suspended or archived client is refused at the session pin, on its `{org}` routes, for its API keys, at run start, approval decisions and the worker's claim and advance (`runtime_0003`) |
| Item 13 (J-M2) | major | R3 | Admin cannot read run output in prod; capture_content has no supported setter | runtime, tenancy | runtime/service.py:539-544 | closed (issue #2, B-2550): `PUT /api/v1/orgs/{org}/capture-content`, audited; waiting for the `deliberate_changes.yaml` entry of migration `tenancy_0006`; settings key guard `tenancy_0007` (issue #39, B-2717), waiting for its `deliberate_changes.yaml` entry |
| Item 14 (REQ F-12) | major | R3 | Capture off only hides on read; content still stored | runtime, models_admin | runtime/steps.py:341, 954; runtime/repository.py:102 | fixed in code, waiting for the `deliberate_changes.yaml` entries of migration `runtime_0002` (B-2514) |
| Item 15 (REQ F-06) | major | R3 | Execution audit incomplete: no tool-invocation audit; model.invoked lacks run/workspace/project | runtime, models_admin, audit | runtime/steps.py:885; models_admin/service.py:143, 913 | closed (B-2540, `tests/runtime/test_tool_audit.py`) |
| Item 16 (REQ F-13) | major | R3 | No per-tool approval flag; audit_level unused; allowed_agent_ids skipped for pipelines | build, runtime | build/tools.py:36-48; build/kind_ops.py:305; runtime/steps.py:518; runtime/pipeline.py:355 | fixed in code (B-2570, `tests/runtime/test_tool_policy.py`, `tests/runtime/test_pipeline.py`, `tests/runtime/test_graph.py`, `tests/build/test_agents_tools.py`) |
| Item 16 (REQ F-09) | major | R3 | REST adapter lacks OAuth2 client credentials | adapters | adapters/rest_http.py:117 | closed: B-2580, `rest.http` `oauth2_client_credentials` (`tests/adapters/test_f09_oauth2_rest.py`) |
| Item 17 (ARCH M7) | major | R5 | Restore/rollback can leave services stopped and report falsely | install scripts | scripts/shifde-ctl.ps1 | dropped: code not ported |
| Item 18 (J-M4) | major | R5 | DB down: /api/ready takes 130 s to 503; no connect/statement timeout | core, observability | core/db.py:118-124; observability/api.py:44-55 | known defect |
| Item 19 (ARCH M3) | major | R5 | Wedged worker never recovered; no statement/lock/idle timeouts | runtime, core | runtime/worker.py:246-269; core/db.py | fixed in code (B-2590, `tests/runtime/test_worker.py`) |
| Item 20 (ARCH M4; REQ F-11) | major | R5 | No retention purge or maintenance command; partitions absent; run_artifacts undeletable | cli, audit, ledger, runtime | cli/main.py; runtime/repository.py | known defect |
| Item 21 (ARCH M2) | major | R5 | API runs adapters inline (connections, models, ontology) | app, connections, models_admin, ontology | connections/api.py:79-81, 104-108; models_admin/api.py:49, 115-123 | narrowed, not closed: the connection test runs on the worker (issue #10, B-2610), and so do the model provider health check and model discovery (issue #47, B-2720) connection discover (issue #144, B-2971) and connection invoke (issue #190); still inline: model invoke, ontology query/export, and the ontology mapping check's direct discover (ontology syncs do not exist; materialize is a 501 stub) |
| Item 22 (REQ F-17) | major | R5 | No CI, SAST, coverage gate, OpenAPI diff | repo infra | backend/pyproject.toml | closed (issue #11, B-2620..B-2624): bandit SAST, coverage floor and OpenAPI diff in CI |
| Item 22 (REQ F-21) | major | R5 | No repeatable browser E2E suite | frontend | frontend | closed (issue #12, B-2625..B-2630): Playwright suite `cd frontend; pnpm e2e` (sign in, organization selector, workspaces, demo-a cannot see demo-b) |
| Item 22 (REQ F-16) | major | R5 | API latency and harness overhead never measured | new harness | n/a | closed (issue #13, B-2633..B-2638): p95 budget test `backend/tests/perf/test_api_latency.py`, measurement `cd backend; $env:SHIFDE_PERF_ITERATIONS='200'; uv run pytest tests/perf/test_api_latency.py -q -s`, results in `docs/build/perf.md` |
| Item 23 (REQ F-19) | major | R5 | Install validator step fails on clean commit | install validator | scripts/validate/validate-install.ps1 | dropped: code not ported |
| Item 23 (J-M1) | major | R5 | Release bundle older than HEAD; dirty-tree builds allowed | release packaging | scripts/build-release.ps1 | dropped: code not ported |
| Item 23 (REQ F-23) | major | R5 | Traceability matrix unreliable | docs | docs/requirements/TRACEABILITY.md | narrowed, not closed (issue #14, B-2639..B-2644): design 5.3 matrix generated and checked by `scripts/traceability.py --check` (`scripts/tests/test_traceability.py`); SETH PRD ids remain open |
| ARCH M5; REQ F-18 | major | R3 decision | Half the install profile stored, not enforced | installation | installation/profile.py; installation/service.py | dropped: code not ported (security/limits settings to be revisited at go-live) |
| ARCH M11 | major | R3 decision | Classification × provider-trust egress control and data:export not implemented | runtime, ontology, models_admin, governance | runtime/gate.py; ontology/policy.py; governance/service.py:806, 892 | closed (both halves; issue #15, B-2645..B-2652, and issue #56, PR #119 merged `963a795`, B-2722, B-2731): classification x provider-trust egress control done (`tests/runtime/test_egress_classification.py`, `tests/ontology/test_classification.py`); `data:export` and the client-data export route done (issue #56, B-2722): `POST /orgs/{org}/ontologies/{ontology_id}/export` needs tenant-level `data:export`, honours `data_export` policies and is audited (`tests/ontology/test_export.py`) |
| REQ F-08 | major | none | RLS is tenant-only; PRD requires workspace and project | authz, tenancy | authz/pdp.py; tenancy/scope.py | open, risk recorded (B-2653..B-2656): workspace/project RLS not added; `backend/tests/authz/scoping/test_workspace_routes.py` classifies every mounted route and proves a user bound at one workspace is refused on another's resources on all 180 scoped routes; the residual risk awaits the owner's written acceptance |
| REQ F-10 | major | R3 decision | Approval rules skip pipeline runs; no data-export or target-env rule | runtime, governance | runtime/pipeline.py; governance/validation.py | closed (factory job 019, issue #17, B-2657): pipeline write nodes consult approval rules; `data_export` and `target_environments` conditions (`tests/runtime/test_f10_pipeline_approval_rules.py`); follow-up #60 (factory job 055, B-2728) closed isolation-review items A1-A3: environment labels match ignoring case and spaces and an unlabelled write matches, writes are classed by method (DELETE `destructive`), and every matched approver permission is required |
| REQ F-22 | major | R4 | No real IdP sign-in tested | identity | identity/oidc.py | narrowed, not closed: OIDC conformance suite against the local test IdP in `backend/tests/identity/test_oidc_conformance.py` (issue #18, B-2661..B-2663); live Entra sign-in remains open, waiting on owner input (sub-project 3, `.devdata/entra.env`) |

## Minor

| Id | Summary | Owner |
|---|---|---|
| ARCH m1 | repr(settings)/model_dump expose DB passwords | core |
| ARCH m2 | backup restore --target-url takes password in argv | cli |
| ARCH m3 | Import-linter contracts weaker than claimed | tooling |
| ARCH m4 | Reaper terminal transitions skip settle_terminal | runtime |
| ARCH m5 | Worker does not check schema revision at start | runtime |
| ARCH m6 | SECURITY DEFINER list drift vs DATA_MODEL | core |
| ARCH m7 | audit_events write_own allows tenant_id NULL inserts | audit |
| ARCH m8 | CSP, X-Frame-Options, HSTS absent (frontend) | app |
| ARCH m9 | /api/docs and /api/openapi.json unauthenticated in prod | app |
| ARCH m10 | Fake provider and test servers ship in all environments | adapters |
| ARCH m11 | Postgres leaks through neutral layers | build |
| ARCH m12 | Rate cards keyed adapter+model | ledger |
| ARCH m13 | Cached tokens never parsed | adapters |
| ARCH m14 | Deadlines use app clock | core |
| ARCH m15 | Per-tenant queue cap defaults 0; no round-robin | core, runtime |
| ARCH m16 | Worker has no health/metrics port | runtime |
| J m1 | Cancel cannot finish without a worker | runtime |
| J m2 | Platform status healthy up to 3 leases after worker kill | tenancy |
| J m3 | Healthy run shows "Attempts 3" | runtime |
| J m4 | 12 MiB body via web gives 500 not 413 | app |
| J m5 | API money totals are floats | governance/ledger |
| J m6 | Failed connection test sets degraded; manual recovery | connections |
| J m7 | shifde-ctl stop logs exit code as error | dropped: install tooling |
| REQ F-24 | Past-dated rate versions fill gaps | ledger |
| REQ F-25 | Weak tests (test_graceful_stop) | tests |
| REQ F-26 | Expand/contract migrations not practised | migrations |
| REQ F-27 | Bundle of smaller unbuilt clauses | multiple |
| SEC Minor-1 | Email-only account lockout | identity |
| SEC Minor-2 | /platform/status readable by any principal | tenancy |
| SEC Minor-3 | No delete path for secrets, groups, connections, service accounts, IdPs, roles, workspaces | several |

## Unsure owners (lead's resolution)

- Item 3: doctor/proxy defaults in install tooling are dropped; the rate-limit and audit-IP code in `core`/`app` is fixed (R2).
- REQ F-08: owned by `authz`; the PDP route enumeration test is in place (B-2653), workspace/project RLS was not added, and the residual risk (B-2655) awaits the owner's written acceptance.
- Retention (item 20): known defect owned by `cli`.
- J m5: `ledger`.
