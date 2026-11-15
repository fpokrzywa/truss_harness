# Inventory: SETH `runtime` and `ontology` modules (for unified-platform port)

- Source: SETH `S:/Development_2026/shi_work/shi_enterprise_truss_harness`, commit `3ba0e06` (read via `git show`, `git grep`, `git ls-tree` only; nothing changed, nothing committed).
- Paths below are relative to `backend/` unless noted. Line counts are `wc -l` of the file at `3ba0e06`.
- `shifde` in an import column means `from shifde import X` (top-level package import).
- Totals: runtime 21 files, 8,806 lines; ontology 7 files, 2,103 lines.

---

## 1. Files and `shifde.*` imports

### 1a. `src/shifde/runtime/`

| File | Lines | `shifde.*` modules imported (external to this package unless marked `[rt]`) |
|---|---:|---|
| `__init__.py` | 1 | none (docstring only) |
| `state.py` | 99 | none |
| `schemas.py` | 257 | core.bodies |
| `repository.py` | 1091 | shifde (ledger), core.ids, observability |
| `governance.py` | 338 | core.clock, core.ids, ledger, ledger.budgeting, ledger.enforcement |
| `pipeline_ops.py` | 218 | core |
| `context.py` | 224 | adapters.runtime, audit, authz, authz.service, build.service, core.clock, core.context, core.settings, tenancy, tenancy.scope, runtime [rt], runtime.governance [rt], runtime.repository [rt], runtime.state [rt] |
| `settle.py` | 117 | audit, core.clock, core.context, runtime [rt], runtime.repository [rt], runtime.state [rt] |
| `gate.py` | 221 | authz, authz.service, core.clock, core.context, core.ids, runtime [rt], runtime.context [rt], runtime.governance [rt], runtime.repository [rt], runtime.state [rt] |
| `ontology_exec.py` | 141 | authz.service, build.executors, core.errors, ontology, ontology.service, runtime.context [rt], runtime.gate [rt] |
| `policies.py` | 168 | shifde (ledger), runtime.context [rt], runtime.gate [rt], runtime.governance [rt], runtime.steps [rt] (TYPE_CHECKING only) |
| `schedules.py` | 191 | adapters.runtime, admin.iam, audit, build.issues, build.service, core.clock, core.cron, core.errors, core.ids, tenancy.scope, runtime.schemas [rt] |
| `steps.py` | 1172 | shifde (ledger), adapters.base, authz.service, build, build.env, build.issues, core.clock, core.errors, core.ids, core.redaction, ledger, models_admin, observability, runtime.context [rt], runtime.gate [rt], runtime.governance [rt], runtime.ontology_exec [rt], runtime.policies [rt], runtime.repository [rt], runtime.state [rt] |
| `agent.py` | 281 | authz.service, build.env, build.kind_ops, core.errors, runtime.context [rt], runtime.gate [rt], runtime.ontology_exec [rt], runtime.repository [rt], runtime.state [rt], runtime.steps [rt] |
| `action.py` | 266 | authz.service, build, build.executors, build.issues, core.clock, core.errors, core.ids, observability, ontology.service, tenancy, runtime.context [rt], runtime.gate [rt], runtime.governance [rt], runtime.repository [rt], runtime.schemas [rt], runtime.state [rt], runtime.steps [rt] |
| `graph.py` | 527 | authz.service, build.env, core, core.clock, core.ids, core.redaction, runtime.context [rt], runtime.gate [rt], runtime.repository [rt], runtime.state [rt], runtime.steps [rt] |
| `pipeline.py` | 1447 | adapters.base, authz.service, build, build.env, build.issues, build.pipeline_nodes, build.prompts, core, core.clock, core.errors, core.ids, core.redaction, runtime.context [rt], runtime.gate [rt], runtime.ontology_exec [rt], runtime.pipeline_ops [rt], runtime.repository [rt], runtime.settle [rt], runtime.state [rt], runtime.steps [rt] |
| `lifecycle.py` | 468 | adapters.runtime, audit, authz, authz.service, core, core.clock, core.context, core.db, core.hooks, core.ids, core.redaction, core.settings, observability, runtime.action [rt], runtime.agent [rt], runtime.context [rt], runtime.governance [rt], runtime.graph [rt], runtime.pipeline [rt], runtime.repository [rt], runtime.settle [rt], runtime.state [rt] |
| `service.py` | 1023 | adapters.runtime, audit, authz, authz.service, build.issues, build.kind_ops, build.service, core.clock, core.errors, core.ids, core.pagination, core.redaction, observability, tenancy, tenancy.scope, runtime.governance [rt], runtime.repository [rt], runtime.schemas [rt], runtime.settle [rt], runtime.state [rt] |
| `api.py` | 180 | adapters.deps, core.errors, core.pagination, tenancy.dependencies, runtime.action [rt], runtime.governance [rt], runtime.schedules [rt], runtime.schemas [rt], runtime.service [rt] |
| `worker.py` | 376 | shifde (__version__), adapters.registry, adapters.runtime, audit, core, core.context, core.db, core.egress, core.settings, observability, observability.logging, runtime.governance [rt], runtime.lifecycle [rt], runtime.repository [rt] |



### 1b. `src/shifde/ontology/`

| File | Lines | `shifde.*` modules imported (external unless marked `[ont]`) |
|---|---:|---|
| `__init__.py` | 0 | none |
| `schemas.py` | 124 | core.bodies |
| `policy.py` | 144 | adapters.base, build |
| `repository.py` | 239 | core.sqlutil |
| `engine.py` | 574 | adapters.base, build, connections.service, core.errors, ontology [ont] (`policy`, `schemas`) |
| `service.py` | 921 | shifde (ledger), adapters.base, authz, build, build.service, connections.service, core.errors, core.ids, core.pagination, identity, ontology [ont] (`engine`, `repository`, `policy`, `schemas`) |
| `api.py` | 101 | adapters.deps, build.service, core.pagination, tenancy.dependencies, ontology [ont] (`schemas`), ontology.service [ont] |

Ontology has no import of `shifde.runtime`. Runtime imports ontology (`ontology.service` from `action.py` and `ontology_exec.py`; `ontology` from `ontology_exec.py`), so ontology parts must land first.

---

## 2. Intra-module import order and proposed parts

### 2a. Runtime: which file imports which (own files only)

| File | Imports these runtime files |
|---|---|
| `state.py`, `schemas.py`, `repository.py`, `governance.py`, `pipeline_ops.py`, `__init__.py` | none |
| `settle.py` | repository, state |
| `context.py` | repository, state, governance |
| `gate.py` | repository, state, context, governance |
| `policies.py` | gate, context, governance; steps (TYPE_CHECKING only, no runtime cycle) |
| `schedules.py` | schemas |
| `ontology_exec.py` | gate, context |
| `steps.py` | gate, ontology_exec, policies, state, repository, context, governance |
| `agent.py` | gate, ontology_exec, state, steps, repository, context |
| `action.py` | gate, state, steps, repository, context, governance, schemas |
| `graph.py` | gate, state, steps, repository, context |
| `pipeline.py` | gate, ontology_exec, state, steps, repository, context, pipeline_ops, settle |
| `lifecycle.py` | repository, state, action, agent, context, governance, graph, pipeline, settle |
| `service.py` | repository, schemas, state, governance, settle |
| `api.py` | schemas, action, governance, schedules, service |
| `worker.py` | lifecycle, repository, governance |

No cycles among runtime files (the `policies` -> `steps` edge is TYPE_CHECKING only).

### 2b. Runtime proposed parts (order is mandatory; files within a part are listed in import order)

Ontology parts ONT-1 and ONT-2 (section 2d) come before RT-1 because `ontology_exec.py` and `action.py` import `ontology.service`.

| Part | Files (in order) | Lines | Needs earlier runtime parts | Notes |
|---|---|---:|---|---|
| RT-1 | `__init__.py`, `state.py`, `governance.py` | 438 | none | |
| RT-2 | `schemas.py`, `pipeline_ops.py` | 475 | none | |
| RT-3 | `repository.py` | 1091 | none | OVERSIZE, single file |
| RT-4 | `context.py`, `settle.py`, `gate.py` | 562 | RT-1, RT-3 | |
| RT-5 | `ontology_exec.py`, `policies.py`, `schedules.py` | 500 | RT-1, RT-2, RT-4; ONT-2 | |
| RT-6 | `steps.py` | 1172 | RT-1, RT-3, RT-4, RT-5 | OVERSIZE, single file |
| RT-7 | `agent.py`, `action.py` | 547 | RT-2, RT-4, RT-5, RT-6; ONT-2 | |
| RT-8 | `graph.py` | 527 | RT-4, RT-6 | |
| RT-9 | `pipeline.py` | 1447 | RT-2, RT-3, RT-4, RT-5, RT-6 | OVERSIZE, single file |
| RT-10 | `lifecycle.py` | 468 | RT-3, RT-4, RT-7, RT-8, RT-9 | |
| RT-11 | `service.py` | 1023 | RT-1, RT-2, RT-3, RT-4 | OVERSIZE, single file |
| RT-12 | `api.py`, `worker.py` | 556 | RT-1, RT-5, RT-10, RT-11 | `api.py` needs RT-11; `worker.py` needs RT-10 |

Total 8,806 lines. Four parts exceed 600 lines (RT-3, RT-6, RT-9, RT-11) because each is one file of 1023 to 1447 lines; a file-level split cannot meet 400-600. Splitting those files by function would be a separate decision (UNSURE whether allowed).

External `shifde.*` dependencies outside both modules that the parts need first: `ledger`, `authz`, `audit`, `observability`, `tenancy`, `build` (incl. `build.executors`, `build.issues`, `build.env`, `build.service`, `build.kind_ops`, `build.pipeline_nodes`, `build.prompts`), `adapters` (base, deps, runtime, registry), `core` (all submodules), `admin.iam` (schedules.py), `models_admin` (steps.py), `identity` (ontology only), `connections.service` (ontology only).

### 2c. Ontology: which file imports which (own files only)

| File | Imports these ontology files |
|---|---|
| `__init__.py`, `schemas.py`, `policy.py`, `repository.py` | none |
| `engine.py` | policy, schemas |
| `service.py` | engine, repository, policy, schemas |
| `api.py` | service, schemas |

### 2d. Ontology proposed parts

| Part | Files (in order) | Lines | Needs earlier parts |
|---|---|---:|---|
| ONT-1 | `__init__.py`, `schemas.py`, `policy.py`, `repository.py`, `engine.py` | 1081 | none (external build, adapters, connections, core) |
| ONT-2 | `service.py`, `api.py` | 1022 | ONT-1 |

Total 2,103 lines. Both parts are inside the 1000-1400 band.

---

## 3. Migrations (`backend/migrations/versions/`) for runtime and ontology

Alembic chain at 3ba0e06, last file `p11_0001_unknown_write_outcome` (single-head status UNSURE), in order: p01_0001..p01_0003, p02_0001..p02_0004, p03_0001..p03_0003, p04_0001..p04_0002, p05_0001..p05_0003, p06_0001..p06_0003, p07_0001..p07_0002, p08_0001..p08_0003, p09_0001, p11_0001. (There is no p10 file.)

### 3a. Tables created or altered by runtime and ontology migrations

| Migration | Module | Tables created | Altered (runtime or ontology relevant) | Foreign keys to other modules' tables |
|---|---|---|---|---|
| `p05_0001_runtime.py` | runtime | `run_state_transitions`, `workers`, `runs`, `run_events`, `run_artifacts`, `run_steps`, `tool_invocations`, `approvals`, `jobs`, `usage_events`, `budgets`, `budget_reservations`; functions `shifde_claim_jobs` (SD), `shifde_reap_expired_leases` (SD), `runs_guard`, `approvals_guard` | `artifacts`, `artifact_versions`, `artifact_version_pins` (kind/role CHECKs widened) | `runs` -> `workspaces`, `projects` (tenancy, p01), `artifact_versions` (build, p04; agent, orchestration version); `tool_invocations` -> `artifact_versions`; all -> `tenants` (p01) |
| `p05_0002_runtime_permissions.py` | runtime | none | grants only | none |
| `p05_0003_run_lease_job.py` | runtime | none | `runs` + `lease_job_id`; `shifde_reap_expired_leases` redefined (SD) | none |
| `p06_0001_pipelines.py` | runtime (pipelines; owner UNSURE) | `pipeline_version_refs`, `pipeline_validation_reports`, `pipeline_schedules` | `runs` + `pipeline_id`, `pipeline_version_id`, `ck_runs__target`, `runs_kind_check`, index `ix_runs__pipeline`; `run_steps_kind_check`; `approvals_reason_check`; `usage_events` + `pipeline_version_id`, `node_id`, `usage_events_event_type_check`; `artifacts`/`artifact_versions` kind CHECK | `runs` -> `artifact_versions`; `pipeline_version_refs` -> `artifact_versions`, `connections` (p03), `models` (p03); `pipeline_schedules` -> `artifacts`, `artifact_versions`, `service_accounts` (p02) |
| `p06_0002_pipeline_permissions.py` | runtime | none | grants (UNSURE which) | none |
| `p06_0003_run_pipeline_integrity.py` | runtime | none | `artifact_versions` UNIQUE `(tenant_id, id, artifact)`; `runs` FK `fk_runs__pipeline_of_version` | `runs` -> `artifact_versions` |
| `p07_0001_ontology.py` | ontology | `ontology_source_mappings`, `ontology_version_info`, `ontology_access_policies`, `ontology_principal_attributes`, `ontology_query_lineage`, `ontology_materializations`; function `ontology_mappings_guard` | `runs` + `ontology_id`, `ontology_version_id`, `action_key`, FK `fk_runs__ontology_of_version`, index `ix_runs__ontology`; `pipeline_version_refs` CHECK `ck_pipeline_refs__typed`; `artifacts`/`artifact_versions`/`artifact_version_pins` CHECKs | `ontology_source_mappings` -> `artifact_versions`, `connections` (p03); `ontology_version_info` -> `artifact_versions`; `ontology_access_policies` -> `artifacts`; `ontology_query_lineage` -> `artifact_versions`, `runs` (runtime); `ontology_materializations` -> `artifact_versions`; all -> `tenants` |
| `p07_0002_ontology_permissions.py` | ontology | none | grants | none |
| `p08_0001_governance.py` | governance/ledger (owner UNSURE; runtime touches) | `rate_cards`, `rate_card_versions`, `usage_rollups_daily`, `budget_alerts`, `quotas`, `governance_policies`, `retention_policies`; function `shifde_ops_stats` (SD) | `usage_events` + 13 columns (`provider_key`, `model_key`, `cached_write_tokens`, `prompt_id`, `pipeline_id`, `orchestration_id`, `build_ref`, `rate_card_version_id`, `rate_snapshot`, `cost_amount`, `currency`, `corrects_event_id`, ...), CHECKs; `budgets` + `metric`, `amount`, `currency`, `enforcement`, `on_exceed`, `alert_thresholds`, `updated_at`; `budget_reservations` + `cost_amount`, `settled_cost`, `currency`; `policy_decisions` + `policy_ids`; `workers` + `last_reap_at` | `usage_events` -> `rate_card_versions`; `rate_card_versions` -> `rate_cards`; `budget_alerts` -> `budgets` |
| `p08_0002_governance_permissions.py` | governance | none | grants | none |
| `p08_0003_rate_card_guard.py` | governance | none | `rate_card_versions_guard` trigger function redefined (SD) | none |
| `p09_0001_installation_profile.py` | installation (not runtime) | `installation_profile` | none | none |
| `p11_0001_unknown_write_outcome.py` | runtime | functions `land_verdicts`, `unknown_outcome_text`, `settle_ended_run` (not SD); | `runs_guard` redefined; `shifde_reap_expired_leases` redefined (SD); `tool_invocations` + `arguments jsonb` | none (functions read `run_steps`, `approvals`, `jobs`, `tool_invocations` - column detail not checked) |

### 3b. Migrations that touch the named tables

| Table | Migrations (create / alter / grant / mention) |
|---|---|
| `runs` | p05_0001 (create); p05_0003 (alter); p06_0001 (alter); p06_0003 (alter FK); p07_0001 (alter); p11_0001 (guard function); p02_0001, p04_0001, p06_0002, p08_0001 mention `runs` only (UNSURE, not inspected in detail) |
| `run_steps` | p05_0001 (create); p06_0001 (alter kind CHECK); p11_0001 (function body) |
| `approvals` | p05_0001 (create, trigger `trg_approvals_guard`); p05_0002 (grants); p05_0003 (reaper body); p06_0001 (alter `approvals_reason_check`); p11_0001 (reaper and settle bodies); p02_0001 mentions (UNSURE) |
| `usage_events` | p05_0001 (create); p06_0001 (alter); p08_0001 (alter, 13 columns, `rate_card_version_id` FK) |
| `schedules` | No table of this name exists. Schedules are `pipeline_schedules`, created only in p06_0001. Cron execution is off in MVP (migration docstring). |
| `jobs` | p05_0001 (create, `ix_jobs__claim`, `ix_jobs__lease`); p05_0003 (reaper body); p11_0001 (reaper body); p08_0001 mention (UNSURE) |
| Ontology tables (6) | p07_0001 (create); p07_0002 (grants). `runs` gains ontology columns in p07_0001. |

---

## 4. SETH tests importing `shifde.runtime` or `shifde.ontology`

Found with `git grep -l 'shifde\.(runtime|ontology)'` over `backend/tests/` at 3ba0e06. Full-app means the test uses `conftest.World` (the `world` fixture) or the `p2util`..`p8util` helpers, which boot the app. Direct means the test imports only the module and uses no World or helper.

| File | Lines | World uses | Helper imports (top-level) | Runtime or ontology imports | Mode |
|---|---:|---:|---|---|---|
| `conftest.py` | 294 | 6 (defines `World`) | none | none (lists ontology table names as strings, lines 172-174) | Host of World fixture |
| `p5util.py` | 415 | 0 | p3util, p4util | `shifde.runtime.worker` (`Worker`, subprocess `python -m shifde.runtime.worker`, line 60, 81) | Helper, no World |
| `test_p5_core.py` | 406 | 10 | conftest, p2util, p3util, p4util, p5util | runtime, runtime.agent, runtime.schemas | Full app |
| `test_p5_review.py` | 778 | 21 | conftest, p2util, p3util, p5util | runtime.lifecycle, repository, context, governance, worker | Full app |
| `test_p6_build.py` | 619 | 21 | conftest, p2util..p6util | runtime.pipeline (line 81, indented) | Full app |
| `test_p6_exec5.py` | 75 | 4 | conftest, p5util, p6util | runtime.state, runtime.pipeline | Full app |
| `test_p6_review1.py` | 598 | 19 | conftest, p2util..p6util | runtime.lifecycle, state, pipeline, pipeline_ops | Full app |
| `test_p6_review2.py` | 297 | 11 | conftest, p5util, p6util | runtime.lifecycle, state, pipeline | Full app |
| `test_p6_units.py` | 270 | 0 | none | runtime.pipeline_ops (`OpError`, `apply_transform`, etc.) | Direct only |
| `test_p7_build.py` | 539 | 16 | conftest, p2util, p3util, p4util, p7util, p5util | none as module; raw SQL on `ontology_source_mappings`, `ontology_version_info` | Full app |
| `test_p7_query.py` | 828 | 20 | conftest, p2util, p3util, p4util, p7util | ontology.engine (line 295, indented); raw SQL on ontology tables | Full app |
| `test_p7_units.py` | 378 | 2 (type annotation and `world` fixture param, line 339) | conftest, p2util, p3util, p7util | ontology.engine, ontology.policy, ontology.schemas | Full app (world fixture) |
| `test_p8_budgets.py` | 522 | 16 | conftest, p2util..p5util, p8util | runtime.governance | Full app |
| `test_p8_review_r1.py` | 494 | 11 | conftest, p2util..p5util | runtime.governance | Full app |
| `test_p9_config.py` | 318 | 0 | none | runtime.worker (line 135, indented) | Direct import; uses conftest fixtures `settings`, `owner_session` (lines 274-280), no World |
| `test_r1_critic.py` | 830 | 22 | conftest, p2util, p3util, p5util | runtime.repository, runtime.settle | Full app |
| `test_r1_round2.py` | 725 | 19 | conftest, p2util, p3util, p5util, p6util | runtime.gate, state, steps (also `steps._journal_arguments`, line 690) | Full app |
| `test_r1_round3.py` | 641 | 17 | conftest, p5util, p6util | runtime.repository, state, steps, pipeline | Full app |
| `test_r1_round4.py` | 380 | 10 | conftest, p2util, p3util, p5util, p6util | runtime.gate, state, steps (`classify_tool_failure`) | Full app |
| `test_r1_round5.py` | 364 | 12 | conftest, p5util, p6util | runtime.lifecycle, state, steps | Full app |
| `test_r1_round6.py` | 436 | 18 | conftest, p2util, p3util, p5util, p6util | runtime.repository | Full app |
| `test_r1_round7.py` | 255 | 8 | conftest, p3util, p5util | runtime.lifecycle, state, steps, action, agent, graph, pipeline (lines 251-252) | Full app |
| `test_r1_unknown_outcome.py` | 561 | 14 | conftest, p3util, p5util, p6util | runtime.worker | Full app |

Summary: 19 test files plus `p5util.py` are full-app. Two are direct only: `test_p6_units.py` (needs `shifde.runtime.pipeline_ops` only) and `test_p9_config.py` (needs `shifde.runtime.worker` plus conftest fixtures without World). Those two can run before the app is assembled, if their other imports exist.



---

## 5. Audit and planning findings naming runtime or ontology files

Source: `docs/planning/REMEDIATION_PLAN.md`, `docs/audit/*.md` at 3ba0e06.

### 5a. C1 and the crash-plus-cancel defect

The brief's quotation ("worker crash during an approved write must not be reported as cancelled") is a paraphrase. The matching findings are:

| Finding | File:line | One-line summary | Cited code |
|---|---|---|---|
| E2E `C1` (Critical) | `docs/audit/E2E_AND_RELIABILITY.md:28` | Worker crash during an approved write, then Cancel or passed deadline: the delivered write is recorded as `cancelled` or `timed_out`, tool step `running` forever, no `indeterminate` approval, 1 delivery. | `p05_0003_run_lease_job.py:143-156` (reaper, cancel path), `:114-140` (deadline path); `runtime/steps.py:660-661` (classification); `runtime/settle.py:54-61` |
| ARCH `C1` (Critical) | `docs/audit/ARCHITECTURE_REVIEW.md:20` | Same defect class, "may have been delivered ... recorded as `cancelled` or left `running` on a terminal run". | `runtime/settle.py:54-61`; `runtime/lifecycle.py:441-453` (any exception -> `failed/internal`); `p05_0003_run_lease_job.py:142, :114, :74`; `runtime/service.py:690-701` (cancel); `runtime/steps.py:659-661`; `runtime/pipeline.py:1042-1048` |
| E2E `M3` (Major) | `E2E_AND_RELIABILITY.md:78` | Cancel and deadline close "outcome unknown" approvals, so the human decision is lost. | `runtime/steps.py:820` (tool timeout capped by remaining deadline); reaper expiry |
| E2E `m1` (Minor) | `E2E_AND_RELIABILITY.md:105` | Cancel needs a worker; reaper runs only in worker. | `runtime/service.py:688`; `runtime/worker.py:290` |
| E2E `m3` (Minor) | `E2E_AND_RELIABILITY.md:107` | `recovered` events emitted on normal re-advance, reads as crash recovery. | `runtime/lifecycle.py:145-148` |
| ARCH `M3` (Major) | `ARCHITECTURE_REVIEW.md:65` | Wedged worker thread never recovered. | `runtime/worker.py:246-269` |
| ARCH `m4` (Minor) | `ARCHITECTURE_REVIEW.md:142` | Reaper terminal transitions skip `settle_terminal`; approvals not cancelled on `worker_lost`; pipeline parent not woken. | `p05_0003` reaper; `runtime/settle.py` |
| ARCH `m5` (Minor) | `ARCHITECTURE_REVIEW.md:143` | Worker does not check schema revision at start. | `runtime/worker.py` |
| ARCH `M1` (Major) | `ARCHITECTURE_REVIEW.md:42` | Hard budgets overshoot by the model's returned tokens. | `runtime/steps.py:228, :266-269` |
| ARCH `M12` (Major) | `ARCHITECTURE_REVIEW.md:126` | Usage can be silently wrong while labelled `calculated`. | `runtime/steps.py:130-141, :212-222` |
| ARCH boundaries table | `ARCHITECTURE_REVIEW.md:202, :205` | `runtime/steps`, `pipeline`, `graph`, `worker` are outside the documented layering; `shifde.runtime.repository` is not forbidden by import-linter. | `pyproject.toml` (import-linter) |

### 5b. Requirements findings naming runtime or ontology files (`REQUIREMENTS_COMPLIANCE.md`)

| Finding | Line | One-line summary | File(s) |
|---|---:|---|---|
| F-02 | 17 | Archived workspace or project not inert (only tenant status checked). | `runtime/service.py:85, :194` |
| F-05 | 23 | Per-run `max_cost` never enforced. | `runtime/agent.py:266-280` |
| F-06 | 25 | No tool-invocation audit event. | `runtime/steps.py:885` |
| F-08 | 29 | RLS is tenant-only, not workspace/project. | `migrations .../p05_0001_runtime.py:77-79` |
| F-10 | 33 | Approval rules do not apply to pipeline runs. | `runtime/pipeline.py` (no policy call) |
| F-12 | 37 | Content capture "off" only hides content on read. | `runtime/steps.py:341, :954`; `runtime/repository.py:102`; `runtime/service.py:539` |
| F-13 | 39 | `allowed_agent_ids` enforced for agents only; per-tool approval flag absent. | `runtime/steps.py:518` |
| F-20 | 53 | Pipeline test mode runs real writes. | `runtime/pipeline.py:232`; default approvals at `:68` (file for `:68` UNSURE) |
| F-04 | 21 | Numeric keys above 2^53 return the wrong object (ontology). | no file cited |

Matrix rows citing runtime or ontology files (not findings, but relevant to porting): FR-EXE-001 (`runtime/agent.py, lifecycle.py, worker.py`), FR-EXE-003 (`runtime/state.py`), FR-EXE-006 (`runtime/gate.py`), FR-ONT-006/-007/-009/-010 (`ontology/engine.py`, `service.py`), FR-PIPE-004/-007/-011 (`pipeline.py`, `gate.py`, `schedules.py`), FR-GOV-002 (`runtime/governance.py`), NFR-REL-001/-003 (`runtime/worker.py`, `steps.py`), NFR-REL-002 (`runtime/steps.py:81`).

### 5c. Remediation plan

`docs/planning/REMEDIATION_PLAN.md` names no runtime or ontology file.

| Line | Batch | Relevant text |
|---:|---|---|
| 17 | R1 Release blockers | "C1 and J-M3: truthful terminal state and open decisions for unknown write outcomes" (refers to E2E C1 and M3; no file) |
| 18 | R2 Security | "runtime role RLS check" (role, not a file); "numeric keys" (ontology F-04) |
| 19 | R3 Correctness and governance | "budget overshoot; `max_cost`; usage honesty; archived and suspended scopes; `capture_content`; capture off stops storing; execution audit; per-tool approval, `audit_level`" (maps to ARCH M1, F-05, M12, F-02, F-12, F-06, F-13) |

---

## 6. SECURITY DEFINER functions and cross-tenant SQL

### 6a. SECURITY DEFINER functions (runtime path)

| File:line | Function | Note |
|---|---|---|
| `backend/migrations/versions/p05_0001_runtime.py:92-94` | `shifde_reap_expired_leases()` (first version) | SD, cross-tenant reaper; superseded |
| `backend/migrations/versions/p05_0001_runtime.py:233-236` | `shifde_claim_jobs(...)` | SD, cross-tenant claim (`FOR UPDATE SKIP LOCKED`), returns ids only; called from `runtime/repository.py:910` |
| `backend/migrations/versions/p05_0003_run_lease_job.py:36-38` | `shifde_reap_expired_leases()` (second version) | SD; superseded |
| `backend/migrations/versions/p11_0001_unknown_write_outcome.py:449-451` | `shifde_reap_expired_leases()` (current) | SD, cross-tenant; called from `runtime/repository.py:983` |

Not SD: `settle_ended_run` (`p11_0001_unknown_write_outcome.py:159`), `land_verdicts` (`:69`), `unknown_outcome_text` (`:128`). They run as the caller, so RLS applies. Governance SD functions outside the scope: `shifde_ops_stats` (`p08_0001_governance.py:60-62`), `rate_card_versions_guard` (`p08_0003_rate_card_guard.py:24-25`). ARCH m6 says the SD list drifted (14 functions exist, DATA_MODEL lists 8).

### 6b. Raw SQL with no tenant predicate in the statement (protection is FORCE RLS plus session tenant context)

Only runtime and ontology `src` files at 3ba0e06. Each line is one statement without `tenant` in the statement text. Whether every caller sets tenant context is UNSURE and was not traced.

| File:line | Statement scope | One line |
|---|---|---|
| `runtime/repository.py:116` | `runs` by id | `get_run` |
| `runtime/repository.py:239, :900, :805, :871, :975` | `runs` by id | status, state_version, trace_id, root pipeline lookups |
| `runtime/repository.py:248, :259, :266, :996` | `service_accounts`, `api_keys` by id | principal lookups |
| `runtime/repository.py:1000` | `users` (global table, `users_select` policy) | display name by id; cross-tenant by design |
| `runtime/repository.py:457, :1069` | `run_artifacts` by id | artifact content read |
| `runtime/repository.py:678-681` | recursive CTE over `runs` by id, `approvals` filter | approval tree walk |
| `runtime/repository.py:767-770` | recursive CTE over `runs`, `approvals` (`status='pending'`, reason indeterminate) | cross-root check for indeterminate approvals |
| `runtime/repository.py:922, :935, :948, :960` | `jobs` by id or run_id | heartbeat, finish, release, finish_run_jobs |
| `runtime/repository.py:1084-1085` | `run_steps` JOIN `runs` on `child_run_id` | finished awaited children |
| `runtime/repository.py:989, :1010, :1021` | `workers` (global table, `USING (true)`) | reap mark, register, heartbeat; cross-tenant by design |
| `runtime/policies.py:50-51` | `governance_policies` by kind and scope | tenant predicate, if any, is in `scope_match()`; UNSURE |
| `runtime/governance.py:267` | `budgets` by id array | budget lookup |
| `runtime/governance.py:279-281` | `budget_reservations` JOIN `runs` twice by id | root run lookup |
| `runtime/lifecycle.py:200, :203` | `runs`, `jobs` by id | trace and traceparent |
| `runtime/service.py:745` | `runs` by id | cancel request update |
| `ontology/service.py:312` | `runs` by id | root and agent lookup |
| `ontology/repository.py:35-50, :98, :111, :123-138, :184` | `ontology_access_policies` and `ontology_principal_attributes` by ontology or principal id (no tenant) | policy and attribute reads and writes |
| `ontology/repository.py:220, :234` | `ontology_query_lineage` by ontology id or lineage id | lineage read |

Runtime's queue and reaper functions (6a) are the only deliberate cross-tenant readers. Everything else in 6b depends on RLS.

---

## Surprises and open items

- The four largest runtime files (`pipeline.py` 1447, `steps.py` 1172, `service.py` 1023, `repository.py` 1091) each exceed the 400-600 band, so file-level parts cannot meet it.
- The brief's `schedules` table does not exist; the table is `pipeline_schedules` (p06_0001).
- The C1 cause is partly in SQL: reaper migrations (p05_0003, p11_0001) and `settle` drive the cancel path. Porting runtime means the migration chain must come with it.
- `runtime/schedules.py` imports `shifde.admin.iam`, a dependency on the admin module that is not obvious from the runtime name.
- `runtime/policies.py` imports `steps` only under TYPE_CHECKING. Mypy will need both files; runtime does not.
- Two tests (`test_p6_units.py`, `test_p9_config.py`) do not need the full app; 19 test files do.
- Ontology has no reverse dependency on runtime, so ONT-1 and ONT-2 can port first.
