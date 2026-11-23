# Sub-project 8: MCP access; Fabric; Palantir; standalone Docker target — short design

Date: 2026-11-23. Status: approved (lead, under charter 3); settled after builder read (B-3923, section 10, which overrides earlier text where they differ). Contract: unified design sections 1, 3 ("access for outside tools over MCP is a later module"), 5.1, 5.2 (`packaging` "also holds the standalone Docker target", `adapters.native`), 5.3, 5.5, 5.6, 6 (row 8: "Decided when reached"), 9; CHARTER sections 2, 3 and 7 (last paragraph). Builds on SP2 (`delivery`, executor, claims, journal), SP3 (releases, evaluation gate, API keys), SP5 (`appgen`, the generated Databricks app; "SP5 6.4" and so on), SP6 (second target kind, `delivery_0007`, `packaging_0004`) and SP7 (`assist`, `board`).

The owner is unavailable, so the lead decides the scope (charter 3). Charter 2 names development targets for Databricks, Snowflake and Entra only. No Fabric workspace, Palantir Foundry stack or Docker host is supplied. So every part that needs one of those is built against a fake, stays `experimental` (`live_validated: false`), and STATUS records "live validation waiting on owner input". Nothing is deployed to or written on any platform outside charter 2. A Docker daemon on this development machine is only a local test harness: no demo needs it, and running it proves nothing about a client host.

## 1. Scope decision

Each part is its own "done when". Sub-project 8 is done when all four are met and STATUS lists each kind's live-validation line.

| Part | What it is | Done when (concrete checks) | Stays |
|---|---|---|---|
| A. MCP access | SETH as an MCP **server** for outside tools (Claude Code, IDEs, scripts). Per client, off by default, bearer API keys only, audited, rate- and cost-capped | 1. `python scripts/mcp_demo.py --fake` ends `MCP DEMO PASSED` (exit 0) or `MCP DEMO FAILED: <step>` (1). 2. `cd backend; uv run pytest tests/mcp_access` passes, cross-client tests included | Not vendor-bound. No live test. `validated` needs no owner input |
| B. Fabric | Native kind `fabric.solution/v1`: the SDK's Lakehouse + Notebook template, carrying `release.json`, deployed to a Fabric workspace through plan, second-person approval, apply, observe and verify | 1. `python scripts/fabric_demo.py --fake` ends `FABRIC DEMO PASSED`. 2. `cd backend; uv run pytest tests/adapters/native/test_fabric_live.py -m live` skips with "live validation waiting on owner input: no Fabric workspace" | `experimental` |
| C. Palantir | Native kind `palantir.marketplace/v1`: installs an approved, already-published Foundry Marketplace product version through the same flow; SETH records which release authorized it | 1. `python scripts/palantir_demo.py --fake` ends `PALANTIR DEMO PASSED` and shows verify's `content-check: not_run` line. 2. `uv run pytest tests/adapters/native/test_palantir_live.py -m live` skips with "live validation waiting on owner input: no Palantir Foundry stack" | `experimental` |
| D. Standalone Docker | Native kind `docker.compose/v1`: SP5's generated Databricks app, run as a container on a client-owned host by that client's executor; it reads the client's Databricks workspace | 1. `python scripts/docker_demo.py --fake` ends `DOCKER DEMO PASSED`, using the fake container runtime (no Docker daemon). 2. `uv run pytest tests/adapters/native/test_docker_live.py -m live` skips with "live validation waiting on owner input: no Docker host". 3. Optional local harness `uv run pytest tests/adapters/native/test_docker_local.py -m docker`, which needs `SHIFDE_TEST_DOCKER=1` and a local daemon. It never marks anything validated | `experimental` |

**Out of scope:**
- `fabric.iq/v1` (Fabric ontology items, a preview feature);
- generated apps on Fabric or Palantir. Both take template or pre-published content only (Calls S8-G, S8-H);
- a Fabric data connection: `FabricPlanned` stays `planned`;
- `palantir.superrepo/v1`. Seal refuses it by name, because it needs an enrollment CLI that does not exist here;
- a Docker app over Snowflake (the Snowflake backend needs Snowpark inside Snowflake);
- remote Docker hosts (`DOCKER_HOST` set to `tcp://` or `ssh://`), Kubernetes and image registries;
- MCP tools that approve, apply, seal, manage secrets or keys, invoke a connection or run raw SQL (Call S8-D);
- an OAuth 2.1 authorization server or dynamic client registration for MCP (Call S8-B);
- deleting, uninstalling or rolling back anything on a platform (as in SP2).

## 2. Shared pieces (SP8-1)

- **Kinds.** `delivery_targets.platform_kind` and `delivery_platform_claims.platform_kind` gain `fabric`, `palantir` and `docker`. `delivery_artifacts.target_kind` and `solution_versions.target_kind` gain `fabric.solution/v1`, `palantir.marketplace/v1` and `docker.compose/v1`. `packaging.TARGET_KINDS` and `delivery` dispatch on the kind, as SP6 4 does.
- **Migrations** (one pair for all three kinds, Call S8-M). `delivery_0008_sp8_kinds` and `packaging_0005_sp8_target_kinds` widen the CHECK constraints only. Each downgrade refuses (raises) while a row of a new kind exists. There is no new delivery table. Bindings stay jsonb and are validated per platform by a discriminated union in `delivery.schemas`.
- **Addresses** (`adapters/native/addresses.py`, keyed by platform):
  - Fabric: a workspace GUID, or `https://app.fabric.microsoft.com/groups/<guid>[/...]`, normalizes to `https://api.fabric.microsoft.com/v1/workspaces/<lowercase guid>`;
  - Palantir: `https://<stack>.palantirfoundry.com` (https, lowercase, no port, path or query), plus `SHIFDE_NATIVE_EXTRA_HOST_SUFFIXES`, as for Databricks;
  - Docker: `docker://<daemon id>`, where the daemon id is the output of `docker info --format '{{.ID}}'` on the client host (Call S8-J).
  - Claims, `platform_already_registered` (409) and its message are SP2's, unchanged.
- **Packaging compatibility per kind.**
  - `fabric.solution/v1` and `palantir.marketplace/v1` are compiler `/1` only. No agent code ships, so SP3's evaluation gate is the release check, and a solution with screens is refused at seal with the existing kind-check code (S6-H pattern).
  - `docker.compose/v1` is `/2` only and applies the Databricks `/2` rules unchanged: Databricks connection binding, read-only `connection.sql` tools, one model binding.
  - `palantir.superrepo/v1` and `fabric.iq/v1` are named in the unsupported report.
- **Executor.**
  - `runner` keeps a dispatch table from `platform_kind` to adapter. `--fake-platform` wires all fakes.
  - A credentials-file entry's `platform` gains `fabric`, `palantir` and `docker`, with the per-grant `tenant_id`, `host` and `platform` checks of SP6 6.4.
  - Fabric: `{entra_tenant_id, client_id, client_secret_file}`. Palantir: `{client_id, client_secret_file}`. Docker: no secret (`{host: "docker://<id>"}`; the daemon is local to the executor).
  - Secret files are read per grant. Their contents are never logged and never sent to SETH.
- **Import-linter.** `adapters.native.{fabric,palantir,docker}` fall under the existing "the executor never reaches the database" contract. `appgen.render_docker` falls under "appgen.render is pure".

## 3. Part A: MCP access

### 3.1 Shape and module

- New module **`shifde.mcp_access`** in the top layer, beside `app`. Only `shifde.app` imports it. Import-linter forbids it from importing any `shifde.*.repository` module: every tool calls the owning service with the caller's `TenantScope`, exactly as a route does.
- **Transport (Call S8-A).** MCP over streamable HTTP, mounted at `/mcp` in the API process with the `mcp` SDK already pinned (`mcp>=1.12,<2`, locked 1.30): `FastMCP(stateless_http=True, json_response=True)`.
  - There is no stdio server, no SSE-only transport, no separate process and no in-memory MCP session, so any API process serves any call.
  - DNS-rebinding protection is on. Allowed `Host` values come from `SHIFDE_MCP_ALLOWED_HOSTS`, and a request carrying any `Origin` header is refused (MCP clients are not browsers).
  - Requests are capped at 256 KiB, and a tool result above 256 KiB is truncated with `truncated: true`.
  - Each sync service call runs in `anyio.to_thread.run_sync` on its own database session, with the client set from the key.
- Installation switch: `SHIFDE_MCP_ENABLED` (default `false`). When it is off, `/mcp` is not mounted. `config preflight` prints one `ok:`/`refused:` line for it (refused when it is on and `SHIFDE_MCP_ALLOWED_HOSTS` is empty).
- SETH's existing `mcp_http` connection adapter (SETH as an MCP **client** of vendor servers) is unchanged and unrelated.

### 3.2 Who can call (Calls S8-B, S8-C)

1. **Bearer `shf_` API keys only** (`identity.apikeys`, SECURITY_MODEL 2.4): user keys and service-account keys. Session cookies, executor `shx_` credentials and worker principals are refused with 401. Failed keys count toward the existing `api_key_failure_limiter`.
2. **New permission `mcp:access`.** The key's permission mask must include it, and the principal must hold it at organization level. Effective rights are the principal's grants ∩ the key mask ∩ the key's `resource_scope`, as for every key.
3. **Per-client switch.** `mcp_client_settings.enabled` defaults to false. While it is false, every call from that client's keys gets `mcp_disabled` (403), audited in that client. Turning it on or off needs `mcp:settings:write`. The change is audited, and the client overview shows it to the client's owners, following the SHI-provider pattern of 5.3.
4. A platform administrator gets no MCP reach: a key belongs to one client's principal, and no MCP tool takes an organization argument.

### 3.3 Tools (v1, Call S8-D)

Each tool needs `mcp:access` plus the named permission. It is also listed in the client's `allowed_tools`; the default is every tool except `start_run` and `assist_ask`, which an administrator ticks. Ids in arguments resolve only in the key's client and resource scope. Anything else is `not_found` with the same text as an unknown id.

| Tool | Service call | Permission |
|---|---|---|
| `whoami` | key name, client name, expiry, effective MCP tools | `mcp:access` |
| `list_projects` | `tenancy` projects visible to the principal | `project:read` |
| `list_agents` | published agent versions of a project | `agent:read` |
| `start_run` | `RunService.create` on a published agent version in a project. `max_cost` = `min(argument, settings.max_cost_per_call)`. Budget pre-flight and reservation as SP3 | `agent:execute` at the agent's scope |
| `get_run` | status, final output, cost, pending approvals as counts only | `run:read` |
| `list_releases`, `get_release` | `packaging` reads; the manifest is never returned with binding values | `packaging:solution:read` |
| `list_deployments`, `get_deployment` | `delivery` reads: state, checks and journal step names. No artifact, plan body or address | `delivery:deployment:read` |
| `board_get` (SP8-3) | SP7 `board` for a project | the board's own reads (`project:read`, `packaging:solution:read`, `delivery:*:read`) |
| `assist_ask`, `assist_get` (SP8-3) | SP7 request create and read. A proposal is accepted only in SETH's screens | `assist:request:write`, `assist:request:read` |

A run started over MCP that reaches a write tool's approval gate waits for a person in SETH's screens. No MCP tool approves, rejects or cancels an approval.

### 3.4 Limits, cost, audit (Calls S8-E, S8-F)

- **Rate.** A sliding window per key per API process (`core` `SlidingWindowLimiter`), at `settings.rate_per_minute` (default 60). The installation ceiling `SHIFDE_MCP_MAX_RATE_PER_MINUTE` defaults to 600. Over the limit the call gets `rate_limited` with `retry_after`. A second ceiling, per client, is 4x the key rate.
- **Cost.** Only `start_run` and `assist_ask` spend. They go through the ledger unchanged (budgets, per-call reservation, SHI-provider switch per client). `max_cost_per_call` defaults to USD 1.00, with a ceiling of 25.00.
- **Audit.** Every call writes one event in the key's client: `mcp.tool_called` with `{tool, api_key_id, outcome, error_code, duration_ms, run_id?}`. Refusals write `mcp.access_refused` with `{reason: disabled|scope|permission|rate|tool_not_allowed}`. Arguments and results are never written to audit or logs. A key whose secret does not verify follows the existing installation-level rule.

### 3.5 Data model and migration

`mcp_0001_access` (new branch `mcp`, depends on the then-current heads of `identity`, `authz` and `tenancy`; at `0701bf1` these are `identity_0007`, `authz_0002`, `tenancy_0007`):
- Table `mcp_client_settings`:
  - columns `tenant_id` (PK, FK `tenants`), `enabled bool default false`, `rate_per_minute int CHECK 1..600`, `max_cost_per_call numeric(12,4) CHECK > 0 AND <= 25`, `allowed_tools text[]` (CHECK each element in the closed tool list), `updated_by`, `updated_at`;
  - forced RLS with `tenant_isolation` (USING and WITH CHECK);
  - `shifde_app` may SELECT, INSERT and UPDATE; no DELETE.
- Permissions: `mcp:access`, `mcp:settings:read`, `mcp:settings:write` (privileged). `tenant_administrator` holds all three, `developer` holds `mcp:access` and `mcp:settings:read`, and no other role holds any.
- Rollback removes the grants and the permissions and drops the table.

### 3.6 API and screens

- `GET|PUT /api/v1/orgs/{org}/mcp/settings` (`mcp:settings:read|write`). A missing row reads as the defaults. Refresh `openapi.json`. `/mcp` is a mounted ASGI app and is not in OpenAPI.
- Screens (`frontend/src/app/(app)/o/[org]/settings/mcp`):
  - an enable switch with a confirm dialog, rate, max cost per call and the allowed-tools checklist;
  - the last 50 `mcp.*` audit events of this client;
  - an "endpoint" box with a client configuration snippet. The API key screen offers the `mcp:access` scope.

### 3.7 Isolation tests (`tests/mcp_access`, C)

1. A's key with B's run, release, deployment, project and agent ids: `not_found`, same text as an unknown id, and no B row read (`shifde_app` statement log).
2. B's settings are 404 to A on both routes. `shifde_app` selects no B row of `mcp_client_settings` from A's context (SQL level).
3. B disabled and A enabled: B's key gets `mcp_disabled`, A's works, and each refusal's audit row is in its own client only.
4. A key without `mcp:access` in its mask, or whose user lost the grant, gets 403. A cookie session on `/mcp` gets 401. A key scoped to project P1 gets `not_found` for P2's run.
5. A tool not in `allowed_tools`: `tool_not_allowed`. The rate limit trips at N+1.
6. A `start_run` whose budget is exhausted is refused by the ledger, and nothing is sent to a provider. With the SHI provider off for B, B's run cannot reach it.
7. A grep test: no audit or log record contains an argument value (sentinel string).
8. Schema test: `mcp_client_settings` is covered. If `backend/tests/schema/*` needs a line, the builder states it and the lead adds it (B-2530).

## 4. Part B: Fabric (`fabric.solution/v1`)

- **Port** from the SDK at `1fd902b`, rewritten sync with httpx as in SP2:
  - `packages/adapters/fabric/src/shifde_fabric/{adapter,clients,source}.py`, `packages/contracts/src/shifde_contracts/platform_specs/fabric.py` (`FabricSolutionSpec`, the item graph and its validation), `templates/fabric-solution/{lake.json,main.ipynb}`, `packages/core/src/shifde_core/fabric_receipts.py` (receipt shapes only), and `tests/adapters/{fabric_fake,test_fabric,test_fabric_clients}.py`;
  - new package `adapters/native/fabric/{adapter,clients,source,fake}.py` plus `templates/`;
  - dropped: `fabric.iq/v1`, Temporal, containers and pools.
- **Clients.** Fabric REST `https://api.fabric.microsoft.com/v1`, with long-running operations (`x-ms-operation-id`, `Location`, `Retry-After`, polled by observe). Tokens are Entra client credentials for `https://api.fabric.microsoft.com/.default`, minted in the executor. Egress is limited to `api.fabric.microsoft.com` and `login.microsoftonline.com`.
- **Artifact.** The SDK template (Lakehouse `lake.json`, Notebook `main.ipynb`) with `release.json`. The notebook's first cell is generated and is the only change to the template: it exits with the release digest (`mssparkutils.notebook.exit(<digest>)`). `source.py` validates that item definition paths do not overlap, that the item graph is acyclic, and that there are no hidden files and no files outside the items.
- **Bindings.** `FabricBindings {lakehouse_name, notebook_name: ^[A-Za-z][A-Za-z0-9_]{0,99}$}`, unique per claim (409 `binding_in_use`).
- **Steps** (journal checkpoints, SP2 4 unchanged): `lakehouse-metadata-submit/-complete`, `notebook-metadata-submit/-complete` (create), `notebook-definition-submit/-complete` (an update of an owned notebook), `fabric-verification-submit` (one `RunNotebook` job), terminal. There is no adoption: an item of the bound name that this target does not own refuses the plan (`foreign_item`). Reconcile is always `ambiguous`.
- **Verify:**
  - `native-items`: both items exist and are owned, and the notebook definition digest equals the artifact's;
  - `runtime-health`: the verification job reached `Completed` and its exit value equals the plan's `release_digest`.
- **Fake** `FakeFabricWorkspace`: stateful items, definitions, long-running operations and jobs. It "runs" the notebook by reading its first cell's digest. Fault switches: `fail_after_accept:<step>`, `foreign_item`, `lro_failed`, `job_failed`, `hang`. Receipts carry `platform: fake`.
- **Demo** (`scripts/fabric_demo.py --fake`, pattern of `delivery_demo.py`, scratch database `shifde_demo_<hex>`):
  1. Org A: a Fabric target; seal a `/1` release; a plan is refused `evaluation_required`, then the evaluation passes.
  2. Self-approval is 403; a second person approves; apply, observe and verify reach `verified`; a second release updates the notebook definition.
  3. Org B registering A's workspace gets 409 `platform_already_registered`, and B gets 404 on A's target, plan and deployment.
  4. B's executor leases nothing of A's.

## 5. Part C: Palantir (`palantir.marketplace/v1`)

- **Port** `packages/adapters/palantir/src/shifde_palantir/{adapter,clients,source}.py` (Marketplace only), `packages/contracts/src/shifde_contracts/platform_specs/palantir.py` (`FoundryMarketplaceSpec`), `packages/core/src/shifde_core/palantir_receipts.py` and `tests/adapters/{palantir_fake,test_palantir}.py` into `adapters/native/palantir/`. The SDK's rulings stand:
  - no Ontology override: any populated `ontologyRid` is refused at plan, apply and observe;
  - no mutating retry, implicit adoption, abort, delete or rollback, and no silent "latest" version;
  - each accepted RID is persisted (checkpoint) before the next request.
- **Clients.** Foundry REST v3 `/api/v3/platform/marketplaceInstallations` and `.../marketplaceInstallationJobs` with `preview=true`, plus the read-only `/api/v2/ontologies/...`. Tokens are OAuth2 client credentials (`/multipass/api/oauth2/token`), minted in the executor. Egress is limited to the target host. Paths are checked as in the SDK (`/api/` prefix, no `?`, `#` or `..`).
- **Bindings** (on the target, Call S8-H):
  - `{marketplace_rid, product_id, version_id (uuid), namespace_rid, parent_folder_rid, ontology_rid, object_type, frontend_origin, frontend_asset_sha256, allow_preview_apis: bool}`;
  - with `allow_preview_apis` false, a plan is refused 409 `palantir_preview_not_allowed`;
  - `version_id` names the operator-approved product version. Changing it is a target PATCH, audited.
- **Artifact.** A deployment descriptor `{spec, bindings digest, release_digest}` plus `release.json`. Nothing is built for the platform. The deployment row records the release that authorized the install. The product content is not derived from the release, and the plan screen says so.
- **Steps:** `palantir-installation-create`, `palantir-job-submit`, terminal. Observe reads the job state until `SUCCEEDED`/`FAILED`.
- **Verify:**
  - `native-version`: the installation is at the pinned `version_id`;
  - `ontology-read`: `object_type` is readable (one object, read-only);
  - `frontend-asset`: the sha256 of the asset at `frontend_origin` equals the binding.
- **Fake** `FakeFoundryStack` (installations, jobs, an ontology with one object type, a static asset). Faults: `fail_after_accept:<step>`, `job_failed`, `ontology_override`, `asset_mismatch`, `hang`. Receipts carry `platform: fake`.
- **Demo** (`scripts/palantir_demo.py --fake`): the Fabric demo's steps on the Palantir fake, plus `palantir_preview_not_allowed` and `asset_mismatch`, which gives `verify_failed` (not `verified`).

## 6. Part D: standalone Docker (`docker.compose/v1`)

- **What runs (Call S8-I).**
  - The image is SP5's rendered Databricks app, unchanged under `app/` (agent loop, Databricks backend, React bundle, deep health), served by `gunicorn` in a container on a client-owned host.
  - It reads the client's Databricks workspace through the SQL warehouse and the serving endpoint under an M2M service principal. `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` sit in an env file on the host (`/etc/shifde-apps/<project>.env`, mode 0600, written by hand per the runbook). That file is referenced by the compose file, never included in it, and is never in SETH or the artifact.
  - The app does not call SETH (design call 2).
- **Render** (`appgen/render_docker.py`, pure; generator version `shifde.appgen.docker/1.0`):
  - the Databricks render;
  - a `Dockerfile`: base `python:3.12-slim@sha256:<pinned>`, `pip install --require-hashes -r app/requirements-docker.txt`, uid 10001, `EXPOSE 8000`, a label `org.shifde.release_digest`;
  - a `compose.yaml`: one service; image `shifde-<project>:<digest12>`; `env_file` the host path; non-secret `environment` (`DATABRICKS_WAREHOUSE_ID`, `SERVING_ENDPOINT`) from the target bindings; ports `127.0.0.1:<port>:8000`; `read_only: true`; `tmpfs: /tmp`; `cap_drop: [ALL]`; `security_opt: [no-new-privileges:true]`; `restart: unless-stopped`; a healthcheck on `/health`;
  - `requirements-docker.txt` is SP5's lock plus `gunicorn==23.0.0`, hashed and compiled with SP5's command (Call S8-K). A golden test pins the Docker render and confirms the Databricks golden digest is unchanged.
- **Bindings.**
  - `DockerBindings {project: ^[a-z][a-z0-9-]{0,39}$, port: 1024..65535, workspace_host, warehouse_id, serving_endpoint}`;
  - `project` and `port` are unique per claim;
  - `workspace_host` must already be claimed by this organization as a `databricks` target address, else 409 `workspace_not_claimed`. A workspace claimed by another organization gets the same answer, which reveals nothing.
  - SP5 6.6's `serving_endpoint_mismatch` and `model_not_in_target_workspace` gates apply against `workspace_host`.
- **Runtime** (`adapters/native/docker/`):
  - a `ContainerRuntime` protocol (`daemon_id`, `build`, `image_labels`, `compose_up`, `compose_ps`, `http_get_local`) with two implementations;
  - `DockerCliRuntime`:
    - uses the host `docker` CLI (24 or later, compose v2), version-checked, with a minimal environment;
    - refuses a `DOCKER_HOST` that is not the local unix socket or named pipe, refuses any `--privileged`, host network or volume flag, and runs no compose hooks;
    - builds from the bundle in the job's temporary directory and pulls only the pinned base digest;
  - `FakeContainerRuntime`: in memory. Build checks that the `Dockerfile` and `compose.yaml` bytes equal the artifact's. "Up" serves the staged `app/` through Flask's test client with the SP5 Databricks workspace fake injected, so deep health runs the rendered code. Faults: `fail_after_accept:<step>`, `build_failed`, `unhealthy`, `daemon_mismatch`, `deep_health_bad`, `hang`.
- **Steps:**
  - `image-build`;
  - `image-verify` (the label equals the release digest);
  - `compose-up` (`docker compose -p <project> -f compose.yaml up -d --no-build`; the next release replaces the container);
  - terminal `container-ready` (healthcheck `healthy`).
  - Inspect refuses the plan with `docker_host_mismatch` when `daemon_id` differs from the target address, and with `foreign_project` when a compose project of the bound name carries no label of this target.
  - There is no `down` and no image prune.
- **Verify:**
  - `native-version`: the container is running and its label equals the plan's digest;
  - `runtime-health`: `GET http://127.0.0.1:<port>/health` from the executor host;
  - `app-deep-health`: SP5 6.6 rules on `/health?deep=1`.
- **Demo** (`scripts/docker_demo.py --fake`, no daemon):
  1. Org A has a Databricks target (claim) and a Docker target. The SP5 fixture flow runs to a sealed `/2` release, then its evaluation.
  2. A plan with the wrong endpoint is refused `serving_endpoint_mismatch`. A plan that would bind B's workspace is refused `workspace_not_claimed`.
  3. Approval by a second person, apply, `verified` with deep health from the staged code, and a second release replacing the container.
  4. B gets 404 on A's Docker target. B's executor whose daemon id equals A's target address gets `platform_already_registered` at target create.

## 7. Live tests and runbooks (SP8-11)

- Each live test reads its own `.devdata/<platform>.env` with a copy of the Databricks native live test's env loader and skips with the exact message of section 1 when the file or a key is missing. All are opt-in and owner-only. The lead and the builders never create these files.
  - `fabric.env`: `FABRIC_WORKSPACE_ID`, `FABRIC_TENANT_ID`, `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET_FILE`.
  - `palantir.env`: `PALANTIR_HOST`, `PALANTIR_CLIENT_ID`, `PALANTIR_CLIENT_SECRET_FILE`, plus the binding values.
  - `docker.env`: `DOCKER_TARGET_DAEMON_ID`, `DOCKER_APP_ENV_FILE`, plus the Databricks read keys of `databricks.env`.
- Runbooks:
  - `docs/runbooks/fabric-live-validation.md` (service principal, workspace role Contributor, run, cleanup);
  - `docs/runbooks/palantir-live-validation.md` (OAuth client, preview opt-in, approved product version, cleanup);
  - `docs/runbooks/docker-target.md` (host prerequisites, env file, daemon id, firewall: the port stays on loopback behind the client's own proxy, upgrade, manual removal);
  - `docs/runbooks/mcp-access.md` (enable, key with `mcp:access`, Claude Code client configuration, revoke).
- `CLAUDE.md` "Local commands" gains one line per demo and live test. STATUS records the three "live validation waiting on owner input" lines.

## 8. Issue cut (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`. **(C)** marks an isolation-critical issue. Migrations are new files under a protected path, so the approver escalates and the lead merges after the 13-gate set (B-2530). The migration names `delivery_0008_sp8_kinds`, `packaging_0005_sp8_target_kinds` and `mcp_0001_access` are fixed.

1. **SP8-1 Kinds, claims, addresses, executor dispatch for Fabric, Palantir and Docker (C).**
   - Scope: section 2; both migrations; cross-client and claim tests parametrized over the three kinds (with test-only artifacts). Migration: yes.
   - Checks: `uv run pytest tests/delivery/test_targets.py tests/delivery/test_platform_claims.py tests/delivery/test_routes_cross_client.py tests/packaging tests/executor tests/schema tests/adapters/native/test_addresses.py`.
   - Depends: SP6-3 (#270) and SP6-4 (#271) merged (`delivery_0007`, `packaging_0004`).
2. **SP8-2 `shifde.mcp_access`: endpoint, keys, settings, v1 tools, audit, limits (C).**
   - Scope: 3.1 to 3.7 without `board_get` and `assist_*`; `mcp_0001_access`; preflight line; `scripts/mcp_demo.py --fake` (steps of 3.7, using the MCP SDK client over an httpx ASGI transport; no network). Migration: yes.
   - Checks: `uv run pytest tests/mcp_access tests/identity tests/schema; uv run python -m tests._support.openapi_snapshot --check; python scripts/mcp_demo.py --fake`.
   - Depends: none; may start now.
3. **SP8-3 MCP tools over SP7: `board_get`, `assist_ask`, `assist_get` (C).**
   - Scope: 3.3 rows marked SP8-3; the demo adds an assist request routed and read over MCP; cross-client and cross-project tests. Migration: no.
   - Checks: `uv run pytest tests/mcp_access tests/assist tests/board; python scripts/mcp_demo.py --fake`.
   - Depends: 2, SP7-3 (#279), SP7-4 (#280), SP7-5 (#281) merged.
4. **SP8-4 `adapters.native.fabric`: port, clients, fake (C).**
   - Scope: section 4 port, clients and fake; SDK tests ported. Migration: no.
   - Checks: `uv run pytest tests/adapters/native -k fabric`.
   - Depends: none; may start now.
5. **SP8-5 Fabric delivery: targets, bindings, plan, executor, verify, demo (C).**
   - Scope: section 4 bindings, steps, verify, `/1` template artifact; `scripts/fabric_demo.py`. Migration: no.
   - Checks: `uv run pytest tests/delivery tests/executor tests/adapters/native -k "fabric or cross_client"; python scripts/fabric_demo.py --fake; python scripts/delivery_demo.py --fake`.
   - Depends: 1, 4.
6. **SP8-6 `adapters.native.palantir` (Marketplace): port, clients, fake (C).**
   - Scope: section 5 port, clients and fake. Migration: no.
   - Checks: `uv run pytest tests/adapters/native -k palantir`.
   - Depends: none; may start now.
7. **SP8-7 Palantir delivery: targets, bindings, plan, executor, verify, demo (C).**
   - Scope: section 5 rest; `scripts/palantir_demo.py`. Migration: no.
   - Checks: `uv run pytest tests/delivery tests/executor tests/adapters/native -k "palantir or cross_client"; python scripts/palantir_demo.py --fake`.
   - Depends: 1, 6.
8. **SP8-8 `adapters.native.docker`: container runtime protocol, Docker CLI runtime, fake runtime, adapter (C).**
   - Scope: section 6 runtime, steps and verify; the local harness test (`-m docker`, opt-in). Migration: no.
   - Checks: `uv run pytest tests/adapters/native -k docker`.
   - Depends: none; may start now.
9. **SP8-9 Docker target: `render_docker`, packaging kind, bindings and gates, delivery, demo (C).**
   - Scope: section 6 render, bindings and gates; Docker golden digest and Databricks golden digest unchanged; `scripts/docker_demo.py`. Migration: no.
   - Checks: `uv run pytest tests/appgen tests/packaging tests/delivery tests/executor -k "docker or render or cross_client"; python scripts/docker_demo.py --fake; python scripts/solution_demo.py --fake`.
   - Depends: 1, 8, SP5-9 (#262) merged.
10. **SP8-10 Front end: MCP settings and key scope; Fabric, Palantir and Docker target forms, plan refusals, timelines.**
    - Scope: 3.6; target forms with `experimental` badges; plain-words refusals (`palantir_preview_not_allowed`, `workspace_not_claimed`, `docker_host_mismatch`); a Palantir plan note that the product content is not derived from the release.
    - Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`.
    - Depends: 2, 5, 7, 9.
11. **SP8-11 Live tests (skipping), runbooks, `CLAUDE.md` lines, STATUS lines.**
    - Scope: section 7.
    - Checks: `cd backend; uv run pytest tests/adapters/native/test_fabric_live.py tests/adapters/native/test_palantir_live.py tests/adapters/native/test_docker_live.py -m live -rs` (three skips with the section 1 messages); `python scripts/traceability.py --check`.
    - Depends: 3, 5, 7, 9, 10.

## 9. Calls (lead, 2026-11-23; to log in `DECISIONS.md`)

Call: S8-A — MCP access is SETH acting as an MCP server over stateless streamable HTTP at `/mcp` inside the API process (`FastMCP`, `mcp` 1.x). There is no stdio server and no separate process. The installation switch `SHIFDE_MCP_ENABLED` is off by default.
Call: S8-B — MCP authentication is by existing `shf_` API keys (user or service account) carrying the new permission `mcp:access`. Cookies, `shx_` credentials and OAuth 2.1 or dynamic client registration are not accepted in v1.
Call: S8-C — MCP is off per client by default (`mcp_client_settings`). A tenant administrator turns it on. The change is audited and shown to the client's owners, following the SHI-provider pattern.
Call: S8-D — the v1 tool catalog is reads plus `start_run` and the SP7 `assist_*` tools, the last two opt-in per client. No MCP tool approves, applies, seals, plans, touches secrets or keys, invokes a connection or runs raw SQL. Run approvals happen only in SETH's screens.
Call: S8-E — the MCP rate limit is per key per API process (in-memory sliding window). Spend is bounded by the ledger plus `max_cost_per_call` (default USD 1, ceiling 25). A database-backed limiter is deferred.
Call: S8-F — MCP audit records metadata only (tool, key, outcome, code, duration, run id), never arguments or results.
Call: S8-G — Fabric ships `fabric.solution/v1` `/1` template releases only (the SDK's Lakehouse + Notebook, with a generated digest-exit first cell). `fabric.iq/v1`, generated apps on Fabric and a Fabric data connection are out; `FabricPlanned` stays `planned`.
Call: S8-H — Palantir ships `palantir.marketplace/v1` only: it installs an operator-approved, pre-published product version named on the target. Preview APIs need the `allow_preview_apis` opt-in, and Ontology overrides are refused. SETH records the authorizing release; the product content is not derived from it. `palantir.superrepo/v1` is refused by name.
Call: S8-I — the standalone Docker target runs SP5's generated Databricks app unchanged in a hardened container on a client-owned host, driven by that client's executor. It reads a Databricks workspace this organization has claimed. The service-principal secret exists only in a host env file. Snowflake-backed Docker apps are out.
Call: S8-J — a Docker host's identity is its daemon id, entered as the target address (`docker://<id>`), claimed under the one-platform rule and checked at every inspect (`docker_host_mismatch`).
Call: S8-K — the Docker image uses a pinned-digest `python:3.12-slim` base and `gunicorn==23.0.0` added in a separate hashed lock (`requirements-docker.txt`). The Databricks render and its golden digest are unchanged.
Call: S8-L — Fabric, Palantir and Docker are built and proven only against in-process fakes and stay `experimental`. Each live test skips with "live validation waiting on owner input: ...". The local Docker daemon test is an opt-in harness (`-m docker`) that never counts as validation, and no demo needs Docker.
Call: S8-M — one migration pair (`delivery_0008_sp8_kinds`, `packaging_0005_sp8_target_kinds`) widens the kinds for all three platforms in SP8-1, so the platform issues need no migration and run in parallel. MCP adds `mcp_0001_access`, its only new client-owned table.

## 10. Settled after builder read (B-3923)

A fresh builder read this design and raised 40 points. Each point's own recommendation is accepted. The lines below bind every issue of section 8 and override earlier text where they differ. Names were checked against the code at `0701bf1`.

1. `mcp_access` builds an ASGI sub-app (FastMCP's streamable-HTTP app). Its `StreamableHTTPSessionManager.run()` is entered from the API app's lifespan, and an ASGI auth middleware sits in front of it. `shifde.app` mounts it at `/mcp` when `SHIFDE_MCP_ENABLED`.
2. `identity` gains one public, bearer-only entry point, `identity.dependencies.authenticate_bearer_only(request, session, settings, ctx)`. It wraps the existing `_authenticate_bearer`, which calls `apikeys.authenticate_bearer` and counts failures in `app.state.api_key_failure_limiter`. Cookies are never read.
3. Only `Authorization: Bearer shf_...` is accepted. Anything else gets one generic 401 `invalid_credentials` with the same body: no header, another scheme, an `shx_` executor credential, a cookie, or an unknown, revoked or expired key.
4. `mcp:access` is checked with the PDP at the organization's ref (`scope.tenant_ref`). A binding at workspace or project level only is not enough.
5. A client that has MCP switched off gets 403 `mcp_disabled` after the key is authenticated. The refusal is audited in the key's own client.
6. A platform administrator has no MCP route of their own. Their keys are ordinary keys of one client's principal, and no tool takes an organization argument.
7. The owner-visible overview moves to SP8-10. The client overview reads `enabled`, `updated_by` and `updated_at` from `GET /api/v1/orgs/{org}/mcp/settings` (`McpSettingsOut`), plus the named field `endpoint_url` (from `SHIFDE_MCP_PUBLIC_URL`, null when unset).
8. Permission per tool:
   - `whoami`: `mcp:access`;
   - `list_projects`: `project:read`;
   - `list_agents`: `agent:read`;
   - `start_run`: `agent:execute` at the agent's scope;
   - `get_run`: `run:read`;
   - `list_releases`, `get_release`: `packaging:solution:read`;
   - `list_deployments`, `get_deployment`: `delivery:deployment:read`;
   - `board_get`: the board's own reads (SP7 7.2);
   - `assist_ask`: `assist:request:write`;
   - `assist_get`: `assist:request:read`.
9. Each tool has one JSON input schema, with `additionalProperties: false`. List tools take `limit` 1..50 (default 20) and `cursor`.
10. `start_run` input is `{project_id: uuid, agent_version_id: uuid, input: string (1..32768 chars), max_cost?: number > 0}`. It returns `{run_id, status}`.
11. The actor of every call is the key's principal, a user or a service account. No tool takes an actor.
12. `max_cost` defaults to `mcp_client_settings.max_cost_per_call`, or USD 1.00 without a row. It is always `min(argument, setting)`.
13. Ids resolve only inside the key's client and `resource_scope`. Anything else is `not_found`, with the same text as an unknown id. `get_run` applies the run route's permission (`run:read` through `RunService`) and returns the retention-scrubbed content as the route does.
14. `allowed_tools`: NULL means the default set (every tool except `start_run` and `assist_ask`), and an empty array means no tool.
15. `core.ratelimit.SlidingWindowLimiter.check` gains an optional `limit: int | None = None` that overrides the instance limit for that call. The change is additive; existing callers are unchanged.
16. `core.errors.RateLimited` gains an optional `retry_after: int | None`, rendered as a `Retry-After` header and in the MCP error. The change is additive.
17. The effective key rate is `min(mcp_client_settings.rate_per_minute, SHIFDE_MCP_MAX_RATE_PER_MINUTE)`. The client ceiling is 4x that rate.
18. Refusals are audited through the existing denial-audit pattern, `identity.dependencies.register_denial_audit`, plus explicit `mcp.access_refused` rows in the key's client.
19. A key whose secret does not verify writes nothing in any client. Only the existing installation-level row of `apikeys._reject_unproven` is written.
20. The middleware enforces 256 KiB twice. A `Content-Length` above the cap gets 413 before the body is read. A streamed body is counted and cut at 256 KiB, also with 413.
21. A request with any `Origin` header gets 403 with body `{"error": "origin_not_allowed"}`. A `Host` not in `SHIFDE_MCP_ALLOWED_HOSTS` gets 421 from the SDK's transport-security check.
22. Migration `depends_on` uses the real heads at build time, read with `uv run alembic heads`, never this text.
23. `shifde_app` gets column-wise UPDATE on `mcp_client_settings` (`enabled`, `rate_per_minute`, `max_cost_per_call`, `allowed_tools`, `updated_by`, `updated_at`), plus SELECT and INSERT. `tenant_id` cannot be updated, and nothing can be deleted.
24. `mcp:settings:write` is flagged privileged, like `assist:admin` and `delivery:plan:approve`. `platform_administrator` gets none of the three MCP permissions.
25. Import-linter: SP8-2 inserts a new layer `"shifde.mcp_access"` directly below `"shifde.app | shifde.cli"` in the layers contract, and adds a forbidden contract from `shifde.mcp_access` to `shifde.*.repository`. No contract is loosened.
26. The audit list reuses the existing `GET /api/v1/orgs/{org}/audit-events?category=mcp` (`audit:read`). SP8-2 adds `"mcp": ("mcp.",)` to `audit.service.CATEGORY_PREFIXES`. No new route.
27. The repo has no shared statement-log fixture. The tests use the `event.listen(bind, "before_cursor_execute", ...)` pattern of `tests/ontology/test_service.py`, and SP8-2 may lift it into `tests/_support/` as a context manager.
28. The builder writes the exact `backend/tests/schema/*` snapshot lines in build.md, and the lead adds them (B-2530).
29. SP8-3 depends on SP7-3 (#279), SP7-4 (#280) and SP7-5 (#281).
30. `assist_ask` takes SP7's request body `{text, specialist?, context?}` plus `project_id` (and the workspace derived from it). Spend goes through the ledger as the key's principal, capped as in 12.
31. SP8-1 builds on SP6-3 (#270) and SP6-4 (#271), which bring `delivery_0007` and `packaging_0004`.
32. A Fabric workspace id is strict lowercase-normalized hex `8-4-4-4-12`. Any other form is 422.
33. Docker daemon ids follow "first claim wins". A host whose real daemon id differs from the target address is caught at inspect (`docker_host_mismatch`). An operator frees a claim by archiving the target. `docs/runbooks/docker-target.md` says so.
34. Section 1's Docker row is reworded: "SP5's generated Databricks app, run as a container on a client-owned host by that client's executor; it reads the client's Databricks workspace".
35. The SDK paths of sections 4 and 5 were confirmed with `git show 1fd902b:<path>`.
36. The fakes prove nothing about the real platforms. Every new kind keeps `live_validated: false` until the owner's live run passes.
37. Palantir: a new product version is a target PATCH of `version_id` (audited) followed by a new plan. The plan stores the bindings digest, and apply refuses with `plan_stale` if the target changed after the plan.
38. No content check of the Palantir product runs. The plan screen says so, and verify records `{"name": "content-check", "status": "not_run"}` in its detail. Done-when adds: the Palantir demo shows that line.
39. Docker: `requirements-docker.txt` is a checked-in hashed lock. The base image digest is a recorded decision (DECISIONS) with a test pinning it. Tests make no network call.
40. SP8-9 depends on SP5-9 (#262). The "workspace claimed by this organization" lookup is one `delivery` service method, `TargetService.holds_claim(platform_kind, address) -> bool`, which reads this organization's rows only.

**Real platforms.**
- `DockerCliRuntime` and the `-m docker` harness are opt-in. They are never in `factory/checks.sh` or CI and never count as validation.
- Each live test reads only its own named env file in `.devdata/` (`fabric.env`, `palantir.env`, `docker.env`) and skips without it.
- In this build, nothing is deployed anywhere but the in-process fakes.
