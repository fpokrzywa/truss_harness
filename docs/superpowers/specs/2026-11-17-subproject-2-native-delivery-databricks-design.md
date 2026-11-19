# Sub-project 2: native delivery, Databricks — short design

Date: 2026-11-17. Status: draft for the lead (CHARTER 7 step 1). Contract: unified design sections 1, 5.1-5.4, 6, 9.

## 1. Scope and "done when"

**Done when (design section 6):** "The SDK's template Databricks App is planned, approved by a second person, deployed and verified from SETH's screens against a real workspace."

There is no workspace (`.devdata/databricks.env` absent). This sub-project is therefore finished against a **fake Databricks platform**: the same flow (register target, issue executor credential, start executor, plan, seal, approve as a second user, apply, observe, verify) is run from SETH's screens and by an end-to-end test, with the executor pointed at the fake. The Databricks integration stays `experimental` (`live_validated: false`); STATUS.md records "live validation waiting on owner input". A live test file exists and runs only when `.devdata/databricks.env` is present; it is never counted as passing otherwise.

In scope: `adapters.native` (Databricks only), `delivery`, `shifde-executor`, their tables, gateway, screens and tests; the executor rule and the one-platform-per-organization rule of 5.3 proven by test. Out of scope: Snowflake, Fabric, Palantir adapters (sub-projects 6, 8); releases, digests from `packaging`, evaluation gate (sub-project 3; until then the deployable artifact is the SDK template built by `delivery`); generated apps (sub-project 5).

## 2. Modules and layers

| Module | Layer (import-linter `layers` in `backend/pyproject.toml`) | Contents |
|---|---|---|
| `shifde.adapters.native` | existing `shifde.adapters` layer (no change to the list) | `contracts.py` (pydantic wire models shared by API and executor: target, plan, plan step, artifact, receipt, observation, verification report, capability report, journal checkpoint), `base.py` (the ten-operation protocol), `databricks/adapter.py`, `databricks/clients.py`, `databricks/fake.py`, `databricks/templates/` (package data) |
| `shifde.delivery` | new layer directly above `shifde.runtime`, below `shifde.app \| shifde.cli` | `models`/`repository`/`service`/`api` (targets, executors, artifacts, plans, approvals, deployments, journal, native resources), `gateway_api.py` (executor routes), `gateway.py` (lease/fence logic) |
| `shifde.executor` | top layer, beside `shifde.main` (`"shifde.main \| shifde.executor"`) | `main.py` (`shifde-executor` entry point), `client.py` (HTTPS client of the gateway), `credentials.py` (local credential broker), `runner.py` (runs one job through the adapter) |

Contract changes (made in issue 2 or 4, which first need them): add `shifde.delivery` to the layer list and to the "api -> service -> repository -> models" containers; add its service/repository/gateway modules to the "services stay HTTP-free" list; new forbidden contract "the executor never reaches the database": `shifde.executor` and `shifde.adapters.native` may not import `sqlalchemy`, `psycopg`, `alembic`, `shifde.core.db`, `shifde.delivery`, or any module above `adapters` (indirect imports included, so a stray `shifde.core.*` import that pulls in the database fails the gate). `delivery` does not import `packaging`, `evaluation`, `appgen` or `assist` (they do not exist yet; later they sit below `delivery`).

**Ported from the SDK (commit `1fd902b`), rewritten synchronous to match SETH (sync SQLAlchemy, sync httpx, `subprocess.run`):**
- `packages/adapters/databricks/.../adapter.py`, `clients.py`, `templates/*` -> `adapters/native/databricks/`. Keeps: pinned CLI version check, minimal CLI environment, `databricks bundle deploy` then `bundle run`, refusal of application-controlled bundle hooks, refusal to adopt an existing app by name, health origin check (`*.databricksapps.com`, https, no path), observe logic (pending vs active deployment, `CRASHED`), reconcile always `ambiguous`, verify = runtime check + warehouse health.
- `packages/contracts/{plans,execution,builds,capabilities,checkpoints,jobs,common}.py`, `platform_specs/databricks.py` -> `adapters/native/contracts.py` (Databricks fields only).
- `packages/core/{journal,planning,policy,hashing,native}.py` -> journal semantics (intent before write, input digest, conflicting receipt refused, `NeedsAttention`) and the default policy (no `delete`/`unknown` steps) into `delivery`; canonical hashing into `adapters/native/contracts.py`.
- `packages/hosted/{gateway,executor,job_types}.py`, schema v1/v2 -> `delivery` gateway (lease ids, fences, idempotent lease requests, checkpoint sequence, credential grant expiring at `min(lease_until, now+30 s)`, outcome on lease loss).
- `executors/src/shifde_executor/{client,runner,credentials,supervisor}.py` -> `shifde.executor`.
- `tests/adapters/test_databricks.py` `FakeWorkspace` + `FakeCLI` -> `adapters/native/databricks/fake.py`, extended to a stateful fake platform with fault switches: `fail_after_accept`, `crash_app`, `no_new_deployment`, `health_bad`, `hang`. All 16 SDK Databricks tests are ported.

**Dropped:** Temporal and its workflow; the file-based `StepJournal` and `LocalArtifactStore` (replaced by gateway checkpoints and database artifacts); hosted `auth.py` JWT/JWKS (replaced by executor credentials, section 5); `outbox`, `idempotency_keys`, `resources`/`resource_links` generic tables, `memberships`, `worker_pools` (one executor identity per org replaces pools); per-action container sandbox and image-signature checks (recorded as a go-live item); Snowflake/Fabric/Palantir code and receipts; the TypeScript SDK; `databricks-sdk` as a dependency (the three REST reads it served, `apps.get`, `apps.list`, `warehouses.get`, become plain httpx calls; the CLI stays the only write path).

## 3. Tables

All client-owned (forced RLS, `tenant_isolation` on `tenant_id = current_tenant_id()`, composite `(tenant_id, ...)` foreign keys, `shifde_app` gets column-wise grants only). **No new global table**, so `tests/schema/global_tables.txt` (a protected path) is untouched.

| Table | Key columns |
|---|---|
| `delivery_platform_claims` | `tenant_id`, `id`, `platform_kind` (`databricks`), `address_key` (sha256 hex of the normalized address), `created_by`, `created_at`. **UNIQUE (`platform_kind`, `address_key`)** across all rows |
| `delivery_targets` | `tenant_id`, `id`, `workspace_id`, `project_id` (nullable), `name`, `platform_kind`, `address` (normalized URL), `claim_id`, `environment` (`dev`/`test`/`prod`), `bindings` jsonb (`warehouse` id), `credential_ref` (a name, never a secret), `status` (`active`/`archived`), `revision` |
| `delivery_executors` | `tenant_id`, `id`, `name`, `status` (`active`/`disabled`), `last_seen_at`, `version`; partial unique index: one `active` executor per `tenant_id` |
| `delivery_executor_credentials` | `tenant_id`, `id`, `executor_id`, `prefix` (unique), `secret_hash` (peppered, as `identity.apikeys`), `created_by`, `expires_at`, `revoked_at` |
| `delivery_artifacts` | `tenant_id`, `id`, `target_kind` (`databricks.app/v1`), `digest`, `size`, `content` bytea (template archive, cap 5 MB), `created_by` |
| `delivery_plans` | `tenant_id`, `id`, `target_id`, `artifact_id`, `artifact_digest`, `application_key`, `status`, `requested_by`, `steps` jsonb, `snapshot` jsonb, `adapter_version`, `toolchain_digest`, `digest`, `sealed_at`, `expires_at` |
| `delivery_approvals` | `tenant_id`, `id`, `plan_id`, `plan_digest`, `decided_by`, `decision` (`approve`/`reject`), `note`, `decided_at`; UNIQUE (`plan_id`) |
| `delivery_deployments` | `tenant_id`, `id`, `plan_id` (unique), `target_id`, `application_key`, `status`, `status_reason`, `receipt` jsonb, `verification` jsonb, `observe_deadline`, `reconcile_evidence` jsonb, `resolved_by`, `resolved_note`, `resolved_at`, timestamps |
| `delivery_jobs` | `tenant_id`, `id`, `kind` (`plan`/`apply`/`observe`/`verify`/`reconcile`), `plan_id`, `deployment_id`, `status` (`queued`/`leased`/`done`/`lost`), `run_after`, `deadline`, `attempts`, `max_attempts` (1 for `apply`), `executor_id`, `lease_id`, `lease_request_id`, `fence`, `lease_until`, `heartbeat_at`, `result` jsonb |
| `delivery_journal` | `tenant_id`, `deployment_id`, `step_key`, `input_digest`, `state` (`intent`/`complete`), `receipt` jsonb, `job_id`, `fence`, `recorded_at`; PK (`tenant_id`, `deployment_id`, `step_key`) |
| `delivery_native_resources` | `tenant_id`, `target_id`, `application_key`, `resource_type`, `native_id`, `deployment_id`; UNIQUE (`tenant_id`, `target_id`, `resource_type`, `native_id`) — ownership, so a later deployment cannot adopt an app it did not create |

**Migration branch** `delivery` (label `("delivery",)`, `down_revision = "base_0001"`, `depends_on` tenancy, identity, authz heads): `delivery_0001_targets` (claims, targets, permission keys and role grants), `delivery_0002_executors` (executors, credentials, definer `shifde.delivery_resolve_executor(prefix, hash)`), `delivery_0003_queue` (jobs, journal, definer `shifde.delivery_lease_job(executor_id, request_id, kinds)`, which takes the organization from `current_tenant_id()`, never from an argument (B-2670)), `delivery_0004_plans` (artifacts, plans, approvals, and the `(tenant_id, plan_id)` key from jobs to plans, B-2673), `delivery_0005_deployments` (deployments, native resources, the deployment keys from jobs/journal added here). Each has a downgrade.

**One platform per organization.** `delivery_service.register_target` normalizes the URL (https only, lowercase host, default port dropped, path/query/fragment refused, `*.cloud.databricks.com` / `*.azuredatabricks.net` / `*.gcp.databricks.com` only), hashes it, and inserts a `delivery_platform_claims` row in the caller's organization (or reuses its own). The unique index is checked by PostgreSQL across every organization's rows regardless of RLS, so a second organization gets a unique violation on `uq_delivery_platform_claims__address`; the service maps it to `409 platform_already_registered`, "This platform address is already registered as a deployment target." The same message is used when the caller's own organization holds it in an archived target; the constraint DETAIL is never passed on. Archiving the last target on a claim deletes the claim. This is the SDK's `managed_namespaces` mechanism, with a hash so the address never sits in an index another organization's error could echo.

## 4. Deployment state machine, journal, unknown outcome

```
plan:        requested -> planning -> sealed --(approve by 2nd person)--> approved
                             |          |--(reject)--> rejected
                             |          `--(now >= expires_at)--> expired
                             `--(plan job fails / lost)--> plan_failed
deployment:  queued -> leased -> applying -> observing -> verifying -> verified
             queued -> expired (plan expired before lease) | cancelled (only before lease)
             leased/applying -> failed (no intent recorded) | unknown_outcome (intent, no complete)
             observing/verifying -> failed (platform says FAILED/CRASHED, deadline passed, verify fails)
             unknown_outcome -> resolved_failed (person, audited, with note)
```

- **Plan.** FDE requests a plan for (target, artifact): service builds the template artifact (offline, no credential), queues a `plan` job. Executor inspects the workspace and returns steps + snapshot; the service applies the default policy (no `delete`/`unknown` steps), computes the plan digest over (artifact digest, target revision, snapshot fingerprint, steps, adapter and toolchain versions) and seals it: `sealed_at = now()`, `expires_at = sealed_at + 15 min`. Sealed plans are immutable (trigger).
- **Approval.** `delivery:plan:approve` permission; approver must differ from `requested_by` (enforced in service and by a `CHECK`-trigger on `delivery_approvals`); approval stores the plan digest and must land before `expires_at`. Approval creates the deployment and its `apply` job in one transaction. All approvals and rejections are audited.
- **Apply (written once).** The lease function refuses an `apply` job whose plan has expired (deployment -> `expired`) or whose target has an unresolved `unknown_outcome`. `max_attempts = 1`; an `apply` job is never re-queued. Before each platform write (`bundle-deploy`, `bundle-run`) the executor posts an `intent` checkpoint and writes only after the gateway acknowledges it under a valid lease and fence; after the write it posts `complete` with the receipt.
- **C1-style unknown outcome.** If the lease expires, heartbeat stops, the executor reports an error, or a fence is stale while any journal step is `intent` without `complete`, the deployment becomes `unknown_outcome`, never `failed`, `cancelled` or healthy. With no intent recorded it becomes `failed` ("no platform write was attempted"). The sweep runs inside `delivery_lease_job` (every executor poll for that organization) and when a deployment is read; no worker change is needed. A target with an `unknown_outcome` deployment refuses new plans until a person runs **Reconcile** (read-only job; result is shown as evidence, always `ambiguous` for Databricks) and **Mark as failed** (audited, note required). Cancel is refused once a job has been leased.
- **Observe and verify** are read-only jobs, re-queued with `run_after = now() + 15 s` until `observing` reaches `succeeded` or the 20-minute deadline (then `failed`, "did not become healthy"); they may be retried up to 3 times on lease loss. `verified` needs both checks of the ported verify (`native-runtime`, `runtime-warehouse`) to pass.

Test proof (design 9): worker-crash-style tests kill the executor (stop heartbeating) between intent and complete, after complete, and before intent, and assert the three outcomes above; a fourth asserts a stale-fence executor cannot checkpoint.

## 5. Executor gateway, authentication, credential

Routes under `/api/executor/v1` (separate router, not behind `require_auth`; user tokens are refused there and executor tokens everywhere else):

| Route | Does |
|---|---|
| `POST /lease` `{request_id, kinds}` | Leases at most one due job of the caller's organization (`FOR UPDATE SKIP LOCKED`, `run_after <= now()`); returns job, `lease_id`, `fence`, `lease_until` (45 s); same `request_id` returns the same lease; 204 if none |
| `POST /jobs/{id}/heartbeat` `{lease_id, fence}` | Extends the lease; 409 if lost |
| `GET /jobs/{id}/artifact` | Artifact bytes for that job, digest in a header; executor verifies |
| `POST /jobs/{id}/credential-grant` | Grant `{job_id, executor_id, target_id, credential_ref, audience = target address, expires_at = min(lease_until, now+30 s)}`; no secret |
| `POST /jobs/{id}/checkpoint` `{lease_id, fence, sequence, step_key, state, input_digest, receipt?}` | Journal write; refused on stale lease/fence, changed input digest, conflicting receipt or cancel requested |
| `POST /jobs/{id}/result` `{lease_id, fence, status, value or error_code}` | Terminal; validated against the result model for the kind |

**Authentication.** An organization owner or a holder of `delivery:executor:manage` creates the executor identity on the Executors screen and issues a credential, shown once (`shx_<prefix>_<secret>`, stored as a peppered hash). The executor sends it as a bearer token over HTTPS. `delivery_resolve_executor` (SECURITY DEFINER, like `identity_0003`'s API-key resolver) returns `(tenant_id, executor_id)` only for an active executor and an unexpired, unrevoked credential; the request then runs with that organization set and every query is RLS-scoped, so a job id from another organization is a plain 404. Rotation: issue a second credential (at most two live), restart the executor with it, revoke the old one; credentials expire after 90 days. Disabling the executor revokes all at once. All issue/rotate/revoke events are audited.

**Deployment credential.** It never enters SETH. The target stores only `credential_ref`; the executor's local file (`SHIFDE_EXECUTOR_CREDENTIALS_FILE`, mode 600, outside the repo) maps `credential_ref -> {host, token}`. The broker (ported `credentials.py`) uses it only when the grant matches the job, the executor, the target and `audience == host`, and has not expired; the token is passed to the CLI through its environment and to httpx in a header, never logged or returned. An executor holds only its own organization's file and can lease only its organization's jobs, so it can neither receive nor use another organization's credential. Executor config also carries `SHIFDE_EXECUTOR_API_URL` (https required outside dev) and `SHIFDE_EXECUTOR_TOKEN`.

Fake mode: `shifde-executor --fake-platform` swaps in `adapters/native/databricks/fake.py`; refused unless `SHIFDE_ENV` is `dev` or `test`; every receipt it produces carries `platform: fake`, and the deployment screen shows a "fake platform" badge.

## 6. Front-end screens

Under `frontend/src/app/(app)/o/[org]/deliver/`, with Vitest tests and nav entry "Deliver":
- `targets` (list, register, archive; shows the claim conflict message; `experimental` badge on the Databricks kind),
- `executors` (identity, last seen, version; issue / rotate / revoke credential, one-time reveal),
- `plans` and `plans/[id]` (request plan, steps, digest, expiry countdown, status),
- `approvals` (sealed plans awaiting a second person; approve/reject; own plans shown but disabled with the reason),
- `deployments` and `deployments/[id]` (status, journal timeline, receipt, verification checks, unknown-outcome banner with Reconcile and Mark as failed).

## 7. Factory issues (in order)

No issue changes a protected path (`factory/*`, `.claude/*`, existing migrations, `backend/tests/schema/*`); migrations are new files. Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`; listed below are the issue-specific commands. **C** = isolation-critical (`module-builder-critical`, isolation review).

1. **adapters.native: contracts, Databricks adapter, fake platform.** Port per section 2, sync; 16 SDK tests + fault-switch tests; executor-import forbidden contract. Migration: no. Checks: `uv run pytest tests/adapters/native`. Depends: none.
2. **delivery: targets and the one-platform rule (C).** Layer contract entries, `delivery_0001`, register/list/get/archive routes, normalization, claim, permissions. Migration: yes. Checks: `uv run pytest tests/delivery/test_targets.py tests/delivery/test_platform_claims.py tests/delivery/test_routes_cross_client.py tests/schema`. Depends: 1.
3. **delivery: executor identities and credentials (C).** `delivery_0002`, resolver definer, issue/rotate/revoke routes, executor auth dependency, user/executor token separation. Migration: yes. Checks: `uv run pytest tests/delivery/test_executors.py tests/delivery/test_executor_auth.py tests/delivery/test_routes_cross_client.py tests/schema`. Depends: 2.
4. **delivery: job queue, gateway and journal (C).** `delivery_0003`, lease definer, six gateway routes, fences, journal rules, lease-loss sweep, using a test-only job kind. Migration: yes. Checks: `uv run pytest tests/delivery/test_gateway.py tests/delivery/test_journal.py tests/delivery/test_gateway_cross_client.py tests/schema`. Depends: 3.
5. **delivery: artifacts, plans, sealing, second-person approval (C).** `delivery_0004`, template artifact build, plan job handling, digest, 15-min expiry, approve/reject. Migration: yes. Checks: `uv run pytest tests/delivery/test_plans.py tests/delivery/test_approvals.py tests/delivery/test_routes_cross_client.py tests/schema`. Depends: 1, 4.
6. **delivery: deployments, unknown outcome, reconcile (C).** `delivery_0005`, apply/observe/verify handling, native-resource ownership, state machine, Reconcile / Mark as failed, the four crash tests. Migration: yes. Checks: `uv run pytest tests/delivery/test_deployments.py tests/delivery/test_unknown_outcome.py tests/delivery/test_routes_cross_client.py tests/schema`. Depends: 5.
7. **shifde-executor process and end-to-end against the fake (C).** Entry point, client, broker, runner, `--fake-platform`; e2e test runs the API in-process and the executor loop against the fake: plan, approve as a second user, apply, observe, verify, plus organization B's executor leasing nothing of A's. Migration: no. Checks: `uv run pytest tests/executor tests/delivery/test_end_to_end.py; uv run shifde-executor --help`. Depends: 6.
8. **Front end: targets and executors screens.** Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`. Depends: 3.
9. **Front end: plans, approvals and deployments screens.** Checks: as 8. Depends: 6, 8.
10. **Live-validation harness and runbook.** `tests/adapters/native/test_databricks_live.py` (runs only with `.devdata/databricks.env`; reads it itself), `docs/runbooks/databricks-live-validation.md`, `scripts/delivery_demo.py` (drives the fake flow for the done-when check). Migration: no. Checks: `uv run pytest tests/adapters/native -k live` (reports "not run: no workspace"); `python scripts/delivery_demo.py --fake`. Depends: 7, 9.

## 8. Open questions (with recommended answers)

1. Same `shifde.jobs` table as the worker, or a separate `delivery_jobs`? **Separate table, same mechanics** (`run_after`, `SKIP LOCKED`, leases). Sharing `jobs` would mean changing runtime's claim and reaper functions (the reaper re-queues expired leases, which would retry a write) and altering a schema-compared table. It is still one queue design in one database, not a second application's queue.
2. Does the deployment credential live in SETH (encrypted, handed to the executor per job) or only at the executor? **Only at the executor** (SDK model); SETH stores `credential_ref`. Design 5.3 says only the executor ever holds one.
3. Executor auth: SDK-style JWT/JWKS or SETH-issued bearer tokens? **SETH-issued, hashed, rotatable tokens**; no issuer to run, mirrors API keys.
4. One executor per organization strictly, or several instances? **One active identity per organization, at most two live credentials**; scale-out later.
5. Port the adapter async (SDK) or sync (SETH)? **Sync.**
6. Keep `databricks-sdk` as a dependency? **No**; three REST reads via httpx, CLI pinned at the SDK's version for writes.
7. Where does the fake platform live? **In `src` (`adapters/native/databricks/fake.py`)** so the screens can be demonstrated; refused outside `dev`/`test` and badged.
8. Which process runs observe/verify? **The executor** (needs the credential); the worker is untouched. Design 5.1's "deployment timers" become `run_after` on delivery jobs.
9. Where does build run? **In the API** (offline template, no credential); the executor only verifies the digest.
10. Multiple targets on one workspace inside one organization? **Yes** (claim table separate from targets); across organizations, never.
11. Who may approve? **Anyone with `delivery:plan:approve` other than the requester**; owners and admins get it in the role grants, FDEs do not by default.
12. Plan expiry clock: from sealing, and must the apply be leased before expiry? **Yes to both**; an approved plan whose executor was offline past expiry becomes `expired` with no write.
13. Can a person mark an unknown outcome verified? **No**; only `resolved_failed`, then a new plan.
14. Observe interval and deadline? **15 s, 20 min.**
15. Executor container sandbox? **Dropped for now**; listed as a go-live item.
16. Domain allow-list for Databricks hosts? **AWS, Azure and GCP Databricks domains only**; others refused.
17. Front-end route name? **`/o/[org]/deliver/...`**, separate from the run `approvals` page.

## 9. Settled calls (lead, 2026-11-17; DECISIONS B-2500, B-2501)

Every recommended answer in section 8 is accepted as written (B-2500). Q1 departs from the wording of the unified design 5.1 ("a job in SETH's queue"): `delivery_jobs` is a separate table with the same queue mechanics (`FOR UPDATE SKIP LOCKED`, `run_after`, leases), so runtime's reaper can never re-queue a client-platform write; the intent of 5.1 (no Temporal, one PostgreSQL queue mechanism, no second queue system) holds.

A fresh builder read this design and listed what it would have to guess. Settled (B-2501); these bind every issue in section 7:

1. Permissions: `delivery:target:read|write`, `delivery:plan:read|request|approve`, `delivery:deployment:read|resolve`, `delivery:executor:read|manage`. Owners and admins get all; FDEs get read, `target:write`, `plan:request`; nobody approves their own plan.
2. User routes: REST under `/api/v1/orgs/{org}/delivery/...` following the existing org-scoped route pattern (targets, executors, `executors/{id}/credentials`, plans, `plans/{id}/approve|reject`, deployments, `deployments/{id}/reconcile|mark-failed|cancel`). Each route gets a cross-client test (A's user asks for B's object: 404).
3. Every table has `created_at`, `updated_at`; targets `created_by`; plans `status_reason`.
4. Status CHECK values are exactly the state machine's names in section 4; `delivery_jobs.status` also allows `cancelled`.
5. Gateway result handlers (delivery service) do the follow-on work in the same transaction as completing the job: seal a plan after the plan job; derive deployment state from checkpoints and results (first intent checkpoint means `applying`; apply result means `observing`; observe success means `verifying`; verify result means `verified`/`failed`); enqueue the next job with `run_after = now() + 15 s`.
6. Each observe poll is a new job row. The 20-minute observe deadline counts from entering `observing`, is stored on the deployment and copied to each observe job.
7. Cancel is allowed only while the apply job is queued (not leased). The "checkpoint refused on cancel" clause is dropped.
8. Lease loss: during **apply**, it is `unknown_outcome` (C1 rule, never retried, never cancelled). During plan/observe/verify (read-only), the job is re-queued up to 3 times, then the deployment is `failed` ("executor lost").
9. The lease-expiry sweep is a SECURITY INVOKER function run for one organization on every lease call and on every deployment read, so a dead executor still surfaces `unknown_outcome` on read.
10. A target with an unresolved `unknown_outcome` refuses new plan requests (409 `target_unresolved`); an already queued apply on it is not leased and shows "blocked by unresolved unknown outcome".
11. Reconcile stores its evidence in `delivery_deployments.reconcile_evidence jsonb` and does not change status; mark-failed needs a note, not a prior reconcile.
12. Executors: `last_seen_at` is updated on every gateway call; `version` comes from the `X-Executor-Version` header. Disable is `PATCH executors/{id}` (status), which revokes its tokens. A second active executor in one organization is a 409.
13. Gateway errors: a stale lease or sequence is 409; an executor token on a user route, or a user token on the gateway, is 401. Checkpoint `sequence` is monotonic per job (`delivery_jobs.checkpoint_seq`); `<=` current is refused. A lease `request_id` replayed after the job is done returns 204.
14. The credential grant audience is the target's normalized address; the executor compares it with its credential file's host using the same normalizer as target registration (in `adapters.native`).
15. Executor tokens use the `identity` API-key hashing helper (same pepper source); revoke needs `delivery:executor:manage`.
16. Gateway wire models live in `adapters/native/contracts.py`; `delivery` and the executor both import them, and the executor imports nothing from `delivery`, `core.db` or any database module (import-linter contract added in issue 1, layer entry for `delivery` in issue 2).
17. Plan request body: `{target_id, application_key, display_name}`; `application_key` is a slug unique per target. Target bindings: `{"warehouse_id": "<id>"}`.
18. Native-resource ownership: the plan job reports existing app names; the service refuses a plan that would adopt an app not owned by this target. A row in `delivery_native_resources` is written when the apply result reports the deployed app.
19. `scripts/delivery_demo.py --fake` runs in-process (TestClient, executor against the fake platform) with the local cluster running. The live test is `pytest -m live`, skipped with reason "live validation waiting on owner input: no Databricks workspace" when `.devdata/databricks.env` is absent.
