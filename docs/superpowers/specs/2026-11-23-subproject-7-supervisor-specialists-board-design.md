# Sub-project 7: supervisor and specialists; project and release board — short design

Date: 2026-11-23. Status: approved (lead, under charter 3). Contract: unified design sections 5.2 (`assist`), 5.3 (isolation; "a specialist agent in SETH works inside the organization the user is currently in"), 5.6, 5.7, 6, 7 (calls 9, 10, 12), 8. Builds on SP1 (`runtime`, `build`, `governance`, `ledger`, `models_admin`), SP2/SP3 (`delivery`, `packaging`, `evaluation`), SP5 (`appgen`, `runtime/jobkinds.py`, `shifde/worker_main.py`; written "SP5 5.2" and so on) and SP6. Settled after a builder read in section 14.

## 1. Scope and "done when"

**Done when (design section 6):** "A request in SETH reaches the right specialist; each client's solutions show their release and target status." No owner input.

Concrete checks:
1. `python scripts/assist_demo.py --fake` (cluster running) ends with `ASSIST DEMO PASSED` (exit 0), or `ASSIST DEMO FAILED: <step>` (1). On a scratch database (section 12) it shows:
   - six requests in organization A, each routed to the expected specialist (five by rule, one by the fake router model), specialist runs that answer, and one refused request (`route_ambiguous`) with model routing off;
   - a deployment specialist's proposal accepted by a person as a `requested` delivery plan, with the second-person approval rule unchanged;
   - A's board showing only A's solutions with their release and target status, B's board only B's, and every cross-client read refused with 404.
2. `cd backend; uv run pytest tests/assist tests/board tests/runtime/test_internal_tools.py` passes, including the golden routing corpus (5.2) and the isolation tests of section 9.

**In scope:**
- new module `shifde.assist`: the supervisor (routing), the specialist catalog and per-organization install, requests, proposals, and the SETH-internal tool functions the specialists use;
- new module `shifde.board`: a read model of each organization's projects, solutions, releases and target status;
- `runtime` and `build`: an injected registry of SETH-internal tool functions (the `jobkinds` pattern of SP5 3);
- the screens, the demo and the `CLAUDE.md` line.

**Out of scope:**
- task tracking (design call 12): no tasks, assignees, due dates or comments;
- a board across organizations, platform administrators included (Call S7-F);
- specialists that write to a client platform, approve plans, accept appgen drafts or run SQL over client data (Calls S7-D, S7-I);
- conversation memory across requests: one request is one specialist run (Call S7-L);
- a default specialist: an undecided request with model routing off is refused (B-3922 point 32);
- MCP access to the supervisor (SP8); telemetry from deployed apps (call 2): target status is what SETH's own deployment records know.

## 2. Shape

```
user request ─► assist.route (rules ─► [model fallback, worker job]) ─► specialist key
            ─► permission + drift pre-check ─► RunService.create(agent, requester as principal)
            ─► specialist run (ordinary agent run; seth.* tools under the run principal)
            ─► answer text + proposals ─► a person accepts ─► owning service (appgen, evaluation, delivery)
```

**Call S7-A: the supervisor is code, not an agent.** Routing is a deterministic function in `assist/routing.py`. A model is asked only when no rule decides, only if the organization switched model routing on, and only to pick a key from the closed catalog. Pre-checks and run creation are never model-driven.

## 3. Modules, layers, worker wiring

**Layers** (`backend/pyproject.toml`, after SP5):

```
"shifde.main | shifde.executor | shifde.worker_main",
"shifde.app | shifde.cli",
"shifde.assist",
"shifde.board",
"shifde.delivery",
"shifde.appgen",
...
```

- `assist` may import: `core`, `authz`, `audit`, `tenancy.service`, `models_admin.service/.schemas`, `governance.service/.schemas` (usage and budget reads live there, not in `ledger`), `build.service/.schemas/.internal_functions`, `runtime.service/.schemas/.internal_tools/.jobkinds`, `connections.service/.schemas/.repository.enqueue_job`, `packaging.service/.schemas`, `evaluation.service/.schemas`, `appgen.service/.schemas`, `delivery.service/.plans/.schemas`, `board.service/.schemas`, `jsonschema`.
- `board` may import only `core`, `authz`, its own `board.repository`, and `*.service`/`*.schemas` read functions of `tenancy`, `packaging`, `evaluation`, `delivery`. A new forbidden contract "board writes nothing" forbids `shifde.audit.service`, every other module's `.repository`, `shifde.delivery.plans` and `shifde.delivery.deployments` (their `list`/`get` run the plan and deployment settle steps, which commit). `board/repository.py` issues only `SELECT` (a test greps it for `INSERT|UPDATE|DELETE|commit`).
- Both join the "api -> service -> repository -> models" containers, the HTTP-free services list, and the forbidden lists of the core and both executor contracts. `runtime`, `build`, `delivery`, `appgen`, `governance` never import `assist` or `board`.

**Internal tool registry (Call S7-C).** The precedent is `build/executors.py` `ONTOLOGY_QUERY`: a `builtin.function` key that runs only inside a run, with the run principal.
- `build/internal_functions.py` (declarations only): `INTERNAL_FUNCTIONS: Mapping[str, InternalFunctionDecl]` with `key` (`seth.<area>.<verb>`), `input_schema`, `output_schema` (every one has `truncated: boolean`), `side_effect_class` (one of `SIDE_EFFECT_CLASSES`), `description`.
- `build/executors.py`: `binding_schema("builtin.function")` enumerates `sorted(BUILTIN_FUNCTIONS | INTERNAL_FUNCTIONS)`, and tool create and publish validation accept a key in that union. `build/tool_runtime.run_builtin` and the test panel refuse an internal key with "seth.* functions run only inside an agent run", as `_ontology_query` does.
- `runtime/internal_tools.py` (the only definition of these types): `InternalToolCall` (frozen dataclass: `session`, `scope` — the run context's `TenantScope`, PDP re-derived by `authz.execution`; `tenant_id`, `workspace_id`, `project_id`, `run_id` from the run row; `step_key`, the step's idempotency key `sha256(run_id, call_ref, epoch)` of `steps._key`; `args`, already validated by `steps._arg_problems`). `InternalToolFn = Callable[[InternalToolCall], JsonValue]`. `InternalToolRegistry.of({...})` raises `ValueError` on a key not in `INTERNAL_FUNCTIONS`.
- **Transactions.** A function runs in the worker's step session, with the run's organization set for RLS, inside the step's transaction; it never commits, never opens a session and never calls a service method that commits (`PlanService.list/get`, `DeploymentService.list`, anything that settles). Deployment and plan facts come from `board.service`.
- **Journal and approval.** `steps.py` skips the journal only for `builtin.function` with class `none`. Internal `read` functions are therefore journaled like any read tool; the three `seth.propose.*` (`write_idempotent`) are journaled with `step_key` and replays find the existing proposal through UNIQUE (`tenant_id`, `run_id`, `step_key`). An agent's approval classes are `spec.approval_rules.require_approval_for ∪ {"destructive"}`; install sets `require_approval_for: []`. A governance rule (`policies.tool_approval`) can still add an approval: then the proposal waits on the normal run approval, which is correct and documented in the settings screen.
- **Principal.** A requester deactivated or stripped of access after the run starts is refused by `authz.execution.execution_principal` at the next gate; the tool call gets the 403 and the run fails `authz_denied`, as any run.
- **Wiring.** `runtime.worker.Worker` and `runtime.worker.run` gain `internal_tools: InternalToolRegistry | None`; `steps.py` dispatches an internal key to the registry; an internal key with no registered function (no registry, or a key missing from it) fails the call with `internal_function_unavailable`. Callers that pass `shifde.worker_main.internal_tools()`: `shifde.worker_main.run` (the `shifde-worker` command after SP5-2), the in-process workers of `tests/_support` that run agent runs (`runs.py`, `fullapp.py`, `modeljobs.py`; SP7-1 lists the exact set), and every demo script that runs a worker. `job_handlers()` gains `assist.service.ROUTE_JOB_KIND: assist.service.run_route_job`. Tests assert the registry keys equal `INTERNAL_FUNCTIONS`, and that a worker built by `worker_main` executes a `seth.*` tool.

**`shifde.assist` contents:** `catalog.py` (section 4), `routing.py` (pure, `ROUTER_VERSION = "assist.router/1"`), `schemas.py`, `repository.py`, `service.py` (settings, install, request, handover, route job, proposals), `tools.py`, `api.py`.

**`shifde.board` contents:** `schemas.py`, `repository.py` (read queries), `service.py`, `api.py`.

## 4. The specialist catalog

`assist/catalog.py` is code, versioned `CATALOG_VERSION = "assist.catalog/1"`. Each entry: `key`, `title`, `router_description` (one paragraph the router model sees), `instructions` (system prompt text, declaring the prompt variables `assist_request_id` and `context`, both strings), `tools` (internal keys), `required_permissions` (checked on the request's project before handover), `proposal_kinds`.

| Key | Does | Tools | Required permissions | Proposes |
|---|---|---|---|---|
| `research` | answers questions about the project's data and existing work (from AF `0fdd09c` `general/contracts.py` `agent_type` `research`/`review`) | `seth.catalog.read`, `seth.solutions.read` | `connection:read` | nothing |
| `solution_design` | proposes the shape of a solution: agent, tables, screens, target kind | `seth.catalog.read`, `seth.solutions.read`, `seth.releases.read` | `connection:read`, `packaging:solution:read` | nothing (text) |
| `build` | turns a brief into an appgen request | `seth.catalog.read`, `seth.propose.app_request` | `connection:read`, `appgen:request:write` | `appgen_request` |
| `evaluation` | explains evaluation results, proposes a run | `seth.releases.read`, `seth.evaluations.read`, `seth.propose.evaluation_run` | `packaging:solution:read`, `evaluation:run:read`, `evaluation:run:request` | `evaluation_run` |
| `cost` | answers spend and budget questions | `seth.costs.read`, `seth.budgets.read` | `usage:read`, `budget:read` | nothing |
| `deployment` | explains release and target status, prepares a plan request | `seth.board.read`, `seth.targets.read`, `seth.propose.delivery_plan` | `delivery:target:read`, `delivery:deployment:read`, `delivery:plan:read`, `delivery:plan:request` | `delivery_plan` |

All keys exist in the catalog today except `appgen:request:write`, which SP5-2 (#255) seeds; a test asserts every listed key is a row of `shifde.permissions`.

**Internal functions.** Every id in `args` is resolved through the owning service **and** must belong to the run's project (or, for targets, the project or its workspace); otherwise the result is `{"error": "not_found"}`, the same for another project and another organization. Results are capped at 64 KiB with `truncated: true`.

| Key | Class | Calls (run principal's scope) | Returns |
|---|---|---|---|
| `seth.catalog.read` | read | `connections.service`: connections visible in the run's project, latest `schema` discovery | names, kinds, tables with column names and types. **No row values** (Call S7-I) |
| `seth.solutions.read` | read | `packaging.service.SolutionService.list/get/list_versions/get_version` | solutions, versions, entrypoint, `spec.screens` (added by SP5-3) |
| `seth.releases.read` | read | `packaging.service.scoped_release`, `SolutionService.list_releases` | release status, digest, compatibility diagnostics |
| `seth.evaluations.read` | read | `evaluation.service.EvaluationService.list_runs/get_run` | runs of a release: status, per-threshold result |
| `seth.costs.read` | read | `governance.service.GovernanceService.summary` (project filter), `.run_usage(run_id)` (`usage:read`) | totals by day, model and run kind; one run's cost |
| `seth.budgets.read` | read | `GovernanceService.list_budgets`, `.budget_status` (`budget:read`) | budgets in scope and remaining amount |
| `seth.board.read` | read | `board.service.project_board` | section 7.2 for the run's project |
| `seth.targets.read` | read | `delivery.service.TargetService.list` filtered to the run's project and workspace | targets (no `credential_ref`) |
| `seth.propose.app_request` | write_idempotent | `assist.service.propose` | proposal id |
| `seth.propose.evaluation_run` | write_idempotent | `assist.service.propose` | proposal id |
| `seth.propose.delivery_plan` | write_idempotent | `assist.service.propose` | proposal id |

**Install (Call S7-B, B-3922 lead call).** Specialists are ordinary **project-level** SETH agents, installed per project (`scope {type: "project"}`, `workspace_id` and `project_id` set). `authz/pdp.py` `_covers` (a project grant covers resources with that `project_id`) and `runtime.service._target` (the agent must belong to the run's project) then work unchanged; nothing in `authz` or `runtime` is loosened.
- **Organization level.** An org admin (`assist:admin`) enables assist for the client in `assist_settings` (`assist_enabled`) and holds the catalog version pin (`catalog_version`, the only version install accepts). Turning `assist_enabled` off disables every project's specialists of that client: handover refuses `specialist_disabled` while it is off; project rows are not rewritten.
- **Project level.** `POST /orgs/{org}/workspaces/{ws}/projects/{proj}/assist/install` with `{model_id, keys?: [..]}`. Refused 409 `assist_not_enabled` while the org switch is off or the pin differs from `CATALOG_VERSION`. Pre-check before any write, on the project ref: `assist:install`, `prompt:create|update|publish`, `tool:create|update|publish`, `agent:create|update|publish`, `agent:activate`; and `model_id` visible and allowed (`models_admin` `allowed_model_ids`). No auto-install anywhere.
- **First acceptance check of SP7-2:** a user bound only as `developer` on project P1 executes P1's installed specialist in P1, and gets 404 or 403 on P2's specialist and on a run of it in P1.
- Per key, in order, all in the project: one published `builtin.function` tool per internal key, artifact key `seth-<area>-<verb>` (`.` -> `-`, `_` kept, so `seth-propose-app_request`; all match `build/schemas.py` `KEY_PATTERN`), with `input_schema`/`output_schema`/`side_effect_class` generated from `INTERNAL_FUNCTIONS`; the prompt `assist-<key>` (`sections.system` = `instructions`); the agent `assist-<key>` with the tools, the model, `approval_rules {require_approval_for: []}` and `limits {max_iterations: 8, max_tool_calls: 12, max_tokens: 100000, deadline_s: 120, max_cost: settings.specialist_max_cost}` (all keys of `build/agents.py`), published and set `active`.
- Idempotent and resumable per project: `install_progress` on each (project, key) row is an ordered list `[{step, artifact_key, artifact_id, version_id}]`; each step reuses the project's existing artifact of the same key and is recorded after it commits (build commits per call). A repeated install resumes or, when everything exists, publishes new versions only if the generated spec differs.
- Per-project `enable`/`disable` (`assist:install` on the project): set the row's `status` and the agent's `status` to `active`/`disabled` (`agent:activate`). Build has no `archived` status and `retired` is terminal, so disable uses `disabled`.
- A project member with build rights may edit `assist-<key>` in the builder; the pinned version stays in use until install runs again, and the drift check (5.4) runs per project. Build has no artifact delete path, so the RESTRICT foreign keys of section 8 are never hit by `build`; a retired agent is refused at handover.

## 5. Routing

### 5.1 Request

`POST /api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/assist/requests` with `{text, specialist?, context?}`.
- `text`: 1..4000 characters after strip; never logged, never in audit details.
- `specialist`: optional key, the user's pick.
- `context`: optional `{kind, id}`, resolved under the caller's scope and required to be in this project:

| kind | Resolved by |
|---|---|
| `solution` | `packaging.service.SolutionService.get(ws, proj, id)` |
| `release` | `packaging.service.scoped_release(scope, id)`; its `project_id` must equal the request's |
| `target` | `delivery.service.TargetService.get(id)`; in this project or its workspace |
| `plan` | `delivery.plans.PlanService.get(id)`; its target in this project or workspace and its release in this project (a plan without a release is refused) |
| `deployment` | `delivery.deployments.DeploymentService.get(id)`; same rule, its plan's release in this project |
| `evaluation_run` | `evaluation.service.EvaluationService.get_run(id)`; its release in this project |

  Not visible or not in this project gives 404 `context not found`, and nothing is stored. (These getters may commit their settle step before the insert; this is the API path, not a run step.)

Checks before any write: `assist:request:write` on the project (403); project active.

### 5.2 Deterministic rules

`routing.route(text, specialist, context_kind) -> RouteResult` is pure; `RouteResult = {specialist: key | None, method: "explicit"|"rule"|"undecided", rule_id: str | None, candidates: [keys]}`.

1. `specialist` given: `method = explicit`; an unknown key is 422 `specialist_unknown`.
2. Normalize: Unicode NFKC, lowercase, tokens `[a-z0-9]+`, joined by single spaces.
3. Rules `(rule_id, key, pattern)`; each pattern is `\b(?:...)\b` matched with `re.search` over the normalized string, in table order:

| Order | rule_id | Alternatives | Key |
|---|---|---|---|
| 1 | `deploy` | `deploy\w*`, `rollout`, `roll out`, `promote`, `release to` | `deployment` |
| 2 | `status` | `which release`, `what release`, `where is`, `target status`, `is live`, `is running` | `deployment` |
| 3 | `cost` | `cost\w*`, `spend\w*`, `spent`, `budget\w*`, `tokens?`, `price\w*`, `bill\w*` | `cost` |
| 4 | `eval` | `evaluat\w*`, `test cases?`, `accuracy`, `thresholds?` | `evaluation` |
| 5 | `build` | `(build\|draft\|generate\|create\|make)( an?)? (agent\|tool\|prompt\|screen\|app)` | `build` |
| 6 | `design` | `design\w*`, `architect\w*`, `approach`, `how should (we\|i) (build\|structure)` | `solution_design` |
| 7 | `research` | `research\w*`, `look up`, `find out`, `which tables`, `what data`, `summari[sz]e`, `review (the\|this) document` | `research` |

4. A context adds a key: `release`, `plan`, `target`, `deployment` -> `deployment`; `evaluation_run` -> `evaluation`; `solution` -> none.
5. `candidates` = the distinct keys matched, in table order, then the context key if new.
6. Exactly one candidate: `method = rule`, `rule_id` = the first matching rule, or `context:<kind>`. Otherwise `method = undecided` (zero or several candidates).

SP7-3 owns the final table; the golden corpus `backend/tests/assist/routing_corpus.json` has at least 60 requests (at least 8 per specialist, 6 ambiguous, 6 with no match), each with the expected `RouteResult`. The six ambiguous cases are: "what will it cost to deploy release 3", "evaluate the cost of this agent", "design an evaluation for the churn agent", "research which release is live", "build an app and deploy it", "what is the budget for the evaluation run". A rule change that changes any corpus answer updates the corpus in the same issue.

### 5.3 Model fallback (Call S7-K)

Only when `method = undecided` and `assist_settings.model_routing_enabled`:
- The request is stored `queued` and one job `assist.route` is queued in the same transaction (`connections.repository.enqueue_job`, `dedupe_key = f"assist.route:{request_id}"`); 202.
- `run_route_job(database, runtime, tenant_id, job_id)` (SP5 5.2 pattern): load; `routed|refused|failed` return; `classifying` -> `failed` `model_call_unknown` with no call; `handing_over` -> resume 5.4 step 4. Re-derive the requester; lost `assist:request:write` -> `failed` `access_revoked`. No `router_model_id` -> `failed` `router_model_missing`. Set `classifying`, commit.
- One call: `ModelService.invoke_at(ResourceRef(tenant, ws, proj, "assist_request", id), router_model_id, InvokeIn(messages=[system, user], max_tokens=300), usage_event_type="model_call")` as the requester. `invoke_at` requires `model:invoke` on the project and applies visibility, the allowlist, the SHI switch, the budget hold and the usage row. `ResourceRef.resource_type` is used only by the PDP; the ledger stores tenant, workspace, project and principal, so no ledger value is involved (Call S7-H).
- `system` = fixed text plus each entry's `key` and `router_description`; `user` = `{"text": ..., "candidates": [...]}`. Nothing else.
- Reply: one JSON object `{"specialist": <key>|null, "reason": str <= 300}` validated with `jsonschema` (closed). The allowed keys are `candidates` when non-empty, else all six. `null` or an out-of-set key -> `refused` `route_unclear`; invalid JSON -> `refused` `route_unclear`, detail `invalid_model_output` in the audit row. An `AppError` from `invoke_at` -> `failed` with its `code`.
- On a key: `method = model`; continue with 5.4 inside the job.

With model routing off, `undecided` is `refused` at once: `route_ambiguous` (two or more candidates, listed) or `route_unclear` (none). The screen offers the candidates, or all six, as explicit picks. There is no retry route: a `refused` or `failed` request is final, and the user sends a new request (Call S7-L).

### 5.4 Handover

For the chosen key, in order; each failure ends the request `refused` with the code:
1. `assist_settings.assist_enabled` is on (`specialist_disabled`), and the `assist_specialists` row for (the request's project, key) exists (`specialists_not_installed`; the request is stored `refused` and answered 201, no auto-install) and is `enabled` (`specialist_disabled`). The run always uses that project's own agent, in the request's project, as the requester; a P1 specialist never runs for a P2 request.
2. The requester holds every `required_permissions` key and `agent:execute` on the agent, on the project (`specialist_not_permitted`; the response names the missing keys).
3. Drift: the pinned agent version is `active`; its tools are exactly the catalog's internal tools for the key; each tool's spec equals the one generated from `INTERNAL_FUNCTIONS` (`specialist_modified`, "run install again").
4. Set `handing_over` and commit (the API path also queues the `assist.route` job in that transaction, as a recovery path). Then `RunService.create(RunCreate(kind="agent", agent_id, version_id=pinned, workspace=ws_slug, project=proj_slug, input=text, variables={"assist_request_id": id, "context": json or ""}), idempotency_key=f"assist:{request_id}", max_cost=settings.specialist_max_cost)` under the requester's scope. `RunService.create` commits its own transaction, so handover is two transactions; the idempotency key (per requester and key) makes a repeat after a crash return the same run, so there is no orphan or duplicate run. The variables are validated against the prompt's declared variables by `AgentOps.assemble`.
5. Set `routed` with `specialist_key`, `route_method`, `rule_id`, `router_version`, `run_id`. If the returned run is already `failed` (the budget pre-flight of `_budget_denied`), the request is `failed` with that run's error code and keeps `run_id`.

The run is then ordinary: status, steps, cost and cancel are the existing run routes. `AssistRequestOut.run` is read live through `RunService.get` (`queued|running|...|completed|failed|cancelled|timed_out`): a failed, cancelled or timed-out run shows that status and its error; its open proposals stay open and can be accepted or dismissed.

### 5.5 Request states

| From | To | Actor | When |
|---|---|---|---|
| (new) | `handing_over` | API | explicit or rule, pre-checks 1-3 passed |
| (new) | `refused` | API | 5.3 off-path or 5.4 checks 1-3 |
| (new) | `queued` | API | undecided, model routing on |
| `queued` | `classifying` | worker | committed before the model call |
| `classifying` | `handing_over` / `refused` / `failed` | worker | 5.3, 5.4 |
| `handing_over` | `routed` / `failed` | API or worker | 5.4 step 5 |
| `queued`, `classifying` | `failed` | worker | `access_revoked`, `model_call_unknown`, `router_model_missing` |

Every transition is audited: `assist.request.created|classifying|handing_over|routed|refused|failed` with `specialist_key`, `route_method`, `rule_id`, `router_version`, `status_reason`; never the text.

## 6. Proposals (Call S7-D)

A `seth.propose.*` call stores a proposal and changes nothing else. The payload is validated at proposal time against the owning schema and the run's project (errors are the tool result, so the agent can correct itself):
- `appgen_request`: `{connection_id, tables, brief, model_id}` (SP5 5.1 body);
- `evaluation_run`: `{release_id}` (`evaluation.schemas.EvaluationRequest` is `{}`; dataset and thresholds come from the release manifest);
- `delivery_plan`: `delivery.schemas.PlanCreate` `{target_id, release_id, application_key, display_name}`.

Routes `POST /orgs/{org}/assist/proposals/{id}/accept` (body `{}`) and `/dismiss`:
- The workspace and project come from the proposal's request row, never from a body or path. The caller needs `assist:request:read` on that project (404 `proposal not found` otherwise) and `assist:proposal:accept` (403).
- Accept calls the owning service **as the accepting person**, in that project: `appgen.service` request create (SP5-2, #255), `evaluation.service.EvaluationService.request(release_id, EvaluationRequest())`, `delivery.plans.PlanService.request(PlanCreate(...))`. Success: `accepted` with `result_ref {kind, id}`. Any refusal (including state that changed since the proposal: release withdrawn, target archived, connection inactive) is `failed` with the service's error code; a failed proposal can only be dismissed.
- The accepter must be able to read `result_ref` (it was created by them in their scope); the detail route shows `result_ref` only to callers who can read the referenced object.
- No catalog entry holds `delivery:plan:approve`; a test scans `INTERNAL_FUNCTIONS` and `assist/tools.py` and fails on any call to an approve, reject, accept-draft, apply, cancel, reconcile or mark-failed function. The accepting person becomes the plan's requester, so SP2's self-approval refusal (`SelfApprovalForbidden`) applies to them.
- Accept is idempotent: a second accept of an `accepted` proposal returns it unchanged; `dismissed` or `failed` gives 409 `proposal_not_open`.

Audit: `assist.proposal.created|accepted|failed|dismissed` with kind and `result_ref`.

## 7. The board (Calls S7-F, S7-G)

### 7.1 Model

No table. The board is computed by `board/repository.py` SELECTs over `tenancy` (workspaces = engagements, projects), `packaging` (solutions, versions, releases), `evaluation` (evaluation runs) and `delivery` (targets, plans, deployments). Links: `delivery_deployments.plan_id -> delivery_plans.id`; `delivery_plans.release_id` (nullable: pre-SP3 template plans have none and are not shown) and `delivery_plans.target_id`; `releases.solution_version_id -> solution_versions.solution_id`.

Per project, the targets shown are all targets visible to the caller in that project or in its workspace with no project, active or not, whether or not a plan exists (cells empty when none). Per solution and target:
- `current`: the latest deployment with status `verified` of a release of the solution;
- `last_attempt`: the latest deployment of any status (`queued, leased, applying, observing, verifying, verified, expired, cancelled, failed, unknown_outcome, resolved_failed`). `unknown_outcome` is shown as such and never folded into `current`;
- `open_plan`: the latest plan in `requested|planning|sealed|approved` with no deployment; a `sealed`/`approved` plan past `expires_at` is shown `expired` (computed on read; the board never runs the settle step).
A release also shows its latest evaluation run status (`queued|running|passed|failed|error`, or `null`).

### 7.2 API

`GET /api/v1/orgs/{org}/board?workspace=&project=&limit=20&cursor=` (`limit` 1..50 projects; `paginate`/`encode_cursor`; ordered by `project.id`) and `GET /orgs/{org}/workspaces/{ws}/projects/{proj}/board`.

```
BoardOut = {generated_at, projects: [{workspace: {id, slug, name}, project: {id, slug, name}, truncated,
  solutions: [{id, name, status, latest_version_no,
    releases: [{id, version_no, digest12, status, sealed_at,
                evaluation: {run_id, status} | null}]   (newest 5; digest12 = releases.digest[:12]),
    targets: [{target_id, name, environment, platform_kind,
               current: {release_id, version_no, deployment_id, verified_at} | null,
               last_attempt: {deployment_id, release_id, status, status_reason, updated_at} | null,
               open_plan: {plan_id, release_id, status, expires_at} | null}]}]}],
  next_cursor}
```

Visibility uses `scope.pdp.visible_scopes(action) -> ScopeFilter(tenant_wide, workspace_ids, project_ids)` (`authz/pdp.py`):
- projects: `project:read`; solutions and releases: `packaging:solution:read`;
- targets: `delivery:target:read` on the target's scope; target cells also need `delivery:deployment:read` (current, last attempt) and `delivery:plan:read` (open plan), else those fields are `null`;
- evaluation: `evaluation:run:read`, else `null`.
The board lists only visible targets and says nothing about hidden ones (B-3922 point 70). The `workspace`/`project` filters: an unknown slug and an invisible one both return `projects: []` with 200 (no existence oracle); the single-project route gives 404 for both. A caller with no read anywhere gets `projects: []`. Another or an unknown organization is 404 as every org route; a platform administrator without a binding gets 404 (as `tests/tenancy/test_capture_content.py` `..._one_without_a_binding_gets_404`).

Bounds: 50 projects per page, 200 solutions per project (`truncated: true` beyond), 5 releases per solution; every query filters `tenant_id` and runs under forced RLS. SP7-5 adds the board route to `tests/perf/test_api_latency.py` with a budget recorded in `docs/build/perf.md`.

## 8. Tables and migration

Migration branch `assist`, one file `assist_0001_assist`: `revision="assist_0001"`, `branch_labels=("assist",)`, `down_revision="base_0001"`, `depends_on` the heads of the branches it references, today `("authz_0002", "identity_0007", "tenancy_0007", "build_0002", "runtime_0007", "models_admin_0004", "appgen_0001")` (re-check with `uv run alembic heads` from `backend/` at build time; `appgen_0001` arrives with SP5-2). It seeds permissions the `delivery_0001` way and has a downgrade. All tables client-owned: forced RLS, `tenant_isolation` on `tenant_id = shifde.current_tenant_id()`, composite `(tenant_id, ...)` FKs, column-wise grants to `shifde_app`, owner `shifde_owner`, `created_at`/`updated_at`. **No new global table; `board` has none.** `shifde_app` gets no DELETE except the retention path below.

**`assist_settings`** (PK `tenant_id`; SELECT, INSERT, UPDATE (`assist_enabled`, `catalog_version`, `model_routing_enabled`, `router_model_id`, `specialist_max_cost`, `updated_by`, `updated_at`)): `model_routing_enabled` bool default false; `assist_enabled` bool default false; `catalog_version` text NULL (the org's pin, set from `CATALOG_VERSION` when enabling); `router_model_id` uuid, no FK (a shared SHI model has `tenant_id` NULL); `specialist_max_cost` numeric(12,4) CHECK > 0, default 1.0000. A missing row means the defaults. Save is refused 422 `router_model_required` when routing is enabled without a model, and 422 `router_model_not_allowed` when the model is not visible to the organization or not in its allowlist (`models_admin` `allowed_model_ids`; a shared SHI model only through the allowlist and the per-client switch). Both are checked again at call.

**`assist_specialists`** (one row per (project, key); PK `tenant_id, id`; UNIQUE (`tenant_id`, `project_id`, `key`); SELECT, INSERT, UPDATE (`agent_version_id`, `catalog_version`, `install_progress`, `status`, `updated_at`)): `workspace_id`, `project_id` composite FK to the project (RESTRICT); `key` CHECK in the six keys; `agent_id`, `agent_version_id` composite FKs to the build artifact and version tables (RESTRICT); `status` `installing|enabled|disabled`; `installed_by` FK users.

**`assist_requests`** (PK `tenant_id, id`; SELECT, INSERT, UPDATE (`status`, `status_reason`, `specialist_key`, `route_method`, `rule_id`, `router_version`, `job_id`, `run_id`, `text`, `context`, `updated_at`)):
- `workspace_id`, `project_id` composite FK to the project (RESTRICT); `requested_by` FK users;
- `text` CHECK `char_length` 0..4000 (0 only after scrub); `requested_specialist` text NULL; `context` jsonb NULL;
- `status` CHECK in 5.5; `route_method` `explicit|rule|model` or NULL; `specialist_key` in the six or NULL;
- `job_id`, `run_id` uuid, no FK (runtime reaps jobs; retention deletes runs).

**`assist_proposals`** (PK `tenant_id, id`; SELECT, INSERT, UPDATE (`status`, `status_reason`, `result_ref`, `decided_by`, `decided_at`, `updated_at`)): `request_id` composite FK (CASCADE); `run_id` uuid; `step_key` text; UNIQUE (`tenant_id`, `run_id`, `step_key`); `kind` CHECK `appgen_request|evaluation_run|delivery_plan`; `payload` jsonb CHECK `octet_length(payload::text) <= 65536`; `status` `open|accepted|dismissed|failed`.

**Retention (Call S7-J).** Same pattern as `runtime_0006` (grants plus a guard trigger, no SQL function):
- `maintenance purge --older-than` sets `text = ''`, `context = NULL` on requests older than the cutoff whose run has ended (or is gone); the run's own payload (`input`, `variables.context`) is scrubbed by the existing run purge.
- `maintenance delete --older-than` deletes requests whose `run_id` no longer exists or is NULL, older than the cutoff. `assist_0001` grants DELETE on `assist_requests` and `assist_proposals` to `shifde_app` and adds `BEFORE DELETE` triggers that call `shifde.retention_delete_guard()`'s rule for these tables (refuse unless `shifde.retention_delete_cutoff` is set and the row's `created_at` is before it). Both run as the app role, client by client.

**Permissions** (`is_privileged` false except `assist:admin` and `assist:install`): `assist:request:read`, `assist:request:write`, `assist:proposal:accept`, `assist:install` (project scope), `assist:admin` (tenant scope). Role grants (`authz_0001` role ids): `tenant_administrator` all five; `workspace_administrator` and `project_administrator` `assist:request:read|write`, `assist:proposal:accept`, `assist:install`; `developer` `assist:request:read|write`, `assist:proposal:accept`; `viewer`, `operator`, `auditor` `assist:request:read`. None goes to `platform_administrator` (authz_0002's constraint). The board adds no permission.

If an issue needs another column it adds a new migration in its branch; never edits `assist_0001`.

## 9. Security and isolation

1. **One organization, one project.** A request, its job, its run, its proposals and the specialist agent are in one organization; the run is in the request's project; ids in tool arguments resolve only within that project. Tests: cross-client 404 on every `assist` and `board` route; cross-project 404 (a P1-only member and P2's request, proposal and board); `shifde_app` selects no other tenant's `assist_*` rows (SQL-level).
2. **The specialist's tool set is a subset of the requester's permissions.** The run's principal is the requester (never a service principal, never platform power, `authz.execution`); every function calls the owning service with the run's scope; `required_permissions` refuses early. Tests: a viewer routed to `deployment` gets `specialist_not_permitted`; a member who loses `delivery:target:read` mid-run gets a 403 tool result; a deactivated requester's run fails `authz_denied`; the drift check refuses an agent version with an added `rest.http` tool.
3. **A specialist never sees another client's or project's data.** Tests: in A's P1 specialist run, scripted tool calls naming B's release, connection and target ids, and P2's, each return `not_found`; a proposal naming B's or P2's release is refused at proposal time and again at accept; client B never sees A's specialists (install, catalog status, agents); P2's request never runs P1's specialist (`specialists_not_installed` when P2 has none); a P1-only developer gets 404/403 on P2's specialist agent.
4. **The router sees only the request.** Tests: the fake provider records the prompt: no context object, no other request, no discovered table name; the request text appears in no log record and no audit detail.
5. **Model calls are the client's.** Tests: the SHI provider off for B blocks B's routing call (`failed` with the `models_admin` code) while A's routes; a crash after `classifying` gives `model_call_unknown`, one provider call and the hold `settled_unknown`; a requester without `model:invoke` gets `failed` with the PDP code.
6. **Specialists propose, people decide.** Section 6 tests; self-approval of an accepted plan by its accepter is 403.
7. **Board read-only and scoped.** The contract and grep test of section 3; only visible targets; a platform admin without a binding gets 404 on A's board.
8. **Schema test.** Covers the four tables. If `backend/tests/schema/*` needs an entry, the builder writes it in build.md and the lead adds it (B-2530).

## 10. Cost recording

- Routing model call: one hold and one usage row, event type `model_call` (B-3920), in the request's project, by the requester.
- Specialist run: ordinary run usage, budget pre-flight and per-call reservation (SP3), capped by `specialist_max_cost` (agent `limits.max_cost` and `RunService.create(max_cost=...)`).
- Rule and explicit routing, handover, proposals, accept and the board make no model call and record nothing.
- `AssistRequestOut` links the run's cost page (`govern/runs/[id]/cost`); the `cost` specialist reads the same `governance` reads as the Govern screens.

## 11. API and screens

All routes under `/api/v1/orgs/{org}`, SCOPED, PDP-checked, classified in `tests/authz/scoping/test_workspace_routes.py`, each with a cross-client and (where project-scoped) a cross-project case.

| Route | Permission | Response |
|---|---|---|
| `GET /assist/catalog` | `assist:request:read` | catalog entries plus each key's install status |
| `GET`, `PUT /assist/settings` | `assist:admin` | `AssistSettingsOut` |
| `POST /assist/enable`, `/disable` | `assist:admin` | `AssistSettingsOut` (org switch and catalog pin) |
| `POST /workspaces/{ws}/projects/{proj}/assist/install` | `assist:install` + build keys of section 4, on the project | 200 list of `SpecialistOut` |
| `GET /workspaces/{ws}/projects/{proj}/assist/specialists` | `assist:request:read` | the project's `SpecialistOut` list |
| `POST /workspaces/{ws}/projects/{proj}/assist/specialists/{key}/enable`, `/disable` | `assist:install` + `agent:activate` | `SpecialistOut` |
| `POST /workspaces/{ws}/projects/{proj}/assist/requests` | `assist:request:write` | 201 (`handing_over` resolved to `routed`/`failed`, or `refused`) or 202 (`queued`) `AssistRequestOut`; both documented in OpenAPI (`responses={202: ...}`) |
| `GET /workspaces/{ws}/projects/{proj}/assist/requests?status=&limit=&cursor=` | `assist:request:read` | `Page[AssistRequestSummary]` |
| `GET /assist/requests/{id}` | `assist:request:read` on its project | `AssistRequestOut` (route, run, proposals) |
| `POST /assist/proposals/{id}/accept`, `/dismiss` | section 6 | `ProposalOut` |
| `GET /board`, `GET /workspaces/{ws}/projects/{proj}/board` | section 7.2 | `BoardOut` |

Error codes: 404 `assist request not found`, `proposal not found`, `context not found`; 422 `specialist_unknown`, `router_model_required`, `router_model_not_allowed`; 409 `proposal_not_open`; request `status_reason` codes of 5.3-5.4. Refresh `backend/openapi.json`.

**Screens** (`frontend/src/app/(app)/o/[org]/`, Vitest tests, hand-written types):
- `board`: filters (workspace, project); per project a grid: rows solutions, columns targets grouped by environment; cells with release version and a status badge (`verified`, `unknown outcome`, `failed`, open plan with expiry); links to release, plan and deployment; evaluation badge per release. Sidebar entry "Board"; the `command` dashboard gains a "Board" card.
- `w/[ws]/p/[proj]/assist`: request box with optional specialist picker; on `refused` the candidates as buttons; the request list.
- `assist/[id]`: route decision, run status (polls every 3 s while running, stops after 10 minutes), answer, proposals with Accept/Dismiss and the result link.
- `assist/settings`: install (model picker), enable/disable, model routing switch and router model, `specialist_max_cost`, and the note that a governance approval rule can hold a proposal.
- "Ask SETH" buttons on `releases/[id]` and `deliver/deployments/[id]` open the project's `assist` screen with `context` set.

## 12. Done-when demo

`python scripts/assist_demo.py --fake [--through N]` follows `solution_demo.py`: in-process API, worker (`worker_main.job_handlers()`, `worker_main.internal_tools()`) and executor runner on the fakes, scratch database `shifde_demo_<hex>` dropped at the end (also on failure), no network. The `fake` provider's `script` (`{match, reply}` against the last user message, `adapters/model_providers.py`) drives every model; tool results arrive as user messages, so a script entry matches them (SP5 `AGENT_SCRIPT` pattern). Fixtures: `tests/_support/assist_fixtures.py` `ROUTER_SCRIPT`, `SPECIALIST_SCRIPT`.

1. Organizations A (project P1) and B (project Q1), built with the `solution_demo.py` helpers (SP5-9, #262): a sealed, evaluated release in each; A's deployed and `verified` on the native fake; B's with an `approved` open plan and one earlier `failed` deployment.
2. Each org admin enables assist; the specialists are installed in each project the demo uses (P1, Q1, and P2 for the cross-project step) with a scripted `fake` model.
3. In A, six requests: one per rule family plus one with `context` release; each `routed` by `rule` to the expected key; each run `completed` with an answer; the deployment run's `seth.board.read` result names A's release.
4. In A, an ambiguous request with model routing off is `refused` `route_ambiguous`; switched on with `ROUTER_SCRIPT` it is `routed` by `model` to `evaluation`, with one `model_call` usage row.
5. The deployment specialist proposes a plan; the FDE accepts it (plan `requested`); the FDE's approval is 403; a second admin approves; the deployment reaches `verified`.
6. A viewer's deployment request is `refused` `specialist_not_permitted`.
7. A's specialist run scripted to read B's release id gets `not_found`.
8. A's board lists P1 only, with `current.version_no` and `verified`; B's lists Q1 only, with `last_attempt.status = failed` and `open_plan.status = approved`. A's user gets 404 on B's board, requests and proposals, and the reverse.

## 13. Factory issues (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`. **C** = isolation-critical (`module-builder-critical`, isolation review). Migrations are protected paths: the approver escalates and the lead merges after the 13-gate set (B-2530). SP7-5 may start now; SP7-1 waits on SP5-2 (#255) merged.

1. **SP7-1 runtime internal tool registry and `build` internal function declarations (C).**
   - Scope: section 3 "Internal tool registry"; the refusal outside runs; a test-only registry proving a worker from `worker_main` runs a `seth.*` tool inside a run and that a missing function gives `internal_function_unavailable`; the list of in-process workers that pass the registry. Migration: no.
   - Checks: `uv run pytest tests/runtime/test_internal_tools.py tests/build/test_internal_functions.py; uv run pytest tests/runtime tests/build -k "builtin or internal"`.
   - Depends: SP5-2 (#255) merged.
2. **SP7-2 assist skeleton: migration, catalog, settings, install, enable/disable (C).**
   - Scope: first the project-specialist execution test of section 4 (a P1 developer runs P1's specialist; 404/403 on P2's); then sections 4 (org enable and catalog pin, per-project install), 8; layer and contract changes; routes catalog/settings/enable/disable/install/specialists; the permission-key test; cross-client and SQL-level tests for the four tables. Migration: yes (`assist_0001`).
   - Checks: `uv run pytest tests/assist/test_project_specialist.py tests/assist/test_install.py tests/assist/test_settings.py tests/assist/test_routes_cross_client.py tests/schema`.
   - Depends: 1.
3. **SP7-3 assist routing: rules and corpus, requests, handover, model fallback job, refusals, audit (C).**
   - Scope: section 5; `assist.route` in `worker_main.job_handlers()`; `tests/_support/assist_fixtures.py`; 9.1, 9.2 (pre-check part), 9.4, 9.5 tests. Migration: no.
   - Checks: `uv run pytest tests/assist/test_routing.py tests/assist/test_requests.py tests/assist/test_route_job.py tests/assist/test_handover.py tests/assist/test_routes_cross_client.py`.
   - Depends: 2.
4. **SP7-4 assist internal tools, proposals, retention, demo steps 1-7 (C).**
   - Scope: section 4 functions, section 6, S7-J retention; `REGISTRY` wired in `worker_main.internal_tools()`; 9.2 (in-run), 9.3, 9.6 tests; the no-approve scan; `scripts/assist_demo.py` steps 1-7. Migration: no.
   - Checks: `uv run pytest tests/assist/test_tools.py tests/assist/test_proposals.py tests/assist/test_isolation_in_run.py tests/assist/test_retention.py tests/runtime/test_internal_tools.py; python scripts/assist_demo.py --fake --through 7`.
   - Depends: 3, 5, SP5-9 (#262).
5. **SP7-5 board read model and routes (C).**
   - Scope: section 7; `board` layer, "board writes nothing" contract and grep test; `unknown_outcome`, expiry-on-read, filter-oracle and platform-admin tests; 9.1, 9.7; latency entry. Migration: no.
   - Checks: `uv run pytest tests/board tests/delivery/test_routes_cross_client.py; $env:SHIFDE_PERF_ITERATIONS='50'; uv run pytest tests/perf/test_api_latency.py -q -s -k board`.
   - Depends: none; may start now.
6. **SP7-6 Front end: board.** Scope: `board` screen, sidebar entry, dashboard card. Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`. Depends: 5.
7. **SP7-7 Front end: Ask SETH, request detail, proposals, settings.** Scope: the `assist` screens and "Ask SETH" buttons. Checks: as 6. Depends: 3, 4.
8. **SP7-8 Done-when demo complete.**
   - Scope: demo step 8 and the full run; an "Assist demo" line in `CLAUDE.md` "Local commands"; traceability: no new design 5.3 rows, the new cross-client tests mapped under the existing 5.3 rows (`python scripts/traceability.py`).
   - Checks: `python scripts/assist_demo.py --fake; python scripts/traceability.py --check; python -m unittest discover -s scripts/tests`.
   - Depends: 4, 6, 7, SP5-9 (#262).

## 14. Settled after builder read (B-3922)

The lead accepted each point's recommendation except points 32, 37/18, 52, 54 and 70 (overridden). Each was checked against the code at `8bac461`; "verified" names what was read.

1. SP7-1 depends on SP5-2 (#255, `worker_main`, `jobkinds`, appgen request create); SP7-4/8 on SP5-9 (#262) (section 13).
2. Heads verified from `migrations/versions`: `authz_0002`, `identity_0007`, `tenancy_0007`, `build_0002`, `runtime_0007`, `models_admin_0004`, plus `appgen_0001` (SP5-2); re-check with `alembic heads` at build (section 8).
3. Pattern verified (`delivery_0001`: `branch_labels`, `down_revision="base_0001"`, `depends_on` tuple) (section 8).
4. `scope.pdp.visible_scopes(action: str) -> ScopeFilter` (`authz/pdp.py:323`) (7.2).
5. Board imports only its own repository plus `*.service`/`*.schemas` reads; it may not use `delivery.plans`/`deployments` (verified: their `list`/`get` settle and commit) (section 3).
6. `assist` may import `tenancy.service` (section 3).
7. `InternalToolCall` defined only in `runtime/internal_tools.py` (section 3).
8. Tool create/publish and `binding_schema` accept `BUILTIN_FUNCTIONS ∪ INTERNAL_FUNCTIONS` (section 3).
9. Classes verified (`build/executors.py` `SIDE_EFFECT_CLASSES`). **Verification differs:** `steps.py` skips the journal only for `builtin.function` with class `none`, so internal `read` calls are journaled too; kept `read` and journaled (no runtime change). Proposals are `write_idempotent` and journaled (section 3).
10. Approval: agent approval classes = `approval_rules.require_approval_for ∪ {destructive}` (`runtime/agent.py`); install sets `[]`; a governance rule can still add one. Replay via `(run_id, step_key)` with `step_key = steps._key` (section 3).
11. Worker step session with RLS set, one transaction per step, functions never commit (section 3).
12. Deactivated requester: 403 at the gate, run fails `authz_denied` (section 3).
13. Registry passed by `worker_main.run`, `tests/_support` workers, demos; missing -> `internal_function_unavailable` (section 3).
14. Tool schemas generated from `INTERNAL_FUNCTIONS`; an edit trips the drift check (4, 5.4).
15. `.` -> `-`, `_` kept; verified against `KEY_PATTERN` `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$` (section 4).
16. `install_progress` = ordered step list, reuse by artifact key (section 4).
17. Build keys verified: `{prompt,tool,agent}:create|update|publish`, `agent:activate` (`build/service.py`) (section 4).
18. **Lead call:** specialists are installed per project as project-level agents; org-level agents are not used (section 4).
19. Limits keys verified (`max_iterations`, `max_tool_calls`, `max_tokens`, `deadline_s`; `max_cost` validated in `build/agents.py`) (section 4).
20. Install sets the agent `active`. **Verification differs:** build has no archived status (`active|disabled|retired`, retired terminal); disable sets `disabled` (section 4).
21. `research` requires `connection:read` only (section 4).
22. `cost` requires `usage:read` and `budget:read` (verified in `governance/service.py`) (section 4).
23. `appgen:request:write` comes from SP5-2; permission-key test (section 4).
24. Screens are `solution_versions.spec.screens` (SP5-3) (section 4).
25. Usage reads are `governance.service.GovernanceService.summary/run_usage` (`usage:read`) and `list_budgets/budget_status` (`budget:read`), not `ledger` (section 4).
26. Every `output_schema` includes `truncated` (section 3).
27. Context getters listed per kind (5.1).
28. Installed but disabled: `refused` `specialist_disabled` (5.4).
29. Normalize, then `\b(?:...)\b` in table order; candidates defined (5.2).
30. Patterns run over space-joined tokens (5.2).
31. The six ambiguous cases are listed (5.2).
32. **Override:** refusal is intended; no default specialist (sections 1, 5.3).
33. Model choice restricted to candidates when non-empty, any of six when empty (5.3).
34. Save refused (`router_model_required`, `router_model_not_allowed`); `router_model_missing` at call (5.3, 8).
35. `max_tokens=300` (5.3).
36. Verified: `ResourceRef.resource_type` is used by the PDP only; usage rows and holds store no resource type (`models_admin/service.py`), so `"assist_request"` passes hold and rollup; `invoke_at` requires `model:invoke` on the ref (5.3).
37. **Lead call:** reading `pdp._covers` and `runtime.service._target` shows a project member cannot execute an org-level agent, so specialists are per project; SP7-2's first check is that a project developer executes the project's specialist and gets 404/403 on another project's (section 4).
38. Variables `assist_request_id`, `context` are declared by the prompt and checked by `AgentOps.assemble`; retention covers the run payload and the request (5.4, 8).
39. No retry route; refused or failed is final, a new request instead (5.3).
40. **Verification differs:** `RunService.create` commits its own transaction, so one transaction is impossible; `handing_over` plus the idempotency key `assist:{id}` and the recovery job give no orphan or duplicate run (5.4).
41. Failed, cancelled and timed-out runs shown; their proposals stay open (5.4).
42. Signatures verified: `EvaluationService.request(release_id, EvaluationRequest)` (body `{}`), `delivery.plans.PlanService.request(PlanCreate)` (needs `application_key`, `display_name`), `SolutionService.get(ws, proj, id)`, `scoped_release(scope, id, action)` (section 6).
43. A stale proposal is `failed` with the service's code (section 6).
44. Accept needs `assist:request:read` on the request's project (section 6).
45. Project from the proposal's request row (section 6).
46. Links named: `delivery_deployments.plan_id`, `delivery_plans.release_id` (nullable), `target_id` (7.1).
47. Deployment statuses listed from `DeploymentStatus` (7.1).
48. All visible targets of the project and its workspace shown, null cells (7.1).
49. Ordered by `project.id` (7.2).
50. Evaluation enum verified (`queued|running|passed|failed|error`); `digest12 = releases.digest[:12]` (64 hex, CHECK verified) (7.2).
51. `truncated` on the project object (7.2).
52. **Override:** unknown and invisible filters both return the same empty result (7.2).
53. Platform admin without binding: 404, as `tests/tenancy/test_capture_content.py` (7.2).
54. **Override noted, not triggered:** the ledger has no resource-type column or constraint (point 36), so no escalation and no ledger migration.
55. `context` is in the UPDATE grant (section 8).
56. **Verification differs:** retention uses grants plus a `retention_delete_guard`-style trigger (the `runtime_0006` pattern), not SQL functions (section 8).
57. DELETE granted only on `assist_requests`/`assist_proposals`, guarded; none elsewhere (section 8).
58. Verified: build has no artifact delete path, so no 409 is needed; a retired agent is refused at handover (section 4).
59. Role keys verified (`authz_0001` `SYSTEM_ROLES`); `assist:admin` and `assist:install` privileged; none to `platform_administrator` (section 8).
60. 201 and 202 both in OpenAPI (section 11).
61. Same project resolution as 45 (section 6).
62. SP7-1 proves wiring by test; SP7-4 runs `assist_demo.py --through 7` (section 13).
63. Verified: `FakeProvider` is scriptable (`{match, reply}` on the last user message); SP7-3 adds only fixtures (section 12).
64. SP7-4 and SP7-8 depend on SP5-9 (#262) (section 13).
65. No new 5.3 rows; new cross-client tests mapped under existing rows (section 13).
66. Accepted as written.
67. Router model checked for visibility and allowlist at save and at call; the text is never logged (sections 8, 9.4).
68. Every id-taking function resolves through the run's project scope (`not_found` otherwise), with cross-project tests (sections 4, 9.3).
69. The accepter must be able to read `result_ref` (section 6).
70. **Override:** `targets_hidden` dropped; only visible targets listed (7.2).

Lead call on points 18/37 (B-3922): specialists are installed per project as project-level agents (`assist:install` at project scope, seeded for tenant, workspace and project administrators); the org admin enables assist and pins the catalog version; the org switch disables every project's specialists and per-project disable is allowed; a project without the routed specialist gives `refused` `specialists_not_installed` (201), no auto-install; the run uses the project's own agent in the request's project as the requester; the demo installs in each project it uses (sections 4, 5.4, 8, 9, 11, 12, 13, 15).

## 15. Calls (lead, 2026-11-23; to log in `DECISIONS.md`)

- **Call S7-A:** the supervisor is deterministic code in `assist`; a model only classifies an undecided request into the closed catalog, behind a per-organization switch that is off by default (`fake` provider in every test and demo).
- **Call S7-B (B-3922 lead call):** specialists are ordinary project-level agents installed per project from a code catalog (`assist:install` at project scope), pinned by version, with the drift check per project; the org admin enables assist and pins the catalog version, and the org switch disables every project's specialists. A request in a project without the routed specialist is refused `specialists_not_installed`; no auto-install. Nothing in authz or runtime is loosened.
- **Call S7-C:** specialists reach SETH only through `builtin.function` keys `seth.*`, executed by `runtime` from a registry injected by `worker_main`, with the run principal; `runtime` never imports `assist`.
- **Call S7-D:** specialists propose; a person accepts through the owning service as themselves. No specialist approves a plan, accepts an appgen draft, applies, reconciles or writes to a client platform.
- **Call S7-E:** `seth.propose.*` write only an `assist_proposals` row, are `write_idempotent` and journaled, idempotent per run step; install sets no approval classes, and a governance rule may still add an approval.
- **Call S7-F:** the board is a read model in `shifde.board` with no table; no board across organizations.
- **Call S7-G:** an engagement is a workspace; no new entity.
- **Call S7-H:** the routing call records usage as `model_call` (B-3920); no ledger migration in SP7.
- **Call S7-I:** specialists see discovery metadata (names, column types) and SETH records only; no `connection.sql` tool and no client row values in v1.
- **Call S7-J:** request text is content: scrubbed by `maintenance purge` and deleted by `maintenance delete`, under a guard trigger.
- **Call S7-K:** rule and explicit routing happen in the API request; only the model fallback is a worker job (`assist.route`), with SP5's crash rule; the same job recovers an interrupted handover.
- **Call S7-L:** one request is one specialist run; no conversation memory and no retry route in v1.
