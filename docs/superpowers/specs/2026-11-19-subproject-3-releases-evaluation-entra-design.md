# Sub-project 3: releases, evaluation, cost reservation, Entra sign-in — short design

Date: 2026-11-19. Status: draft for the lead (CHARTER 7 step 1). Contract: unified design sections 5.2, 5.3, 5.5, 5.6, 6, 7 (call 13), 9.

## 1. Scope and "done when"

**Done when (design section 6):** "A release cannot be deployed without a passing evaluation; staff sign in with Entra."

- The first half is proven entirely inside SETH (it needs no client platform): a plan request for a release with no passing evaluation is refused; after an evaluation of that release passes, the same request is accepted and the deployment runs against the **fake** Databricks platform (sub-project 2's executor and fake).
- There is no Entra registration (`.devdata/entra.env` absent). The second half is therefore finished against a **fake Entra issuer** (the existing in-process test IdP in `tests/_support/fake_oidc.py`, extended to issue Entra v2.0 tokens: `tid`, `oid`, `ver`, `groups` as object ids, group overage claims). The Entra profile stays `experimental`; STATUS records "live validation waiting on owner input"; REQ F-22 stays open until a live sign-in. A live test file exists and runs only with `.devdata/entra.env` present **and** `-m live`.

In scope: new modules `shifde.packaging` (solutions, solution versions, compatibility check, sealed releases) and `shifde.evaluation` (datasets, evaluation runs, the deployment gate); `delivery` plans that deploy a release; cost reservations that survive a crash and cover direct model invocations; the Entra profile on SETH's existing OIDC sign-in and group sync; screens, tests, demo and runbook.

Out of scope: generating the native application from a release (sub-project 5; until then the deployable archive is still the SDK template, now carrying the release manifest); Snowflake (6); the supervisor and specialists (7); the standalone Docker target (moved to sub-project 8, which already lists it; B-2698); Agent Factory's per-release hard-coded model and tool allow-lists.

## 2. What exists and what is reused

| Need | Already in this repo (reuse) | From Agent Factory `0fdd09c` (port the idea, not the code shape) |
|---|---|---|
| Pinned, immutable building blocks | `build`: `artifact_versions` (`state` published, `spec_hash`, immutability trigger), `artifact_version_pins`, `pipeline_version_refs`, `build/canonical.py` | — |
| Canonical digest | `build/canonical.py` (sha256 of canonical JSON) | `compiler.canonical_bytes` rules: sorted keys, `(',',':')`, `ensure_ascii=False`, `allow_nan=False`, money as decimal strings; release id = digest of the payload without the id |
| Compatibility report | — | `validate_compatibility`: one `Diagnostic{path, status, reason, mandatory}` per feature; `requires_explicit_acceptance` passes only if listed in `accepted_deviations`; compile raises with the report |
| Evaluation | `runtime` runs a published agent or pipeline version; ledger records usage | `evaluations`: fixed assertions per case, all must pass; latency and cost limits; result bound to the release by its id. AF never gated deployment on it: the gate is new work |
| Reservation | `ledger.budget_reservations` (held/settled/released/expired), `runtime/governance.py` `PolicyBudgetHooks.reserve/settle/release`, `steps.call_model` reserves via `gate.check(reserve=...)` | AF keeps a crashed call's reservation counted ("unknown" keeps the full amount); never silently frees it |
| Entra | `identity/oidc.py` (PKCE, `validate_id_token`, `(iss, sub)` mapping), `idp_configs` (`claim_mappings`, `group_sync_mode`), `shifde_sync_idp_groups` (groups with `source='idp'`, `external_id` match), `fake_oidc.py` | `auth/identity.py`: issuer must be `https://login.microsoftonline.com/{tid}/v2.0`; `tid` equals the configured tenant; `ver == "2.0"`; GUID parsing; principal `{tid}:{oid}` |

## 3. Modules and layers

New layer entries in `backend/pyproject.toml` (issue 1 adds both):

```
"shifde.main | shifde.executor",
"shifde.app | shifde.cli",
"shifde.delivery",
"shifde.evaluation",      # new: imports packaging, runtime, build
"shifde.packaging",       # new: imports build, models_admin, connections
"shifde.runtime",
...
```

`delivery` may import `evaluation` and `packaging` (it gates on both); `evaluation` imports `packaging`; `packaging` imports neither. No cycle (design 8). Both new modules join the "api -> service -> repository -> models" container contract and the "services stay HTTP-free" list. The executor contract ("never reaches the database") is unchanged and must keep passing; `shifde.executor` imports nothing new.

## 4. Data model

All client-owned: forced RLS, `tenant_isolation` on `tenant_id = current_tenant_id()`, composite `(tenant_id, ...)` foreign keys, column-wise grants to `shifde_app`, `created_at`/`updated_at`. **No new global table.**

**Migration branch `packaging`** (`down_revision="base_0001"`, `branch_labels=("packaging",)`, `depends_on` the build, models_admin, connections and tenancy heads):

| Table | Key columns |
|---|---|
| `solutions` | `tenant_id`, `id`, `workspace_id`, `project_id` (required), `name` (unique per project), `target_kind` (`databricks.app/v1` only for now), `status` (`active`/`archived`), `created_by` |
| `solution_versions` | `tenant_id`, `id`, `solution_id`, `version_no` (monotonic per solution), `entrypoint_kind` (`agent`/`pipeline`), `entrypoint_version_id` (composite FK to `artifact_versions`; must be `published` at insert), `spec` jsonb (data reads: list of `{connection_id, tables[]}`; screens: reserved, empty list until sub-project 5; `evaluation`: `{dataset_id, min_accuracy, max_p95_latency_ms, max_total_cost_usd}`), `spec_hash`, `created_by`. Immutable after insert (trigger) |
| `releases` | `tenant_id`, `id`, `solution_version_id`, `digest` (64 hex, UNIQUE per tenant), `manifest` jsonb (section 5), `compatibility` jsonb (the report), `status` (`sealed`/`withdrawn`), `sealed_by`, `sealed_at`. Immutable except `status` -> `withdrawn` (trigger) |

**Migration branch `evaluation`** (`depends_on` packaging and runtime heads):

| Table | Key columns |
|---|---|
| `evaluation_datasets` | `tenant_id`, `id`, `project_id`, `name`, `version_no`, `cases` jsonb (list of `{case_id, input, assertions[]}`, at most 200 cases, at most 1 MB), `digest`, `created_by`. Immutable after insert; a change is a new version |
| `evaluation_runs` | `tenant_id`, `id`, `release_id`, `release_digest`, `dataset_id`, `dataset_digest`, `thresholds` jsonb (copied from the release manifest), `status` (`queued`/`running`/`passed`/`failed`/`error`), `status_reason`, `accuracy` (numeric), `p95_latency_ms`, `total_cost_usd` (decimal string, estimate), `results` jsonb (per case: run id, each assertion `{id, passed, reason}`, latency, cost), `requested_by`, `started_at`, `finished_at` |

**Delivery migration `delivery_0006_releases`**: `delivery_plans.release_id` (composite FK to `releases`), `delivery_plans.release_digest`, `delivery_plans.evaluation_run_id` (composite FK to `evaluation_runs`); all nullable at the database so existing rows stay valid, required by the service for new plans. Downgrade drops them.

**Ledger migration `ledger_0003_reservation_outcome`** (only if issue 5 needs a new status value): `budget_reservations.status` gains `settled_unknown` (the hold is charged in full because the call's outcome is unknown). Downgrade maps it back to `settled`.

**Identity migration `identity_0007_entra_profile`**: `idp_configs.profile` (`generic`/`entra`, default `generic`), `idp_configs.entra_tenant_id` (uuid, required when `profile='entra'`, CHECK). Users already map by `(iss, sub)`; for `entra` the subject stored is the `oid` claim (see 7).

Permissions (seeded the `delivery_0001` way): `packaging:solution:read|write`, `packaging:release:seal|withdraw`, `evaluation:dataset:read|write`, `evaluation:run:read|request`. Owners and admins get all; FDEs (`DEVELOPER`) get all but `packaging:release:withdraw`.

## 5. Releases (`packaging`)

- **Solution version** = the published entrypoint version (agent or pipeline), its transitive pins (prompts, tools, models, connections, via `artifact_version_pins` and `pipeline_version_refs`), the data reads and the evaluation contract.
- **Seal** (`POST .../solutions/{id}/versions/{vid}/releases`, body `{environment_bindings}`): `environment_bindings` = `{model_bindings: {<pin name>: {provider_id, model}}, connection_bindings: {<connection pin>: connection_id}}`. The service resolves every pin to its `spec_hash`, runs the compatibility check for the solution's `target_kind`, and:
  - if compatible, writes a `releases` row whose `digest` = sha256 of canonical JSON of the manifest (manifest = `{schema_version: 1, solution_id, solution_version_id, version_no, target_kind, entrypoint: {kind, version_id, spec_hash}, pins: [{role, version_id, spec_hash}] sorted, data_reads, bindings, evaluation: {dataset_id, dataset_digest, thresholds}, compiler_version}`; the manifest does not contain its own digest);
  - if not, answers `422 release_incompatible` with the full report and writes nothing.
  Sealing the same version with the same bindings twice returns the existing release (same digest, 200).
- **Compatibility check for `databricks.app/v1`** (design 5.5's first subset). One diagnostic per feature, `path` naming it (`tools.<pin name>`, `models.<pin name>`, `connections.<pin name>`, `entrypoint`, `evaluation`):
  - entrypoint is a published agent or pipeline: else unsupported;
  - each tool: supported only if read-only (no write class, no outside HTTP call, no `invocation_policy.require_approval`); otherwise `unsupported` with the reason ("writes", "calls an outside service", "needs a human approval step");
  - each model binding: the provider exists in this organization (the shared SHI provider is refused for a client release: a deployed app uses models served in the client's workspace, design call 7);
  - each connection binding: belongs to this organization and its vendor is Databricks;
  - evaluation contract present with a dataset of this project: else unsupported ("a release needs an evaluation dataset").
  No `accepted_deviations` in this version: every gap is mandatory (B-2699).
- **Withdraw** (`POST .../releases/{id}/withdraw`, note required, audited): a withdrawn release cannot be planned; deployments already made are untouched.
- All seal, refuse and withdraw events are audited (`packaging.release.sealed|refused|withdrawn`).

## 6. Evaluation (`evaluation`) and the deployment gate

- **Dataset**: cases `{case_id, input: {message: str} | object, assertions: [...]}`. Assertion kinds (closed list): `equals` (output text equals), `contains` (substring, case-sensitive), `not_contains`, `regex` (full match, Python `re`, 200-char pattern cap, 1 s budget), `json_equals` (output parses as JSON and equals the expected value). A case passes when every assertion passes.
- **Run** (`POST .../releases/{id}/evaluations`, body `{}`: the dataset and thresholds come from the release manifest): inserts an `evaluation_runs` row and queues one worker job `evaluation.run` (SETH's runtime queue, `shifde.jobs`). The job starts one run per case of the release's pinned entrypoint version with the release's model bindings, sequentially, as the requesting user, inside the release's organization, workspace and project, with the run's `max_cost` set from the release's remaining cost threshold. Each case's run is a normal SETH run (ledger, budgets and reservations apply; run history stays in the organization).
- **Scoring**: `accuracy = passed cases / total cases`; `p95_latency_ms` over case run durations; `total_cost_usd` = sum of the case runs' estimated costs from the ledger. The run is `passed` when `accuracy >= min_accuracy` **and** `p95_latency_ms <= max_p95_latency_ms` **and** `total_cost_usd <= max_total_cost_usd`; `failed` otherwise; `error` if a case run could not start or ended in an error that is not an assertion failure (an `error` never counts as a pass). The run records `release_digest` and `dataset_digest` at request time.
- **Gate** (in `delivery.PlanService.request`, beside the target checks, and again in `_decide` before the approval is written): a plan request names `release_id`; it is refused with `409 evaluation_required` ("This release has no passing evaluation.") unless there is an `evaluation_runs` row with `status='passed'`, `release_id` = the release, `release_digest` = the release's digest and `dataset_digest` = the manifest's `dataset_digest`. The plan stores `release_id`, `release_digest` and the passing `evaluation_run_id`. A withdrawn release is refused with `409 release_withdrawn`. A release whose `target_kind` differs from the target's kind is refused with `409 release_target_mismatch`. The plan digest (sub-project 2) adds `release_digest`. The plan request body becomes `{target_id, release_id, application_key, display_name}`; `release_id` is required (the template-only plan disappears).
- **Artifact**: until sub-project 5, `delivery` still builds the SDK template archive, and adds `release.json` (the canonical manifest plus its digest) to it, so the artifact digest changes with the release. The deployment screen shows the release and the evaluation it passed.

## 7. Cost reservation (design 5.6)

What exists already covers "reserve before each model call" inside runs. The gaps this sub-project closes:

1. **A crash must not lose a reservation.** Today an orphaned hold stops counting after its 10-minute TTL. New rule: when a run step whose model call may have reached the provider is found orphaned (the resume path that marks a `running` step failed, and the reaper that settles an abandoned run), its hold becomes `settled_unknown`, and a usage row is written at the held amount marked `estimated: true`, `usage_unknown: true`. TTL expiry remains only for holds whose call provably never started (no provider request recorded). Proven by a crash test: a worker dies between reserve and settle; the budget still counts the full hold after the TTL.
2. **Direct model invocations reserve too.** `models_admin` direct invoke (`service.py` `_enforce_budgets`) places a hold through the same `ledger` hook before the call and settles or releases it after.
3. **Evaluation runs are charged** like any run, to the project that owns the release.
Budgets stay per client, per project and per user (already in `governance`).

## 8. Entra sign-in (`identity`)

- An installation IdP (platform administrator, existing `/platform/idps` routes) gets `profile: entra` and `entra_tenant_id`. Validation on save: issuer is exactly `https://login.microsoftonline.com/{entra_tenant_id}/v2.0`; discovery URL is that issuer's `/.well-known/openid-configuration`; `claim_mappings.groups = "groups"`.
- On callback, beyond the existing checks (`validate_id_token`: signature, `iss`, `aud`, `exp`, nonce, PKCE): `tid` equals `entra_tenant_id`; `ver == "2.0"`; `oid` present and a GUID; the user key is `(iss, oid)` (Entra's `sub` is pairwise per application and is not used); `email` falls back to `preferred_username` only if `email` is missing.
- **Groups.** Entra sends group object ids (GUIDs) in `groups`. A platform administrator maps a SETH platform group to an Entra group by setting the group's `idp_config_id` and `external_id` = the Entra object id (existing admin route; GUID format validated for `entra`). Sign-in runs the existing `shifde_sync_idp_groups`. **Group overage** (`_claim_names.groups` or `hasgroups`) means the token does not list the user's groups: sign-in is **refused** with "Your Entra account is in too many groups for SETH to read. Ask an administrator to limit the groups sent to SETH." and audited (`identity.oidc.groups_overage`); memberships are left unchanged (B-2700). Fail closed: no sign-in with a partial group list.
- Only installation IdPs may use `entra` (staff are SHI-wide; design 5.3). An organization IdP with `profile: entra` is refused (422).
- The fake issuer used by tests and the demo: `tests/_support/fake_oidc.py` gains an Entra mode (issuer `https://login.microsoftonline.com/<test tenant>/v2.0` served through `httpx.MockTransport`; it is a test double, never reachable outside tests). The demo script uses the same fake in-process.
- Live: `tests/identity/test_entra_live.py`, `-m live`, reads `.devdata/entra.env` itself (keys in the runbook), skipped with reason "live validation waiting on owner input: no Entra registration" otherwise. Runbook: `docs/runbooks/entra-live-validation.md`.

## 9. Screens

Under `frontend/src/app/(app)/o/[org]/`:
- `solutions` (list per project, create, archive), `solutions/[id]` (versions; create a version by picking a published agent or pipeline version, data reads and evaluation contract; seal a release with bindings; the compatibility report shown feature by feature when refused),
- `releases/[id]` (manifest summary, digest, evaluations list, "Run evaluation", withdraw),
- `evaluations/datasets` (list, create version, upload cases as JSON), `evaluations/[id]` (status, accuracy, latency, cost against thresholds, per-case results),
- Deliver `plans` request form: pick a release instead of the template; the "evaluation required" refusal shown with a link to the release,
- Platform IdP form: profile `entra`, tenant id, group mapping by Entra object id.

## 10. Factory issues (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`; listed below are the issue-specific commands. Every new route is classified in `backend/tests/authz/scoping/test_workspace_routes.py` and has a cross-client case (A's user asks for B's object: 404). **C** = isolation-critical. Migrations are new files under a protected path, so the approver escalates and the lead merges by hand after 13/13 (B-2530).

1. **packaging: solutions and solution versions (C).** Layer entries for `packaging` and `evaluation`; `packaging_0001`; routes `/api/v1/orgs/{org}/solutions...`; pin resolution; immutability trigger. Checks: `uv run pytest tests/packaging/test_solutions.py tests/packaging/test_routes_cross_client.py tests/schema`. Depends: none.
2. **packaging: compatibility check and sealed releases (C).** `packaging_0002`; seal, idempotent re-seal, refuse with report, withdraw; canonical digest. Checks: `uv run pytest tests/packaging tests/schema`. Depends: 1.
3. **evaluation: datasets, evaluation runs, scoring (C).** `evaluation_0001`; `evaluation.run` worker job; assertion kinds; thresholds. Checks: `uv run pytest tests/evaluation tests/schema`. Depends: 2.
4. **delivery: plans deploy a release behind the evaluation gate (C).** `delivery_0006`; plan body with `release_id`; three refusals; `release.json` in the artifact; plan digest; update `tests/delivery` and `scripts/delivery_demo.py --fake` (it must seal a release and pass an evaluation first). Checks: `uv run pytest tests/delivery tests/executor tests/schema; python scripts/delivery_demo.py --fake`. Depends: 3.
5. **ledger/runtime: reservations survive a crash; direct invokes reserve (C).** Section 7 items 1-2, crash test. Checks: `uv run pytest tests/ledger tests/runtime tests/models_admin tests/schema`. Depends: none.
6. **identity: Entra profile and Entra group mapping (C).** `identity_0007`; section 8; fake Entra issuer; overage refusal; live test skipped. Checks: `uv run pytest tests/identity tests/admin tests/schema; uv run pytest tests/identity/test_entra_live.py -m live` (reports skipped). Depends: none.
7. **Front end: solutions, releases and evaluations screens.** Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`. Depends: 3.
8. **Front end: plan request by release; Entra IdP form.** Checks: as 7. Depends: 4, 6, 7.
9. **Done-when demo and Entra runbook.** `scripts/release_demo.py --fake` (in-process, local cluster): seal a release, plan refused `evaluation_required`, failing evaluation still refused, passing evaluation, plan accepted, second person approves, deploy to the fake, verified; Entra sign-in through the fake issuer with a mapped group giving access to one client and not the other; overage refused; ends `RELEASE DEMO PASSED`. `docs/runbooks/entra-live-validation.md`. Checks: `python scripts/release_demo.py --fake`. Depends: 4, 5, 6, 8.

## 11. Open questions (with recommended answers)

1. Where does the "solution" live, given 5.5 introduces it and sub-project 5 generates from it? **In `packaging`, now**, minimal: entrypoint, data reads, evaluation contract; screens are a reserved empty list.
2. Standalone Docker target in this sub-project (design 5.2 lists it under `packaging`)? **No; sub-project 8** lists it by name.
3. Evaluate in SETH or on the client platform? **In SETH**, with the release's model bindings (client-served models through the organization's providers); the deployed app is not evaluated in place until sub-project 5.
4. One passing evaluation per release, or per (release, dataset version)? **Per release digest and the manifest's dataset digest**; a new dataset version needs a new solution version and release.
5. Re-check the gate at approval? **Yes**; and the plan pins the evaluation run id.
6. Accepted deviations? **None in this version**; every incompatibility blocks.
7. Group overage: skip sync, drop memberships, or refuse sign-in? **Refuse sign-in** (fail closed, audited, memberships unchanged).
8. Entra user key: `sub` or `oid`? **`oid`** (stable across applications in the tenant).
9. Organization-level Entra IdPs? **No**; installation only.
10. Reservation status for a crashed call? **`settled_unknown` at the held amount**, never silently expired.

## 12. Settled calls (lead, 2026-11-19; DECISIONS B-2698..B-2701)

Every recommended answer in section 11 is accepted (B-2698; standalone Docker moves to sub-project 8). A fresh builder read this design and listed what it would have to guess; settled here (B-2701). These bind every issue in section 10 and override earlier text where they differ.

**Ledger (issue 5)**
1. Direct invokes have no run. `ledger_0003_reservation_outcome` makes `budget_reservations.run_id` nullable, adds `invocation_id uuid`, a CHECK that exactly one of `run_id` / `invocation_id` is set, replaces the `(tenant_id, run_id, step_key)` unique with two partial unique indexes (`run_id IS NOT NULL` on `(tenant_id, run_id, step_key)`; `invocation_id IS NOT NULL` on `(tenant_id, invocation_id)`), keeps forced RLS and the column grants, and widens the status CHECK to add `settled_unknown`. Every query that sums held or spent amounts counts `settled_unknown` as spent. The migration is required, not optional.
2. `steps.call_model` records "provider request started" before the provider call (a marker on the existing step row, or a column added in a new migration; builder's choice, documented in build.md). The orphan handlers (the reaper in `runtime/worker.py`, and the resume path in `steps.py` / `runtime/lifecycle.py`) move a `held` reservation to `settled_unknown`, with a usage row at the held amount marked `estimated: true`, `usage_unknown: true`, when the marker is present, and to `released` when it is absent. Settle and release SQL keep matching `status='held'` only.

**Delivery (issue 4)**
3. The plan digest stays computed when the plan result arrives (`delivery/service.py`); `release_digest` is added as one more field of the digest input; the digest tests are updated to the new input, not loosened.
4. `delivery` reaches the gate through service functions only: `evaluation.service.require_passing_evaluation(session, release_id)` returning the release and the passing evaluation run, and `packaging.service.get_release`; never raw SQL on the new tables.
5. `release_id` becomes required in the plan request. Issue 4 updates `tests/delivery`, `tests/executor`, `scripts/delivery_demo.py`, `backend/openapi.json` (`uv run python -m tests._support.openapi_snapshot`) and the minimal front-end types and plan form needed to keep `pnpm typecheck` and `pnpm test` green (a plain release select is enough; issue 8 polishes). Responses tolerate `release_id: null` for older plans.
6. Re-check at approval (`_decide`, after the plan row lock): withdrawn release gives `409 release_withdrawn`; no longer a valid passing evaluation gives `409 evaluation_required`. The plan stays `sealed` (no state change, no approval row).

**Routes and scoping (issues 1-3)**
7. Routes: `/api/v1/orgs/{org}/workspaces/{ws}/projects/{project}/solutions[/{id}[/versions[/{vid}[/releases]]]]`, `/api/v1/orgs/{org}/releases/{id}[/withdraw|/evaluations]`, `/api/v1/orgs/{org}/workspaces/{ws}/projects/{project}/evaluation-datasets[/{id}]`, `/api/v1/orgs/{org}/evaluation-runs/{id}`. All are SCOPED. Releases and evaluation runs derive their scope from `solution_versions -> solutions` (workspace, project), and every read and write checks the PDP with that `ResourceRef`; lists filter to the caller's visible scopes. `evaluation_datasets` gets `workspace_id` too. Each new route gets a classification and a builder entry in `tests/authz/scoping/test_workspace_routes.py` (refused on another workspace, allowed on its own) plus a cross-client case.
8. Layers: `"shifde.delivery", "shifde.evaluation", "shifde.packaging", "shifde.runtime"`, in that order. Both new modules join the core forbidden list, the executor and `adapters.native` forbidden lists, the container contract and the HTTP-free services list. `packaging` reads build data through `build`'s service or repository functions only (add read functions there if missing).
9. Tests live in `backend/tests/packaging/` and `backend/tests/evaluation/` with `__init__.py`. `backend/tests/schema/*` is protected: if a schema test needs an entry for the new tables, the builder writes the exact entry in build.md and the lead adds it at merge (B-2530 pattern). The new tables are not ported from SETH, so no SETH comparison entry should be needed.
10. Migration dependencies name real revision ids: the head of each named branch at build time (`uv run alembic heads`). `delivery_0006` depends on the `packaging` and `evaluation` heads.
11. A published artifact version referenced by a solution version cannot be deleted out from under a release: the composite FK is `ON DELETE RESTRICT`; if `build` has a retire path, retiring a version pinned by a sealed, non-withdrawn release is refused with 409.
12. Pin resolution is the transitive closure over `artifact_version_pins` and `pipeline_version_refs` from the entrypoint, depth cap 8, cycle-safe; each pin's `spec_hash` comes from `build`. The manifest digest uses a new `packaging/canonical.py` (sorted keys, `(',',':')`, `ensure_ascii=False`, `allow_nan=False`, money as decimal strings, floats refused); `build/canonical.py` is unchanged. The digest includes `compiler_version` and excludes sealing time and the sealer.
13. `releases` has UNIQUE `(tenant_id, digest)`; seal is `INSERT ... ON CONFLICT DO NOTHING`, then select.
14. Read-only tools: a tool is supported only if its kind is on an explicit read-only allow-list in `packaging` (initially query tools over a connection, where `build` exposes such a kind) and it has no `invocation_policy.require_approval`; anything else is unsupported with a reason (an unknown kind gives "not on the read-only list").
15. The shared SHI provider (`tenant_id` NULL; `build_0002` shared model refs) is refused for release model bindings: "a deployed application uses models served in the client's workspace".

**Evaluation (issue 3)**
16. The builder checks `runtime/worker.py` and the jobs claim for new job kinds. If a run-less job kind cannot be added without changing runtime's claim or reaper semantics, the evaluation instead starts its case runs from the API request (each case an ordinary queued SETH run), and a scoring step finalizes once all case runs have ended (on read, or by a worker job; builder's choice). Either way, nothing changes in how runtime re-queues or retries a write.
17. Case runs run as the requesting user, through the mechanism scheduled runs use (`runtime/schedules.py`); the requester's permissions are checked at request time (`evaluation:run:request` and permission to start runs in the project).
18. A case whose cost is unknown (unpriced model, unknown usage) makes the evaluation `error`, never `passed`.
19. A dataset has 1 to 200 cases; `accuracy` is numeric(5,4); p95 is nearest-rank over case durations (one case: that case).
20. Result columns of `evaluation_runs` (`status`, `accuracy`, `p95_latency_ms`, `total_cost_usd`, `results`, `finished_at`) are written only by the scoring path; `release_id`, `release_digest`, `dataset_id`, `dataset_digest`, `thresholds` are immutable (trigger); a terminal status never changes.
21. Regex assertion: pattern capped at 200 characters, evaluated with the `regex` package's `timeout=1.0` if it is already a dependency, else in a subprocess with a 1 s timeout; if neither is acceptable, drop `regex` from the assertion list and record why in build.md.
22. Dataset size: 1 MB enforced by the request model and a DB CHECK (`octet_length(cases::text) <= 1048576`), and it must fit under the app's global body cap.

**Entra (issue 6)**
23. For `profile='entra'`, right after `validate_id_token` and the `tid`/`ver`/`oid` checks, a derived claims dict sets `sub := oid`, so `_map_identity`, `resolve_identity` and JIT stay unchanged.
24. For `entra` with the pinned `tid`, `email` (or `preferred_username` when `email` is absent) counts as verified for JIT: the tenant is pinned and administered by SHI. The runbook says so.
25. Overage is detected before `_sync_groups`; a new `OidcFailure` reason `groups_overage` carries the section 8 message, goes through `record_failure` and is audited `identity.oidc.groups_overage`.
26. The fake issuer gains an Entra mode without changing its default mode; `login.microsoftonline.com` reaches the in-process transport through the existing resolver mapping, in tests only.
27. The `live` marker description in `pyproject.toml` covers Databricks and Entra. `identity_0007` adds `profile`, `entra_tenant_id` and CHECK `profile <> 'entra' OR (entra_tenant_id IS NOT NULL AND tenant_id IS NULL)`; the request schemas validate the same (422).
28. A group `external_id` for an `entra` IdP is validated as a GUID and stored lowercase; the sync lowercases claim values before matching.

**Front end and demo (issues 7-9)**
29. Front-end types are hand-written as in the existing `deliver` screens; issue 4 keeps the front end green (item 5).
30. The demo uses `demo-a` and `demo-b` from `dev-seed`: a platform group mapped to an Entra group id and bound only in `demo-a`; the Entra user sees `demo-a` and not `demo-b`. The demo also shows that an evaluation case run's ledger rows include a settled reservation (so issue 9 keeps its dependency on 5).
31. Permissions are seeded in `packaging_0001` and `evaluation_0001` the `delivery_0001` way; any authz test pinning a permission count is updated with the new keys, never loosened.
32. Migrations are new files under a protected path: the job's approver escalates; the lead runs the 13-gate set and `isolation-reviewer`, then merges by hand (B-2530), as in sub-project 2.
