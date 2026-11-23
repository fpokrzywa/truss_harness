# Sub-project 5: solution generator for Databricks — short design

Date: 2026-11-23. Status: settled after the builder read (section 13); for the lead to log in `DECISIONS.md`. Contract: unified design sections 1 ("the flow"), 5.2 (`appgen`), 5.3, 5.5, 5.6, 6, 7 (calls 2, 7, 8, 9), 9. Builds on SP2 (`2026-11-17-...`), SP3 (`2026-11-19-...`) and the connection management space (`2026-11-22-...`).

## 1. Scope and "done when"

**Done when (design section 6):** "The flow in section 1 works end to end for a read-only agent with a React front end."

The owner input ("the same workspace, with sample data") is not supplied (`.devdata/databricks.env` absent). As in SP2 and SP3, this sub-project is finished against the **fakes** (CHARTER 7, last paragraph): the in-process Databricks workspace of `adapters/dataplatforms/fakes.py`, extended with sample tables and a model-serving endpoint, and the native fake platform of `adapters/native/databricks/fake.py`. The generated app stays `experimental` (`live_validated: false`); STATUS records "live validation waiting on owner input". A live test exists and runs only with `.devdata/databricks.env` and `-m live`.

**In scope:**
- read-only SQL tools over a Databricks connection;
- the new module `shifde.appgen`, which has two parts:
  - drafting: a worker job asks a model to turn an FDE's brief and the connection's discovered tables into a draft. The draft holds an agent, its read-only SQL tools, screens and evaluation cases. A person edits and accepts it, and accepting creates ordinary `build` artifacts and a `packaging` solution version;
  - rendering: the deterministic Databricks App source of a sealed release. It holds a Python service that runs the agent loop, a React front end and the platform files;
- `delivery` deploying the rendered app instead of the SDK template;
- the screens, the demo, the live test and the runbook section.

**Out of scope:**
- tools that write, call outside services or need approval. They stay unsupported by name (design 5.5, call 8);
- pipelines and orchestrations as the entrypoint of a generated app. Only one agent is supported;
- Snowflake (SP6);
- the supervisor and specialists (SP7). The draft job becomes the "build" specialist's tool there;
- telemetry back to SETH (call 2);
- writing the client's usage table (S2, a known gap).

## 2. The flow mapped to modules

| Step (design 1) | What happens | Module (state) |
|---|---|---|
| 1 Client as organization, groups bound | existing | `tenancy`, `identity`, `authz` (done) |
| 2 Databricks connection, tested, discovered | existing; `access_mode = 'read'` required | `connections` (done, CM) |
| 3a Request | FDE submits brief + connection + chosen tables + drafting model | `appgen` (new) |
| 3b Draft | worker job `appgen.draft`: one model call through `ModelService.invoke_at` with a budget hold; output validated against a closed schema | `appgen`, `models_admin`, `ledger` |
| 3c Review and accept | FDE edits the draft (new revision) and accepts. This creates: a prompt, one `connection.sql` tool per query, an agent, all published; an evaluation dataset; a solution and a solution version with `screens` | `appgen` -> `build`, `evaluation`, `packaging` service functions |
| 3d Test in SETH | ordinary SETH runs of the published agent against the connection | `runtime` (done) |
| 4 Seal + evaluate | seal under compiler `/2` (screens non-empty); the existing evaluation gate | `packaging` (change), `evaluation` (done) |
| 5a Plan | `delivery` gathers `Resolved`, `appgen.render` renders the app, it is stored as the artifact, the executor plans | `delivery` (change), `appgen` |
| 5b Approve | second person, unchanged | `delivery` (done) |
| 5c Deploy + verify | executor bundle deploy/run; verify adds `app-deep-health` | `adapters.native` (change), `shifde.executor` (done) |
| 6 Native app at client | Databricks App: agent loop + React, app identity, client-served model; no call to SETH | rendered source |
| 7 Which release where | existing deployments list | `delivery` (done) |

## 3. Modules, layers, worker wiring

**Layers** (`backend/pyproject.toml`, first entries):

```
"shifde.main | shifde.executor | shifde.worker_main",
"shifde.app | shifde.cli",
"shifde.delivery",
"shifde.appgen",
"shifde.evaluation",
"shifde.packaging",
"shifde.runtime",
```

**`appgen` may import:**
- `shifde.core`, `shifde.authz`, `shifde.audit`;
- `shifde.connections.service`/`.schemas`, `shifde.models_admin.service`/`.schemas`;
- `shifde.build.service`/`.schemas`/`.sqlrules`, `shifde.packaging.service`/`.schemas`, `shifde.evaluation.service`/`.schemas`;
- `jsonschema`.

**`appgen` may not import:** `shifde.delivery`, `shifde.app`, `shifde.cli`, `shifde.main`, `shifde.worker_main`, `shifde.executor`, `shifde.adapters.native`, `shifde.runtime`, `flask` or `databricks`.

**Import-linter changes** (made in SP5-2):
1. The layer list above.
2. `shifde.appgen` joins the "api -> service -> repository -> models" containers and the HTTP-free services list.
3. `shifde.appgen` and `shifde.worker_main` join the forbidden lists of the core contract and of both executor contracts.
4. New forbidden contract "appgen.render is pure":
   - source `shifde.appgen.render`;
   - forbidden `sqlalchemy`, `psycopg`, `httpx`, `shifde.core.db`, `shifde.build`, `shifde.connections`, `shifde.models_admin`, `shifde.packaging`, `shifde.evaluation`, `shifde.runtime`, `shifde.appgen.service`, `shifde.appgen.repository`.
5. New forbidden contract "SETH never imports the generated app's libraries": source `shifde`, forbidden `flask`, `databricks` (added in SP5-5 together with the dev dependencies).

**Worker wiring (R1).**
- **Registry.** `shifde/runtime/jobkinds.py` defines:
  - `JobHandler = Callable[[Database, IntegrationRuntime, UUID, UUID], str | None]`, the signature of `connections.run_discover_job`;
  - `JobKindRegistry`: a frozen mapping of kind to handler. It is built with `JobKindRegistry.of({...})`, which raises `ValueError` on a kind that collides with a built-in kind (`run.advance`, `connection.*`, `model.*`).
- **Worker.** `runtime.worker.Worker.__init__` and `runtime.worker.run(argv, job_handlers=None)` gain `job_handlers: JobKindRegistry | None`. `_execute` checks the built-in kinds first, then the registry, and anything else still ends `dead` with `unknown job kind`.
- **Composition root.** New top-layer module `shifde/worker_main.py`:
  - `job_handlers() -> JobKindRegistry` returns `{appgen.service.DRAFT_JOB_KIND: appgen.service.run_draft_job}`;
  - `run(argv=None)` calls `runtime.worker.run(argv, job_handlers=job_handlers())`.
- **Entry point.** `[project.scripts] shifde-worker = "shifde.worker_main:run"`. The command name is unchanged, so `procctl`, the systemd units and `deploy/ubuntu` need no edit.
- **Isolation.** `runtime` never imports `appgen`. In-process workers in tests and demos pass `shifde.worker_main.job_handlers()`.

**`shifde.appgen` contents:**
- `schemas.py`: the request and draft models, `RequestStatus`, `Resolved`, the closed draft schema;
- `repository.py`, `service.py` (request, revise, accept, discard, retry, `run_draft_job`), `api.py` (router included by `shifde.app`);
- `draft.py`: the prompt, the output schema and the validation of one reply;
- `sqlcheck.py`: the pure SQL rules of 5.4;
- `render.py`: pure, `render(manifest, release_digest, resolved) -> dict[str, bytes]`;
- `templates/` (package data):
  - `app.py.txt`, `agent_loop.py.txt`, `app.yaml.txt`;
  - `requirements.in`, `requirements.txt`;
  - `web/`: the built React bundle and its `BUILD_DIGEST`.

**Ported ideas, not code shape:**
- From Agent Factory `0fdd09c` `backend/app/runtime/__init__.py` `Worker`: a bounded loop in which a tool must be on the release and read-only, and arguments are validated against the tool's input schema with `jsonschema`.
- From Agent Factory `backend/app/models/general.py`: visibly synthetic fixture answers, which become the fake serving endpoint.
- **AF's closed `action_schema` is not used.** The generated loop speaks SETH's own tool protocol (`runtime/agent.py` `PROTOCOL`, `parse_tool_calls`, `untrusted_block`), so the agent behaves the same in SETH (where it is evaluated) and in the app (S4).
- From the SDK `1fd902b`, via the SP2 port: Flask plus `databricks-sdk`, `/health` with a bounded `SELECT 1`, and `DATABRICKS_WAREHOUSE_ID` from the `sql-warehouse` resource.
- Nothing else in either source generates applications.

## 4. Tables

All tables are client-owned. Each gets forced RLS, `tenant_isolation` on `tenant_id = current_tenant_id()`, composite `(tenant_id, ...)` FKs, column-wise grants to `shifde_app` (SELECT, INSERT, and UPDATE only on the mutable columns named below), `created_at`/`updated_at`, and owner `shifde_owner`. **No new global table.**

**Migration branch `appgen`:**
- `branch_labels=("appgen",)`, `down_revision="base_0001"`;
- `depends_on` the real heads of `packaging`, `evaluation`, `build`, `connections`, `models_admin`, `runtime`, `tenancy` and `identity` (`uv run alembic heads` at build time);
- one file, `appgen_0001_requests_drafts`, with a downgrade. It also seeds the permissions the `delivery_0001` way.

**`appgen_requests`** (mutable: `status`, `status_reason`, `job_id`, `accept_progress`, `accepted_revision_no`, `solution_version_id`, `accepted_by`, `updated_at`):

| Column | Rule |
|---|---|
| `tenant_id`, `id` | PK (`tenant_id`, `id`) |
| `workspace_id`, `project_id` | NOT NULL; composite FK to the tenancy project (RESTRICT) |
| `connection_id` | NOT NULL; composite FK `connections` (RESTRICT) |
| `discovery_id` | NOT NULL; composite FK `connection_discoveries` (RESTRICT): the discovery whose tables were offered |
| `tables` | jsonb array of 1-50 lowercase `catalog.schema.table` strings (CHECK `jsonb_array_length` 1..50) |
| `brief` | text, CHECK `char_length` 1..4000 |
| `model_id` | uuid, the drafting model; **no FK** (a shared SHI model has `tenant_id` NULL); checked by `models_admin` at request and at call |
| `status` | CHECK in the state table below |
| `status_reason` | text, a code from 5.6, NULL when none |
| `job_id` | uuid, the latest `appgen.draft` job; no FK (runtime reaps jobs) |
| `accept_progress` | jsonb `{prompt_version_id?, tool_version_ids?: {name: id}, agent_version_id?, dataset_id?, solution_id?, solution_version_id?}` |
| `accepted_revision_no` | int, NULL until accepting |
| `solution_version_id` | composite FK `solution_versions` (RESTRICT), set when `accepted` |
| `requested_by`, `accepted_by` | FK to users, as in `delivery_plans` |

**`appgen_drafts`** (immutable after insert, by trigger):

| Column | Rule |
|---|---|
| `tenant_id`, `id` | PK (`tenant_id`, `id`) |
| `request_id` | composite FK `appgen_requests` (CASCADE) |
| `revision_no` | int >= 1; UNIQUE (`tenant_id`, `request_id`, `revision_no`) |
| `source` | `model` / `edited` |
| `draft` | jsonb, CHECK `octet_length(draft::text) <= 262144` |
| `draft_hash` | sha256 of the canonical JSON (`packaging/canonical.py`) |
| `validation` | jsonb list of `{path, code, message, severity: error\|warning}` |
| `created_by` | FK users (the requester for `model` revisions) |

**Request states.** Every transition is audited.

| From | To | Actor | When |
|---|---|---|---|
| (new) | `queued` | user with `appgen:request:write` | create; the job is queued in the same transaction |
| `queued` | `drafting` | worker (`run_draft_job`) | committed **before** the model call |
| `drafting` | `drafted` | worker | the reply parses and validates against the schema; revision 1 is stored (`source=model`). Diagnostics may exist |
| `drafting` | `failed` | worker | `invalid_model_output`, a provider or budget refusal, or `model_call_unknown` (5.6) |
| `failed` | `queued` | user with `appgen:request:write` | `retry`: a new job and a new model call |
| `drafted` | `accepting` | user with `appgen:draft:accept` | accept starts (5.5) |
| `accepting` | `accepted` | the same | every accept step is done |
| `drafted`, `failed` | `discarded` | user with `appgen:request:write` | discard |

- `accepting` stays `accepting`, with a `status_reason`, if an accept step fails. A repeated accept of the same revision resumes it.
- Revisions are added only while `drafted`.

**No migration elsewhere:**
- `solution_versions.spec.screens` already exists;
- `delivery_targets.bindings` is jsonb;
- the artifact stays in `delivery_artifacts`.

If an issue finds a needed column missing, it adds a new migration in its own branch and never edits an existing one.

**Permissions:**
- `appgen:request:read|write` and `appgen:draft:accept`;
- owners, admins and FDEs (`DEVELOPER`) get all three;
- accepting also needs, checked by each owning service:
  - `build` create and publish for prompts, tools and agents;
  - `evaluation:dataset:write`;
  - `packaging:solution:write`.

## 5. Drafting

### 5.1 Request

`POST /api/v1/orgs/{org}/workspaces/{ws}/projects/{project}/app-requests` with body `{connection_id, tables[], brief, model_id}`.

Checks, in order:
1. The caller has `appgen:request:write` on the project.
2. The connection is visible to the caller and in the project's scope (`packaging.service.in_scope`). Otherwise 404 `connection not found`.
3. `adapter_key == 'databricks'` (else 422 `connection_not_databricks`).
4. `access_mode == 'read'` (the stored field of `connections`; else 422 `connection_not_read_only`).
5. `status == 'active'` (else 422 `connection_not_active`).
6. `connections.service.latest_discovery(connection_id)` exists with `kind == 'schema'` (else 422 `connection_not_discovered`).
7. Each requested table, lowercased, equals `f"{t['catalog']}.{t['schema']}.{t['name']}".lower()` for some `t` in that discovery's `payload["tables"]` (else 422 `table_not_discovered`, `loc` naming the table).
8. The model passes `models_admin`'s existing visibility and per-client SHI-switch checks. The refusal is unchanged (answered like an unknown model).

Then the service:
- inserts the request with `discovery_id` set to the discovery used;
- queues one `shifde.jobs` row with `kind='appgen.draft'`, `payload={request_id, principal_id, principal_type}` and `dedupe_key=f"appgen.draft:{request_id}:{n}"`, using the `connections.repository.enqueue_job` pattern;
- answers 202 with the request.

### 5.2 Job

The job runs as `appgen.service.run_draft_job(database, runtime, tenant_id, job_id)`. It follows the `run_discover_job` pattern: it sets the tenant context and re-derives the requester and their permissions from the database.

1. Load the request.
   - If it is `drafted`, `accepting`, `accepted`, `failed` or `discarded`, return None (idempotent).
   - If it is `drafting`, an earlier attempt may have reached the provider. Set `failed` with `model_call_unknown` and do not call again (5.6).
2. Re-check permission and connection.
   - A permission lost since the request gives `failed` with `access_revoked`.
   - A connection that is no longer active, read and Databricks gives `failed` with `connection_unavailable`.
   - Then set `drafting` and commit.
3. Make the call: `ModelService(scope, runtime, database).invoke_at(ResourceRef(tenant_id, workspace_id, project_id, "appgen_request", request_id), model_id, InvokeIn(messages=[system, user], max_tokens=8000), usage_event_type="appgen_draft")`.
   - `system` is the fixed text `draft.SYSTEM_PROMPT`, which includes the JSON schema.
   - `user` is a JSON document `{brief, tables: [{name, columns: [{name, type}]}]}` built from the stored discovery. **Row values are never sent.**
   - This path places and settles the budget hold, writes the usage row in the client and applies the per-client SHI switch (SP3 section 7).
4. Parse and validate the reply.
   - Parse `out.output` as one JSON object; a single surrounding fenced code block is stripped.
   - Validate it with `jsonschema` against `draft.DRAFT_SCHEMA`.
   - A failure sets `failed` with `invalid_model_output`; the jsonschema messages (at most 20, each at most 300 characters) go into a revision-less `status_reason` detail in the audit row.
   - On success, run the 5.4 diagnostics, insert revision 1 and set `drafted`.
5. Return.
   - If `invoke_at` raises an `AppError`, set `failed` with `status_reason` equal to that error's `code` and return None, so the job ends `done` (the failure is recorded on the request).
   - The job never re-asks the model and never retries the call.

### 5.3 Draft schema

The schema is closed: `additionalProperties: false` at every level.

- `agent`: `{name: slug ^[a-z][a-z0-9-]{1,62}$, instructions: string 1..8000}`.
- `tools`: 1..8 items, each:
  - `name`: slug `^[a-z][a-z0-9_]{1,62}$`;
  - `description`: 1..500;
  - `sql`: 1..8000;
  - `parameters`: 0..10 items of `{name: ^[a-z_][a-z0-9_]{0,62}$, type: "string"|"integer"|"number"|"boolean"|"date", required: bool}`;
  - `max_rows`: 1..1000, default 200.
- `screens`: 1..6 items, one of:
  - `{kind: "chat", title: 1..80, placeholder: 0..200}`, at most one;
  - `{kind: "table", title: 1..80, tool: <tool name>, inputs: [<parameter names of that tool>]}`.
- `evaluation_cases`: 1..50 items of `{case_id, input: {message: 1..4000}, assertions: [evaluation.schemas.Assertion] (1..10)}`.
  - The type is imported from `shifde.evaluation.schemas.Assertion`, so the kinds and rules are SP3's.
  - The JSON schema is generated from a pydantic model `appgen.schemas.Draft` (`Draft.model_json_schema()`), and the same model validates edits.

### 5.4 Draft validation (`appgen/sqlcheck.py`)

Validation produces diagnostics and does not throw. It is stored with every revision. Each item below is an `error` unless marked otherwise.

**Tokenizer** (pure Python, no parser dependency, S5):
- Single-quoted strings (`''` escapes) and double-quoted strings are opaque tokens.
- Backtick-quoted identifiers are identifiers, with backticks removed and lowercased.
- Bare identifiers match `[A-Za-z_][A-Za-z0-9_]*` and are lowercased.
- `:name` outside a string is a parameter token.
- `.`, `,`, `(`, `)` and `;` are punctuation. Everything else is skipped.

**Statement rules:**
- `--` or `/*` outside a string: `sql_comment`.
- More than one statement (any `;` that is not the last token): `sql_multiple_statements`.
- First keyword not `select` or `with`: `sql_not_select`.
- Any of `insert update delete merge create alter drop truncate grant revoke copy put get call optimize vacuum set use refresh msck analyze cache uncache restore` as a bare identifier token: `sql_write_statement`.

**Table references.** A table reference is the name after `from` or `join` (comma-separated lists after `from` included).
- A three-part `a.b.c` reference must be in the request's `tables`; otherwise `sql_table_not_allowed`.
- A two-part name is `sql_table_not_qualified`.
- A one-part name is `sql_table_not_qualified` unless it is a CTE name defined in the statement's `with`.
- A name followed by `(` (a table-valued function such as `read_files`) is `sql_table_function`.
- Any other three-part identifier anywhere (for example in a subquery) is also checked against `tables`.

**Parameters:**
- Parameter tokens must equal the declared names exactly. Otherwise `sql_parameter_undeclared` or `sql_parameter_unused`.

**Other rules:**
- A table screen's `tool` must exist (`screen_tool_unknown`), and its `inputs` must be parameters of that tool (`screen_input_unknown`).
- Duplicate tool, screen title or case id: `duplicate_name`.
- An evaluation case whose expected text is empty is a `warning` (`case_trivial`).

**Edit and accept routes:**
- **Edit:** `POST /api/v1/orgs/{org}/app-requests/{id}/drafts` with `{base_revision_no, draft: Draft}`. It stores `edited` revision n+1.
  - 409 `stale_revision` if `base_revision_no` is not the latest;
  - 409 `request_not_editable` unless the request is `drafted`;
  - 422 for schema errors (pydantic).
  - Diagnostics never block a save.
- **Accept:** see 5.5.

### 5.5 Accept

`POST /api/v1/orgs/{org}/app-requests/{id}/accept` with body:

```
{revision_no, solution_name, model_id,
 thresholds: {min_accuracy, max_p95_latency_ms, max_total_cost_usd}}
```

- `model_id` is an organization model used as the agent's `model` in SETH runs and evaluation (S8).
- It is **not** the drafting model: `appgen_requests.model_id` is never pinned anywhere.
- The release's model binding is given later at seal (SP3), and a generated app has exactly one (5.7 below).

**Refusals:**
- 409 `already_accepted` (status `accepted`);
- 409 `request_not_acceptable` (status not `drafted`, or `accepting` with another `revision_no`);
- 422 `draft_invalid` (the revision has an `error` diagnostic).

**Transaction rule (R15).** The `build`, `evaluation` and `packaging` services each commit their own transaction (`build/service.py` and `evaluation/service.py` call `session.commit()`), so accept cannot be one transaction. It is a resumable sequence:

0. **Pre-check.** Before any write, the caller's PDP decisions for every permission in section 4 are checked on the project. A missing one is 403, and nothing is written.
1. Set `accepting` and `accepted_revision_no`, then commit.
2. Then, each step skipped if its id is already in `accept_progress` and recorded there after it commits:
   1. create and publish a prompt, with `sections.system` = `agent.instructions`;
   2. create and publish one `connection.sql` tool per draft tool, with:
      - `side_effect_class: "read"`;
      - binding `{connection_id, sql_template, max_rows}`, where `sql_template` is the draft SQL with `:name` rewritten to `%(name)s`;
      - an input schema built from the parameters: `integer` -> `integer`, `number` -> `number`, `boolean` -> `boolean`, `date` -> `string` with `format: date` and pattern `^\d{4}-\d{2}-\d{2}$`, `string` -> `string` with `maxLength` 1000;
   3. create and publish an agent, with:
      - `prompt_version_id`, `model`: `model_id`, `tool_version_ids`;
      - `limits {max_iterations: 8, max_tool_calls: 8, max_tokens: 200000, deadline_s: 60, max_cost: thresholds.max_total_cost_usd}`;
   4. create an evaluation dataset version from the cases;
   5. create the solution `solution_name` in the project, or reuse an active solution of that name;
   6. create a solution version with:
      - `entrypoint_kind: "agent"`;
      - `data_reads: [{connection_id, tables}]`;
      - `evaluation: {dataset_id, thresholds}`;
      - `screens` from the draft.
3. Set `accepted` and `solution_version_id`.

A failed step leaves the request `accepting`, with `status_reason` = the step's error code, and the objects already created stay (they are ordinary drafts or published versions in the project). Repeating the accept with the same `revision_no` resumes from there.

### 5.6 Error codes and failure reasons

| Where | Code | HTTP / status |
|---|---|---|
| create | `connection not found` | 404 |
| create | `connection_not_databricks`, `connection_not_read_only`, `connection_not_active`, `connection_not_discovered`, `table_not_discovered` | 422 |
| any route, other client / other project / unknown | `app request not found` | 404 |
| drafts | `stale_revision`, `request_not_editable` | 409 |
| accept | `already_accepted`, `request_not_acceptable` | 409 |
| accept | `draft_invalid` | 422 |
| retry | `request_not_failed` | 409 |
| discard | `request_not_discardable` | 409 |
| job -> `failed` | `invalid_model_output`, `model_call_unknown`, `access_revoked`, `connection_unavailable`, or the `code` of the `AppError` raised by `invoke_at` (budget, provider, SHI switch) | request `status_reason` |

**Crash rule (R8):** a worker that dies after setting `drafting` leaves the request `drafting`.
- The re-leased or retried job sees `drafting` and sets `failed` with `model_call_unknown` **without calling the model**.
- The hold placed by `invoke_at` is settled by the existing invocation-hold sweep (`ledger.reservations.settle_orphaned_invocations`): `settled_unknown` at the held amount if the provider request may have started, `released` otherwise.
- There is no double bill: there is at most one provider call per job, and `retry` is a person's decision that makes a new call.

**Fake model (R6, R30).** The deterministic model is the existing `fake` provider (`adapters/model_providers.py` `FakeProvider`). Its config `script: [{match, reply}]` is matched as a substring against the last user message; otherwise it replies `fake: <last>`. SP5-1 defines the shared fixtures in `backend/tests/_support/appgen_fixtures.py`:
- `SAMPLE_DRAFT`: a valid draft with 2 tools, a chat screen, a table screen and 3 cases;
- `DRAFT_SCRIPT`: one entry matching the marker `[[appgen-sample]]` in the brief and replying `json.dumps(SAMPLE_DRAFT)`, plus an entry for `[[appgen-invalid]]` replying `not json`;
- `AGENT_SCRIPT`: entries that make the published agent answer each case in SETH's tool protocol. The first reply is `{"tool_calls":[{"name":...,"arguments":...}]}`, matched on the case message. The second reply is the final text, matched on a substring of the `<untrusted ...>` tool-result block.

The same `AGENT_SCRIPT` list, in the same `{match, reply}` format and with the same matching rule, configures the fake serving endpoint (6.1). SP5-3's evaluation tests, SP5-5's conformance test and SP5-9's demo all import these fixtures.

## 6. Rendering and deployment

### 6.1 SQL tools over Databricks (SP5-1)

- **`build/executors.py`:**
  - `CONNECTION_TYPES_OF = {"connection.sql": frozenset({"database", "data_platform"}), ...}` replaces the single type (`build/tools.py` compares with `in`);
  - new `DATA_PLATFORM_ADAPTER_OF = {"databricks": ("databricks", "query.sql")}`, so the tool's derived maturity comes from the Databricks adapter, which is `experimental`;
  - a `connection.sql` tool on a `data_platform` connection whose adapter is not in that map is `executor_adapter_unsupported` ("connection.sql supports Databricks data platform connections only").
- **Parameters (R4).**
  - The stored template keeps `build`'s `%(name)s` form (`build.tools.PARAM_NAME`), so `build` validation is unchanged.
  - For a `data_platform` connection, `build/tool_runtime.sql_request` replaces each `%(name)s` with `:name` and adds `param_types` from the tool's input schema: `integer` -> `BIGINT`, `number` -> `DOUBLE`, `boolean` -> `BOOLEAN`, `string` with `format: date` -> `DATE`, other `string` -> `STRING`.
  - `connections.schemas.SqlInvokeIn` gains `param_types: dict[str, Literal["STRING","BIGINT","DOUBLE","BOOLEAN","DATE"]] | None = None`.
  - `adapters/dataplatforms/databricks.py` sends `parameters: [{name, value: None if v is None else str(v), type}]` to the statement execution API, with `row_limit = max_rows`.
- **Row limit (R14).** The tool result is `{columns: [{name, type}], rows: [[...]], row_count, truncated}`; `truncated` comes from the statement API's `manifest.truncated`, true when more rows existed than `max_rows`. If the existing `data_platform` invoke result differs, its shape is kept and the generated app copies it exactly; the conformance test compares them.
- **Write statements (R13)** are refused before reaching the platform in two places:
  - `sqlcheck` at draft time;
  - `build` validation at tool publish, which runs `sqlcheck.statement_rules` on the converted template and gives `sql_write_statement` and the other statement-rule codes as build `report.error` with path `binding.sql_template`. `build` may not import `appgen`, so the pure statement rules live in `build/sqlrules.py` and `appgen/sqlcheck.py` imports them from there.

  The read-only credential (`access_mode = 'read'`) and the app identity's SELECT-only grants remain the real guarantee.
- **Fake workspace with sample data.** `FakeDatabricksWorkspace(sample_data=True)`:
  - loads an in-memory SQLite database (`PRAGMA query_only = ON` after load) with `main.default.customers` (id, name, region; 6 rows), `main.default.orders` (id, customer_id, amount, ordered_on; 12 rows) and `main.sales.invoices` (id, order_id, total; 12 rows), named `main__default__customers` and so on;
  - answers any statement that passes `sqlrules` by rewriting `a.b.c` to `a__b__c` and `:name` to SQLite named parameters;
  - leaves `SELECT 1`, `fake_rows` and the existing switches unchanged;
  - serves `POST /serving-endpoints/{name}/invocations` (OpenAI chat format) from `serving={name: script}` with the `FakeProvider` matching rule. The reply is `choices[0].message.content` with `usage {prompt_tokens, completion_tokens}` counted as words;
  - answers `GET /api/2.0/serving-endpoints/{name}` with `state.ready = READY`;
  - gains fault switches `endpoint_not_ready` and `table_denied:<c.s.t>`.

### 6.2 Compatibility check and compiler version (`packaging`, SP5-3; R18, R19)

The compiler version is chosen at seal time:
- **`shifde.packaging/1`** when `spec.screens` is empty. The manifest is byte-for-byte today's (no `screens`, no `app` keys), so existing and new `/1` digests are unchanged and `/1` releases keep deploying the SDK template.
- **`shifde.packaging/2`** when `spec.screens` is non-empty. The manifest adds:
  - `screens`;
  - `app: {generator_version: appgen.render.GENERATOR_VERSION}`, a string `"shifde.appgen/<major>.<minor>"`, initially `"shifde.appgen/1.0"`.

  Every `/2` release renders an app.

The `/2` diagnostics (all mandatory):
- `entrypoint`: must be an agent ("a generated app runs one agent");
- `screens.<i>`: kind on the closed list; a table screen's `tool` names a tool pinned by the agent and its `inputs` are that tool's parameters;
- `models`: exactly one model pin ("a generated app uses exactly one model");
- `tools.<pin>`: `connection.sql` on a Databricks `data_platform` connection.

`SolutionVersionCreate` gains `screens: list[Screen] = []`, where `Screen` is the pydantic union of 5.3, imported from `packaging.schemas` and defined there, since `appgen` imports `packaging`.

### 6.3 Resolved and render (R20, R21)

`delivery/plans.py` gathers `appgen.schemas.Resolved` for a `/2` release with `build`'s service read functions, under the plan requester's scope. Each pinned version's `spec_hash` must equal the manifest's, or the plan is refused with 409 `release_source_changed`.

```
Resolved = {
  agent: {version_id, spec_hash, name, instructions (prompt sections.system),
          limits: {max_iterations, max_tool_calls, max_tokens, deadline_s}},
  tools: [{version_id, spec_hash, name, description, sql (":name" form),
           parameters: [{name, type, required}], input_schema, max_rows}]  (sorted by name),
  model: {serving_endpoint}          (= the release's single model binding `model`),
  screens: [...]                     (= manifest screens)
}
```

**`appgen.render.render(manifest, release_digest, resolved) -> dict[str, bytes]`:**
- It is pure and deterministic: no clock, no randomness, sorted JSON keys, fixed archive timestamps (via `delivery`'s `create_archive`).
- It refuses a manifest whose `app.generator_version` differs from `GENERATOR_VERSION` with `GeneratorVersionUnsupported`, which `delivery` maps to 409 `generator_version_unsupported`; re-sealing gives a new release.
- It renders these files:
  - `databricks.yml`;
  - `app/app.yaml` (`command: ["python","app.py"]`, env `DATABRICKS_WAREHOUSE_ID` from `sql-warehouse` and `SERVING_ENDPOINT` from `serving-endpoint`);
  - `app/app.py`, `app/agent_loop.py`;
  - `app/requirements.txt`: the committed, hashed lock copied byte for byte. It is compiled from `templates/requirements.in` (`Flask==3.1.3`, `databricks-sdk==0.163.0`) with the SP2 command (`uv pip compile --universal --python-version 3.12 --generate-hashes`), and render never resolves dependencies;
  - `app/config/agent.json`, `tools.json`, `screens.json`, `limits.json`;
  - `app/static/**` from `templates/web/`;
  - `release.json` (as in SP3).
- The archive is tested for:
  - determinism (same inputs, same bytes and digest);
  - no SETH origin, no `shx_`/`sk-` patterns and no hit from the secret-scan rules;
  - the 5 MB cap.

### 6.4 Generated service (SP5-5; owns the deep-health shape, R29)

`agent_loop.py` is standalone (about 250 lines, no `shifde` import):
- It builds messages the way `runtime/agent.py` does:
  - system = instructions + `PROTOCOL`;
  - user = the question;
  - each tool result is a user message made by `untrusted_block(source, step, value)`.
- It parses replies with a verbatim copy of `parse_tool_calls`. A test asserts that the template's `PROTOCOL` equals `shifde.runtime.agent.PROTOCOL` and that both `parse_tool_calls` give the same result on a fixed corpus.
- Limits come from `limits.json`, which copies the agent's `max_iterations`, `max_tool_calls` and `deadline_s`.
- A tool call to an unknown tool, or with arguments failing `input_schema` (`jsonschema`), stops the run with `invalid_tool_call`. Tool results are capped at 256 KiB.
- Models are called through `WorkspaceClient().serving_endpoints.query(name=SERVING_ENDPOINT, messages=...)` and tools through `statement_execution.execute_statement(warehouse_id, statement, parameters, row_limit, wait_timeout="30s")`, all under the app identity.
- Each answer logs one stdout line `{"event":"usage","prompt_tokens":n,"completion_tokens":n,"endpoint":name}`.

**Routes:**
- `POST /api/ask` with `{message}` returns `{answer, tool_calls: n}`;
- `POST /api/tools/<name>` with `{params}` returns the tool result shape of 6.1;
- `GET /` and `/static/*` serve the React bundle;
- `GET /health` is unchanged from the template;
- `GET /health?deep=1` returns 200 if every check passed, else 503:

```
{"status": "ok"|"failed", "generator_version": "shifde.appgen/1.0",
 "release_digest": "<64 hex>",
 "checks": [{"name": "warehouse"|"serving_endpoint"|"table:<c.s.t>",
             "status": "passed"|"failed", "reason": str|null (<= 200 chars)}]}
```

The deep-health checks are:
- `warehouse`: `SELECT 1`;
- `serving_endpoint`: endpoint `state.ready == "READY"`;
- one `table:<c.s.t>` per data-read table: `SELECT * FROM <t> LIMIT 0`, with reason `the app cannot read <t>`.

### 6.5 React front end (SP5-4)

- **Location.** `frontend/appgen-web/` (package `@shifde/appgen-web`; React 19, TypeScript, Vite, Vitest, ESLint), with a new `frontend/pnpm-workspace.yaml` listing `"."` and `"appgen-web"`.
  - The lockfile stays `frontend/pnpm-lock.yaml`.
  - The frontend's own ESLint, tsconfig and Vitest configs exclude `appgen-web/`.
  - The frontend depends on it as `"@shifde/appgen-web": "workspace:*"` (used by the preview, 8).
- **Behaviour.** It reads `config/screens.json` at runtime and draws `chat` (posts to `/api/ask`) and `table` (a form of the screen's inputs, posting to `/api/tools/<tool>`, with rows in a table and a "truncated" note).
- **Build.** Vite is set to fixed file names (`entryFileNames: "assets/[name].js"`, no hashes, `base: "./"`).
- **Committed bundle.** `python scripts/appgen_web.py` builds it and copies `dist/` into `backend/src/shifde/appgen/templates/web/` with `BUILD_DIGEST`, the sha256 over the sorted `(path, sha256)` lines. `--check` rebuilds into a temporary directory and fails on a mismatch.
- **Gates.** `scripts/gates.py` gains `pnpm --filter @shifde/appgen-web lint|typecheck|test|build` and `python scripts/appgen_web.py --check`. `factory/checks.sh` is protected, so the lead adds the same lines there (lead follow-up, recorded in STATUS).

### 6.6 Delivery and native adapter (SP5-6; R23-R25)

- **Target bindings.** `delivery.schemas.TargetBindings` gains `serving_endpoint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,63}$")`. It is optional, and a template plan ignores it.
- **`PlanService.request`** for a `/2` release, after SP3's gates, refuses with 409 in this order:
  1. `serving_endpoint_required`: the target has no `serving_endpoint`.
  2. `serving_endpoint_mismatch`: the release's single model binding `model` differs from the target's `serving_endpoint`.
  3. `model_not_in_target_workspace`: the binding's provider must be either:
     - an `openai_compatible` provider whose `base_url`, put through `adapters.native.addresses.normalize_databricks_address` applied to its scheme and host only (path ignored), equals the target's normalized address;
     - or, in `SHIFDE_ENV` `development`/`test`, a `fake` provider.

     The provider's `base_url` is only compared, never written into the artifact. The endpoint is a Databricks serving endpoint addressed by name.

  Then it gathers `Resolved`, renders, stores the artifact and adds `generator_version` to the plan digest input. `/1` releases keep `template_artifact`.
- **`adapters/native/databricks/source.py`:**
  - `validate_source` accepts the generated `app/**` files and the generated `app.yaml` (compared with `render`'s output, carried in the artifact);
  - `deployment_bundle` adds the resource `{name: "serving-endpoint", serving_endpoint: {name, permission: "CAN_QUERY"}}` when the bindings name one;
  - hooks, includes, hidden files and files outside `app/` are still refused.
- **`verify`** adds `app-deep-health`. It calls `<app url>/health?deep=1` with the existing health-origin rules and passes only if:
  - the response is 200 with `status == "ok"`;
  - `release_digest` equals the plan's `release_digest`;
  - every check passed.

  Failed checks' reasons are copied into the check detail. `verified` needs all three checks.
- **The native fake** answers deep health from the release in the artifact and gains a `deep_health_bad` fault.
- The state machine, the journal and unknown outcome are unchanged.

## 7. API routes

All routes are under `/api/v1/orgs/{org}`, are SCOPED and PDP-checked, and are classified in `tests/authz/scoping/test_workspace_routes.py`. Each has a cross-client case (A's user asks for B's object: 404) and a cross-project case (a P1-only member asks for P2's: 404).

| Route | Does | Response |
|---|---|---|
| `POST /workspaces/{ws}/projects/{project}/app-requests` | create, queue the draft job | 202 `AppRequestOut` |
| `GET /workspaces/{ws}/projects/{project}/app-requests?status=&limit=50&cursor=` | list, newest first; `limit` 1..200; the `paginate`/`encode_cursor` pattern of `delivery/plans.py` | 200 `Page[AppRequestSummary]` |
| `GET /app-requests/{id}` | the request and all revisions | 200 `AppRequestOut` |
| `POST /app-requests/{id}/drafts` | new edited revision | 201 `DraftOut` |
| `POST /app-requests/{id}/accept` | accept (5.5) | 200 `AppRequestOut` |
| `POST /app-requests/{id}/discard` | discard (audited) | 200 `AppRequestOut` |
| `POST /app-requests/{id}/retry` | `failed` only: new job | 202 `AppRequestOut` |
| `GET /releases/{id}/app-preview` | `/2` releases only (else 409 `release_has_no_app`); permission: packaging's release read (`packaging:solution:read` via `packaging.service.scoped_release`) | 200 `AppPreviewOut` |

Response schemas:
- `AppRequestSummary`: `{id, workspace_id, project_id, connection_id, status, status_reason, latest_revision_no, solution_version_id, requested_by, created_at, updated_at}`.
- `AppRequestOut`: the summary fields plus `{tables, brief, model_id, discovery_id, accepted_revision_no, accept_progress, revisions: [DraftOut]}`.
- `DraftOut`: `{revision_no, source, draft, draft_hash, validation, created_by, created_at}`.
- `AppPreviewOut`: `{release_id, release_digest, generator_version, files: [{path, size, sha256}], screens, tools: [{name, description, sql, parameters, max_rows}]}`. It renders with `Resolved` gathered under the caller's scope, never returns archive bytes, and gives 409 `release_source_changed` as at plan time.

Changed routes:
- SP3's solution-version create accepts `screens`;
- SP2's target create and patch accept `bindings.serving_endpoint`.

Audit events: `appgen.request.created|drafting|drafted|failed|retried|discarded`, `appgen.draft.revised`, `appgen.request.accepting|accepted`. Refresh `backend/openapi.json` after these changes.

## 8. Screens (R27)

Under `frontend/src/app/(app)/o/[org]/`, with Vitest tests and hand-written types:
- **`solutions/generate`** is the form: project, Databricks connection (only the caller's visible ones with `access_mode` read), tables from the latest discovery, brief, drafting model, and "Generate draft".
- **`solutions/generate/[id]`:**
  - Status: polls `GET /app-requests/{id}` every 3 s while `queued`, `drafting` or `accepting`, and stops on any other status or after 10 minutes with "Still working; refresh to check again".
  - The draft editor: instructions, tools with SQL and parameters, screens, cases, with diagnostics inline and a revision history.
  - Accept: solution name, runtime model, thresholds. It is disabled with the reason while errors remain.
  - Discard and Retry.
- **`solutions/generate`** also gets a list tab (the paged list).
- **`solutions/[id]`** (existing) shows each version's screens. For each version it calls the existing `GET .../solutions/{id}/versions/{vid}/releases` and links each `/2` release to `releases/[id]`.
- **`releases/[id]`** (existing) gains "App preview": the file list and the screens drawn by `@shifde/appgen-web` with static sample rows (no calls).
- **Deliver:** the target form gets an optional serving endpoint field, and the plan form shows the three refusals of 6.6 in plain words.

## 9. Isolation rules and tests

1. **One organization, one project.** A request, its drafts and its job live in one organization and one project. Every route and the job run under that organization's RLS context, and the job re-derives the requester. Tests:
   - cross-client 404 on every route;
   - cross-project 404;
   - `shifde_app` cannot select another tenant's `appgen_*` rows (SQL-level).
2. **Connections and tables.** Only the requester's visible, in-scope, `access_mode = 'read'` Databricks connection can be named, and only tables from its latest discovery. Tests:
   - another client's or another project's connection gives 404;
   - a `read_write` connection gives 422;
   - an undiscovered table gives 422.
3. **Model call.** It goes through `invoke_at`: the SHI switch, project budget, hold and usage row stay in the client, and only metadata is sent. Tests:
   - SHI provider off gives the `models_admin` refusal and no job;
   - an exhausted budget gives `failed` with the budget code and no provider call;
   - a crash after `drafting` gives `model_call_unknown`, one provider call and the hold `settled_unknown`;
   - the fake records the prompt and it contains no sample row value.
4. **Accept.** It writes only in the request's organization and project, through each owning service. Tests:
   - a caller with `appgen:draft:accept` but without `build` publish gets 403 and nothing is written (the pre-check);
   - a failure injected after step 2 resumes on the repeated accept without duplicate objects.
5. **The rendered app.** It holds no SETH address and no credential, and is checked against the release digest. SP2's executor rules hold. Tests:
   - the archive scan;
   - determinism;
   - organization B's executor leases nothing of A's generated-app plan.
6. **Schema test.** The schema test covers both new tables. If `backend/tests/schema/*` needs an entry, the builder writes it in build.md and the lead adds it (B-2530).

## 10. Done-when check

`python scripts/solution_demo.py --fake` follows the style of `release_demo.py`:
- in-process API, worker (with `shifde.worker_main.job_handlers()`) and executor runner, with no network;
- a scratch database `shifde_demo_<hex>`, dropped at the end, also on failure;
- the test helpers and `tests/_support/appgen_fixtures.py`.

It runs these steps:
1. Org A has project P. A Databricks connection to `FakeDatabricksWorkspace(sample_data=True, serving={"agent-endpoint": AGENT_SCRIPT})` is tested and discovered. There are two `fake` providers: one scripted with `DRAFT_SCRIPT` (drafting), and one scripted with `AGENT_SCRIPT` (runtime).
2. The FDE requests a draft (brief with `[[appgen-sample]]`). The worker drafts it and the request is `drafted`. There is a settled hold and a usage row of type `appgen_draft`.
3. The FDE edits one tool's `max_rows` (revision 2) and accepts. The prompt, the 2 tools, the agent, the 3-case dataset and the solution version with 2 screens exist.
4. A SETH run of the agent answers case 1.
5. The FDE seals a release under `/2` with the model binding `model = "agent-endpoint"`. A plan is refused with `evaluation_required`. The evaluation reaches `passed`.
6. The target, with `serving_endpoint: "agent-endpoint"`, accepts the plan. Rendering twice gives the same digest.
7. Self-approval is refused (403). A second admin approves.
8. Apply, observe and verify (including `app-deep-health`) succeed on the native fake, and the deployment is `verified`.
9. The demo unpacks the artifact and runs the generated `app.py` in-process (Flask test client; `databricks-sdk` with an `httpx`/`requests` transport routed to the fake workspace):
   - `POST /api/ask` with each case gives an answer passing its assertions;
   - `/api/tools/<table tool>` returns sample rows;
   - `/health?deep=1` is 200;
   - `GET /` serves `index.html`;
   - no SETH origin is in the archive.
10. Client B gets 404 on the request, the draft, the release preview and the plan.

The demo ends with `SOLUTION DEMO PASSED` (exit 0), or `SOLUTION DEMO FAILED: <step>` (1).

**R32.** `flask==3.1.3` and `databricks-sdk==0.163.0` enter only:
- `appgen/templates/requirements.in`/`.txt`;
- the backend `dev` dependency group, added in SP5-5, which first runs the generated app.

A test asserts that the dev-group pins equal `requirements.in`. Contract 5 of section 3 keeps `src/shifde` from importing them.

Also part of the check:
- `uv run pytest tests/appgen -m live` skips with "live validation waiting on owner input: no Databricks workspace";
- the runbook `docs/runbooks/databricks-live-validation.md` gains a "Generated app" section: sample tables, serving endpoint, the grants for the app's service principal (S6), then the demo steps live.

## 11. Factory issues (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`. Below are the issue-specific commands.

- **C** = isolation-critical (`module-builder-critical`, isolation review).
- Migrations are new files under a protected path, so the approver escalates and the lead merges after the 13-gate set (B-2530).

1. **SP5-1 SQL tools over a Databricks connection; fake workspace with sample data and serving endpoint; shared fixtures (C).**
   - Scope: 6.1; `build/sqlrules.py`; `tests/_support/appgen_fixtures.py` (5.6).
   - Migration: no.
   - Checks: `uv run pytest tests/build tests/connections tests/adapters/dataplatforms tests/runtime -k "sql or databricks or fake"`.
   - Depends: none.
2. **SP5-2 appgen skeleton, requests, draft job, worker job-kind registry (C).**
   - Scope:
     - the package skeleton and `RequestStatus`;
     - the import-linter changes 1-4;
     - `runtime/jobkinds.py` and `shifde/worker_main.py`, with the entry point moved;
     - `appgen_0001`;
     - routes create, list, get, drafts, discard, retry;
     - `draft.py`, `sqlcheck.py`;
     - the 9.1-9.3 tests.
   - Migration: yes.
   - Checks: `uv run pytest tests/appgen/test_requests.py tests/appgen/test_draft_job.py tests/appgen/test_sqlcheck.py tests/appgen/test_routes_cross_client.py tests/runtime/test_jobkinds.py tests/schema; uv run shifde-worker --help`.
   - Depends: 1.
3. **SP5-3 appgen accept; packaging screens and compiler `/2` (C).**
   - Scope: 5.5, 6.2, the 9.4 tests; `/1` digests unchanged (test against a stored SP3 manifest).
   - Migration: no.
   - Checks: `uv run pytest tests/appgen/test_accept.py tests/packaging tests/evaluation tests/schema`.
   - Depends: 1, 2.
4. **SP5-4 appgen-web: React screen renderer and committed bundle.**
   - Scope: 6.5, including `frontend/pnpm-workspace.yaml` and the `scripts/gates.py` lines.
   - Migration: no.
   - Checks: `cd frontend; pnpm install --frozen-lockfile; pnpm --filter @shifde/appgen-web lint; pnpm --filter @shifde/appgen-web typecheck; pnpm --filter @shifde/appgen-web test; pnpm --filter @shifde/appgen-web build; pnpm lint; pnpm typecheck; pnpm test; pnpm build`; `python scripts/appgen_web.py --check`; `python -m unittest discover -s scripts/tests`.
   - Depends: 2.
5. **SP5-5 appgen render, generated service, deep-health shape, conformance (C).**
   - Scope: 6.3, 6.4; the dev group and contract 5; the protocol-parity test; the generated service run in-process on the fakes over the fixture dataset.
   - Migration: no.
   - Checks: `uv run pytest tests/appgen/test_render.py tests/appgen/test_generated_app.py tests/appgen/test_protocol_parity.py`.
   - Depends: 1, 3, 4.
6. **SP5-6 delivery and adapters.native deploy the generated app (C).**
   - Scope: 6.6, consuming 6.4's deep-health shape; e2e on the fake.
   - Migration: no.
   - Checks: `uv run pytest tests/delivery tests/executor tests/adapters/native tests/schema; python scripts/release_demo.py --fake; python scripts/delivery_demo.py --fake`.
   - Depends: 5.
7. **SP5-7 Front end: generate, review, accept; solution screens.**
   - Scope: section 8, except preview and Deliver.
   - Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`.
   - Depends: 3, 4.
8. **SP5-8 Front end: app preview, target serving endpoint, plan refusals.**
   - Checks: as 7.
   - Depends: 6, 7.
9. **SP5-9 Done-when demo, live test and runbook.**
   - Scope: section 10; `tests/appgen/test_generated_app_live.py` (`-m live`, reads `.devdata/databricks.env` itself); the runbook section; a "Solution demo" line in `CLAUDE.md` "Local commands".
   - Checks: `python scripts/solution_demo.py --fake; uv run pytest tests/appgen -m live` (reports skipped).
   - Depends: 6, 8.

## 12. Open questions

None open: every question is settled in section 13. S2 still needs the owner's acceptance or scheduling.

## 13. Settled calls (lead, 2026-11-23; to log in `DECISIONS.md`)

The earlier open questions 1-9 are settled with the recommended answers:

- **S1 React delivery.** A prebuilt generic renderer, driven by `screens.json` and committed with a digest check (6.5). There is no per-solution JavaScript and no node outside the repo.
- **S2 Client usage table (design 5.6). Known gap.** v1 logs usage as JSON lines to the app logs (6.4) and writes no table. This needs the **owner's written acceptance, or scheduling** in a later sub-project. STATUS lists it under "Waiting on owner input" until then.
- **S3 Shared SHI provider for drafting.** Allowed under the existing per-client switch. Only names and types are sent, never row values.
- **S4 Parity.** The generated loop speaks SETH's own tool protocol (verbatim copy, parity test) and is checked by the conformance test on the fakes. The deployment gate stays SP3's SETH-side evaluation.
- **S5 SQL safety.** A tokenizer and rules with no parser dependency (5.4, `build/sqlrules.py`), plus `access_mode = 'read'` in SETH and SELECT-only grants for the app identity. No `sqlglot`.
- **S6 Grants.** The app's service principal gets SELECT on the data-read tables by hand (client or FDE, runbook). `app-deep-health` fails verify with "the app cannot read <table>" until it does.
- **S7 Entrypoint.** One agent only. Pipelines are reported by name.
- **S8 Skipping drafting.** Allowed: `screens` on solution-version create is enough. The drafting model is never pinned; a release has exactly one runtime model binding, which is also the app's serving endpoint.
- **S9 Old releases.** `/1` releases deploy the SDK template; an app needs a `/2` release (6.2).

The builder-read items R1-R32 are settled in place:
- R1, R2: section 3.
- R3, R4, R13, R14: 6.1.
- R5: 5.4.
- R6, R30: 5.6.
- R7: 5.2.
- R8, R9: 5.6.
- R10: 5.1 (`access_mode`).
- R11: 5.1 (discovery path).
- R12: 5.3 (`Assertion`).
- R15: 5.5.
- R16: S8, 5.5.
- R17: section 4.
- R18, R19: 6.2.
- R20, R21: 6.3.
- R22: SP5-2.
- R23-R25: 6.6.
- R26: section 7.
- R27: section 8.
- R28: SP5-4.
- R29: 6.4.
- R31: section 11 dependencies.
- R32: section 10.
