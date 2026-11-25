# Sub-project 6: Snowflake Native App — short design

Date: 2026-11-23. Status: approved (lead, under charter 3). Contract: unified design sections 1 ("Client B uses Snowflake"), 5.3, 5.5 (last paragraph), 5.6, 6, 7 (calls 2, 7, 8), 9. Builds on SP2 (`2026-11-17-...`), SP3, the connection management space (`2026-11-22-...`) and SP5 (`2026-11-23-subproject-5-...`, called "SP5" below; its section numbers are written "SP5 6.4" and so on).

SP6 adds a second target kind to SP5's pieces. It does not copy them: `appgen` drafting, accept, the draft schema, `sqlcheck`, the evaluation gate, the second-person approval, the journal and the executor gateway are used unchanged. What is new is the Snowflake flavour of each platform-facing piece.

## 1. Scope and "done when"

**Done when (design section 6):** "The same flow produces a Snowflake Native App."

Concrete check:
1. `python scripts/snowflake_demo.py --fake` (cluster running) runs the flow of design section 1 for Client B on the in-process Snowflake fakes and ends with `SNOWFLAKE DEMO PASSED` (exit 0), or `SNOWFLAKE DEMO FAILED: <step>` (1). Steps in section 10.
2. `cd backend; uv run pytest tests/adapters/native/test_snowflake_native_live.py -m live` deploys the same generated app to the owner's development account. It reads `.devdata/snowflake.env` itself with a copy of the Databricks native live test's env loader and **skips** with "live validation waiting on owner input: no Snowflake account" when the file or its deploy keys (section 9.6) are missing. It is opt-in and owner-only, as the other live tests.

Until the owner runs check 2, the Snowflake native kind stays `experimental` (`live_validated: false`) and STATUS records "live validation waiting on owner input" (CHARTER 7, last paragraph).

**In scope:**
- read-only `connection.sql` tools over a Snowflake connection (the SETH-side test and evaluation path);
- target kind `snowflake.native-app/v1` in `packaging` (compiler `/2` only) and `appgen` requests on Snowflake connections;
- `appgen.render` for Snowflake: a Native App package with a Streamlit front end (section 3), the shared agent loop, a deep-health procedure and an app-owned usage table;
- `adapters.native.snowflake` (ported from the SDK), its fake, Snowflake targets, claims and executor credentials in `delivery`/`shifde.executor`;
- the screens, the demo, the live test and the runbook section.

**Out of scope:**
- Snowpark Container Services (SPCS), compute pools, image registries, and so a React bundle inside the app (section 3, Call S6-A);
- Marketplace listings, release directives/channels, cross-account consumer install. The app is installed in the same account as its package (the SDK's profile);
- dropping versions, pruning patches, deleting packages or applications (no native delete, as SP2);
- manifest `references` and consumer-bound objects; grants on client tables are given by hand (Call S6-F);
- `/1` (SDK-template) releases for Snowflake (Call S6-H);
- tools that write, call outside services or need approval (design call 8), pipelines as entrypoint, the supervisor (SP7).

## 2. The flow mapped to modules

| Step (design 1) | Snowflake specifics | Module (state) |
|---|---|---|
| 1 Client B as organization | unchanged | `tenancy`, `identity`, `authz` (done) |
| 2 Snowflake connection, tested, discovered | existing adapter (`adapters/dataplatforms/snowflake.py`), `access_mode = 'read'` | `connections` (done) |
| 3a-3c Request, draft, accept | request accepts a Snowflake connection; draft and accept unchanged | `appgen` (small change, SP6-4) |
| 3d Test in SETH | `connection.sql` over Snowflake; model through an org `openai_compatible` provider on the account's Cortex endpoint, or `fake` in dev/test | `build`, `adapters.dataplatforms` (SP6-1) |
| 4 Seal + evaluate | `/2` with `target_kind = snowflake.native-app/v1`; evaluation unchanged | `packaging` (SP6-4) |
| 5a Plan | `Resolved` gathered as SP5 6.3, `render` for Snowflake, executor plans with `adapters.native.snowflake` | `delivery`, `appgen`, `adapters.native` (SP6-5, 6-6) |
| 5b Approve | unchanged | `delivery` (done) |
| 5c Deploy + verify | eight checkpoints (section 6.2); verify adds `app-deep-health` via `CALL` | `adapters.native.snowflake`, `shifde.executor` (SP6-2, 6-3) |
| 6 Native app at client | Snowflake Native App in Client B's account: Streamlit UI + agent loop, Cortex model, SQL under the app's grants, no call to SETH | rendered source |
| 7 Which release where | existing deployments list | `delivery` (done) |

## 3. The package shape and the front-end decision

**Call S6-A: Streamlit, not React via SPCS.** The generated Snowflake app's front end is a Streamlit object inside the Native App. It draws the same `screens.json` (chat and table screens of SP5 5.3) with Streamlit widgets.

Why:
- Design 5.5 leaves the choice to this sub-project; design 9 names the container path as unproven.
- SPCS needs a compute pool, an image repository, a container build and push in the executor (Docker), and the consumer granting `CREATE COMPUTE POOL` and `BIND SERVICE ENDPOINT`. None of that can be proven by an in-process fake, and every piece adds a live-only failure mode.
- Streamlit in a Native App is plain SQL objects plus staged files: the fake can model each step, and the owner's development account can run the same steps with only the deploy role of section 9.6.
- The SDK's own Snowflake profile (commit `1fd902b`, `packages/adapters/snowflake/source.py`) is Streamlit, so the port stays close to a reviewed source.
- A React bundle inside Streamlit (custom component) is not used: component support and the content-security policy inside Native App Streamlit are not something the fake can prove.

This departs from constraint "generated client front ends are React (web)" for Snowflake only. It is logged as a call for the owner to see; a later `snowflake.native-app-spcs/v1` kind can serve SP5's React bundle without changing anything here.

**Rendered files** (`appgen.render` for `snowflake.native-app/v1`; package-relative paths):

| Path | Content |
|---|---|
| `manifest.yml` | the same text for every release: `manifest_version: 1`; `version: {name: V1, label: "SHIFDE V1"}`; `artifacts: {setup_script: setup.sql, default_streamlit: APP_PUBLIC.UI}`. No `privileges`, no `references`, no external access. The patch label lives only in the `ADD PATCH` SQL (6.2) |
| `setup.sql` | generated, fixed text (below) |
| `ui/environment.yml` | Snowflake Anaconda channel: `python`, `streamlit` and `snowflake-snowpark-python` at pinned versions (Call S6-G) |
| `ui/streamlit_app.py` | the screen renderer: reads `config/screens.json`, draws chat (calls `agent_loop.ask`) and table screens (form of the tool's inputs, rows, "truncated" note) |
| `ui/agent_loop.py` | the platform-neutral loop, **byte-identical** to the Databricks app's: the same template output with no per-release substitution (section 4) |
| `ui/backend_snowflake.py` | `call_model(messages)` via `SNOWFLAKE.CORTEX.COMPLETE` and `run_sql(sql, params, max_rows)` via `session.sql(..., params=[...])`; writes the usage row |
| `ui/deep_health.py` | handler of `APP_PUBLIC.DEEP_HEALTH()` |
| `ui/config/agent.json`, `screens.json`, `limits.json` | as SP5 6.3 (`limits.json` unchanged) |
| `ui/config/tools.json` | per tool `{name, description, sql (with ?), param_order: [names, repeated when a name is used twice], parameters, input_schema, max_rows}` |
| `release.json` | as SP3 |

**`setup.sql`** (identifiers fixed; only the app-local schema names appear):

```
CREATE APPLICATION ROLE IF NOT EXISTS APP_USER;
CREATE OR ALTER VERSIONED SCHEMA APP_PUBLIC;
CREATE SCHEMA IF NOT EXISTS APP_DATA;
CREATE TABLE IF NOT EXISTS APP_DATA.USAGE (at TIMESTAMP_LTZ, release_digest STRING,
  model STRING, prompt_tokens NUMBER, completion_tokens NUMBER);
CREATE OR REPLACE SECURE FUNCTION APP_PUBLIC.HEALTH() RETURNS VARCHAR LANGUAGE SQL AS $$ SELECT 'OK' $$;
CREATE OR REPLACE PROCEDURE APP_PUBLIC.DEEP_HEALTH() RETURNS VARIANT LANGUAGE PYTHON
  RUNTIME_VERSION = '<pinned>' PACKAGES = ('snowflake-snowpark-python') IMPORTS = ('/ui/deep_health.py', '/ui/config/tools.json', '/ui/config/agent.json')
  HANDLER = 'deep_health.run';
CREATE OR REPLACE STREAMLIT APP_PUBLIC.UI FROM '/ui' MAIN_FILE = '/streamlit_app.py';
GRANT USAGE ON SCHEMA APP_PUBLIC TO APPLICATION ROLE APP_USER;
GRANT USAGE ON FUNCTION APP_PUBLIC.HEALTH() TO APPLICATION ROLE APP_USER;
GRANT USAGE ON PROCEDURE APP_PUBLIC.DEEP_HEALTH() TO APPLICATION ROLE APP_USER;
GRANT USAGE ON STREAMLIT APP_PUBLIC.UI TO APPLICATION ROLE APP_USER;
GRANT USAGE ON SCHEMA APP_DATA TO APPLICATION ROLE APP_USER;
GRANT SELECT ON TABLE APP_DATA.USAGE TO APPLICATION ROLE APP_USER;
```

The text above is a sketch. The statement forms, the stage layout and the `IMPORTS` syntax are copied from the SDK setup script and source at `1fd902b`. `IMPORTS` lists every module the deep-health handler imports. `RUNTIME_VERSION` is pinned in SP6-5 together with S6-G: Python 3.11 unless the Anaconda channel says otherwise, and the chosen value is recorded. The fake checks only that the files are present and the statements come in order; only the live test proves the real Snowflake semantics.

`APP_DATA` is non-versioned, so usage rows survive upgrades. The app writes usage under the owner's rights; `APP_USER` gets no INSERT grant. **Call S6-E:** this closes SP5's known gap S2 for Snowflake: the app records its token usage in a table in the client's account (design 5.6). Databricks keeps S2 open.

**Deep health** (`CALL <APP>.APP_PUBLIC.DEEP_HEALTH()`) returns SP5 6.4's JSON shape exactly (`status`, `generator_version`, `release_digest`, `checks`). `generator_version` is the Snowflake renderer's own version. The check names stay SP5's closed list:
- `warehouse`: `SELECT 1`. If it fails, the reason says the application's USAGE grant on the warehouse is missing;
- `serving_endpoint`: `SNOWFLAKE.CORTEX.COMPLETE(<model>, 'ping', {max_tokens: 1})` succeeds. If it fails, the reason names the Cortex model and says the `CORTEX_USER` grant is missing: "the app cannot use Cortex model <m>";
- `table:<D.S.T>` per data-read table: `SELECT * FROM <t> LIMIT 0`; reason "the app cannot read <t>".

The verify detail always names the check that failed.

## 4. Modules, layers, reuse

No new module and no layer change. Changes by module:

- **`build`**: `DATA_PLATFORM_ADAPTER_OF` gains `"snowflake": ("snowflake", "query.sql")`; `tool_runtime.sql_request` converts per adapter (6.1). `build/sqlrules.py` gains a `dialect` argument (`"databricks"` default, `"snowflake"`).
- **`adapters.dataplatforms`**: Snowflake invoke takes typed positional bindings; `FakeSnowflakeAccount(sample_data=True, cortex={model: script})`.
- **`packaging`**: `TARGET_KINDS = {"databricks.app/v1", "snowflake.native-app/v1"}`; the `/2` compatibility check dispatches its `tools.<pin>` rule on the target kind.
- **`appgen`**:
  - `render.py` gains `GENERATOR_VERSIONS = {"databricks.app/v1": "shifde.appgen/1.0", "snowflake.native-app/v1": "shifde.appgen.snowflake/1.0"}` and `generator_version_for(target_kind)`, and dispatches on `manifest.target_kind`. `GENERATOR_VERSION` stays as the Databricks alias. SP5's version pattern widens to `shifde.appgen(.<kind>)?/<major>.<minor>`; SP6-5 owns that parser change;
  - `Resolved` gains the model's provider row `{kind, base_url}`, read under the plan requester's scope; the 6.4 gates use it; the Snowflake part lives in `render_snowflake.py` (pure, under the same "appgen.render is pure" contract, extended to `shifde.appgen.render_snowflake`);
  - `templates/snowflake/` (package data): `streamlit_app.py.txt`, `backend_snowflake.py.txt`, `deep_health.py.txt`, `environment.yml`, `setup.sql.txt`, `manifest.yml.txt`;
  - **shared loop (Call S6-B):** SP6-5 splits the loop into a platform-neutral `templates/agent_loop.py.txt`, used by both renderers, and one backend module per platform. The loop takes `call_model` and `run_sql` callables; SP5's `app.py` wires the Databricks ones (`templates/backend_databricks.py.txt`). A golden digest test pins the Databricks render of the SP5 fixture release byte for byte, and the Databricks generator version does not change.
- **`adapters.native.snowflake`** (new package beside `databricks/`): `source.py`, `clients.py`, `adapter.py`, `fake.py`. It has no templates, because the source is always rendered. The Snowflake normalizer is added to the existing `adapters/native/addresses.py`, keyed by platform.
- **`delivery`**: target kind, bindings and claims for `snowflake`; plan gates (6.4); verify mapping.
- **`shifde.executor`**: adapter dispatch on `target.platform_kind`; Snowflake credential entries.

**Import-linter changes** (SP6-2, SP6-5):
1. `shifde.adapters.native.snowflake` falls under the existing "the executor never reaches the database" contract (it is inside `shifde.adapters.native`).
2. Contract 5 of SP5 ("SETH never imports the generated app's libraries") adds `streamlit` and `snowflake` (the external distribution; `shifde.adapters.native.snowflake` is internal and unaffected). No `snowflake-connector-python` or `snowflake-snowpark-python` enters the runtime dependencies.

**Ported from the SDK `1fd902b`, rewritten sync and httpx as in SP2:** `packages/adapters/snowflake/src/shifde_snowflake/{adapter,clients,source}.py`, `platform_specs/snowflake.py`, `tests/adapters/{snowflake_fake,test_snowflake,test_snowflake_clients}.py`. Kept: identifier validation (`[A-Za-z_][A-Za-z0-9_$]{0,254}`, uppercased, double-quoted), the eight checkpoints, ownership checks between steps, no adoption of pre-existing objects, `ENABLE_RELEASE_CHANNELS = FALSE`, version name <= 30 characters, patch 0..129, hook-free primitive stage copy, setup validated before version create, reconcile always `ambiguous`. Dropped: Temporal, the container sandbox, pools, the OAuth token-file broker modes, the connector dependency (Call S6-C), the SDK's Streamlit "ready" template.

## 5. Tables and migrations

**No new table.** Every SP6 row lands in existing client-owned tables (`delivery_*`, `solution_versions`, `releases`, `appgen_*`), which already have forced RLS, `tenant_isolation`, composite FKs and cross-client tests. `tests/schema/global_tables.txt` is untouched.

Two new migrations widen CHECK constraints only, each in its own branch's next file, each with a downgrade that refuses (raises) while a `snowflake` row exists:
- **`delivery_0007_snowflake_kinds`**: `delivery_platform_claims.platform_kind` and `delivery_targets.platform_kind` IN (`databricks`, `snowflake`); `delivery_artifacts.target_kind` IN (`databricks.app/v1`, `snowflake.native-app/v1`).
- **`packaging_0004_snowflake_target_kind`**: `solution_versions.target_kind` IN the same two kinds.

`delivery_targets.bindings` stays jsonb. Its shape is validated by the service per platform (6.4).

**Cross-client tests for the changed surface** (each new test file named in its issue):
- a Snowflake target, plan, deployment and release preview of organization A are 404 to B on every route that serves them (parametrized over the existing `tests/delivery/test_routes_cross_client.py` routes with a Snowflake fixture);
- B registering A's Snowflake account address gets 409 `platform_already_registered` with the same message as Databricks;
- B's executor leases nothing of A's Snowflake plan;
- `shifde_app` selecting B's Snowflake target rows from A's context returns none (SQL-level).

## 6. Platform pieces

### 6.1 SQL tools over Snowflake (SP6-1)

- **Dialect rules.** `sqlrules.statement_rules/table_rules/parameter_rules(sql, ..., dialect)`; `appgen.sqlcheck` and `build` publish pass the connection's adapter key. For `snowflake`:
  - `"..."` is an identifier, not a string. **Call S6-D:** a quoted identifier is accepted only if its content matches `^[A-Z_][A-Z0-9_$]*$` (quoting is redundant, so it equals the unquoted name); anything else is `sql_quoted_identifier_unsupported`. Backticks are not quotes (`sql_syntax`).
  - `$$...$$` is an opaque string; `//` outside a string is `sql_comment`.
  - The shared write keywords are still refused anywhere in the statement. The Snowflake-only additions `list comment remove unset execute undrop begin commit rollback` are refused only as the first token of a statement.
  - A `?` outside a string literal in tool SQL is refused (`sql_positional_parameter`).
  - A `:` directly followed by a digit outside quoted text and comments is refused (`sql_positional_bind`, #325), as the run-time check refuses it (`col:1` and `x::1` included).
  - The `->>` flow operator in code is refused (`sql_flow_operator`, #358): it chains a second statement. The run-time check refuses it with the same message. Quoted text, comments, `->` and `>>` are not it.
  - An unquoted name, or a whole `"..."` identifier, starting with `SYSTEM$` in any case is refused (`sql_system_function`, #358), as the run-time check does (#285).
  - The word `procedure` is refused anywhere in code as `sql_write_statement` (#358), as the run-time check does.
  - `IDENTIFIER(` and `TABLE(` may hold only one `'...'` literal (no backslash, no doubled quote, no `system$`) or one plain name; anything else is `sql_dynamic_name` (#358). It is the run-time rule of #312 except in two places, where publish is stricter: a bound `IDENTIFIER(:name)` is refused (no value is known at publish, and the generated app checks none; the run-time check accepts it and checks the value, #339), and `TABLE(` has no run-time rule.
  - A name followed by `(` after `from`/`join` (`TABLE(...)`, `IDENTIFIER(...)`) stays `sql_table_function`; in drafts and at render `TABLE(...)` and `IDENTIFIER(...)` are refused anywhere, a plain literal included (#358).
  - In drafts and at render the table rules fail closed (#358): after `from`, `join` and every comma of a FROM clause only an allowed table, an earlier CTE or a subquery may stand (no stage, string or bind); a `..` in a name, a name glued to a number and a non-ASCII character outside quoted text are refused.
  - Table names and aliases are compared uppercased on both sides, against the discovered names uppercased.
  - Each dialect rule has golden test cases in the issue's acceptance checks.
- **Request tables.** For a Snowflake connection, a requested table whose parts do not each match `^[A-Za-z_][A-Za-z0-9_$]*$` is 422 `table_name_unsupported` (mixed-case quoted names cannot be referenced safely under S6-D).
- **Operation name.** The `DATA_PLATFORM_ADAPTER_OF` entry uses whatever operation name `snowflake.py` registers; SP6-1 checks it.
- **Parameters.** The stored template keeps `%(name)s`. For a Snowflake connection, `sql_request` replaces each occurrence, in order, with `?`. It works only on tokens outside string literals. It sends `params` as a positional list (a name used twice is bound twice). The API keeps the Databricks type names in `param_types`, so OpenAPI does not change, and each adapter maps them: `BIGINT` -> `FIXED`, `DOUBLE` -> `REAL`, `STRING`/`DATE` -> `TEXT`, `BOOLEAN` -> `BOOLEAN`. **Call S6-I:** dates are bound as `TEXT` `YYYY-MM-DD` (pattern-checked by the input schema) and rely on Snowflake's implicit cast; the SQL API's epoch-based `DATE` binding is not used.
- **Tool result.** Same shape as SP5 6.1, `{columns: [{name, type}], rows, row_count, truncated}`, with `type` from `rowType[].type`. The generated `backend_snowflake.run_sql` returns the same shape; the conformance test compares them.
- **Fake account with sample data.** `FakeSnowflakeAccount(sample_data=True, cortex={model: script})`:
  - the SP5 sample rows as `DEMO_DB.PUBLIC.CUSTOMERS`, `DEMO_DB.PUBLIC.ORDERS`, `DEMO_DB.SALES.INVOICES` (shared row data with the Databricks fake, one module-level constant), in read-only SQLite, `a.b.c` -> `a__b__c` lowercased, `?` bound positionally; discovery tree includes them;
  - `SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?), PARSE_JSON(?))` is recognized by a regex before the SQLite step, in the SQL API fake only. It is answered from `cortex[model]` with the `FakeProvider` matching rule and returns the JSON of the options form (`choices[0].messages`, `usage {prompt_tokens, completion_tokens}` word-counted);
  - `POST /api/v2/cortex/v1/chat/completions` (OpenAI format, PAT as bearer) serves the same script for SETH-side runs through an `openai_compatible` provider. This path is marked "unverified until the live run";
  - there are two fixtures, one per reply shape (options-form Cortex JSON and OpenAI chat);
  - fault switches `table_denied:<D.S.T>` and `cortex_denied`.
- **Fixtures.** `tests/_support/appgen_fixtures.py` gains `SNOWFLAKE_SAMPLE_DRAFT` (SP5's draft with `DEMO_DB.*` table names) and reuses `AGENT_SCRIPT` unchanged. SP6-4 adds this fixture, not SP6-1.

### 6.2 Native adapter and fake platform (SP6-2)

**Clients (Call S6-C).** SQL and DDL go through the SQL API v2 over httpx, following the pattern of SP2's `clients.py`. The SQL API's egress is allowed only to `*.snowflakecomputing.com`; the CLI stage copy (below) also reaches the account's stage cloud storage (the presigned S3, Azure Blob or GCS endpoints the account returns) and the certificate OCSP responders, so an egress allow-list for the executor must include those (B-3926). The executor mints a key-pair JWT for each call. Only JWT signing and claims move to a pure module, `adapters/snowflake_jwt.py`, which both `dataplatforms.snowflake` and `native.snowflake` import; the token cache stays in the adapter. Stage upload is the only operation the SQL API cannot do. It uses the Snowflake CLI pinned at `3.33.0` (the SDK's pin), in its own virtual environment. The pin is kept wherever SP2 pins the Databricks CLI; SP6-2 finds that place and states it in its build notes. The CLI is version-checked, with a minimal environment, the key passed in a 0600 file inside the job's temporary directory and deleted after, and only `snow stage copy <local> @<stage>/<digest>/ --no-overwrite` (no `snow app`, no project file, no hooks). The 0600 mode is not enforced on Windows, which relies on the ACL of the per-call temporary directory.

**Steps** (each a journal checkpoint: intent before the write, complete after; SP2 section 4 unchanged):

| Step | Statement |
|---|---|
| `package-create` | `CREATE APPLICATION PACKAGE <PKG> ENABLE_RELEASE_CHANNELS = FALSE` (only if inspect saw none; an existing package not owned by this target refuses the plan) |
| `schema-create` | `CREATE SCHEMA IF NOT EXISTS <PKG>.SHIFDE` |
| `stage-create` | `CREATE STAGE IF NOT EXISTS <PKG>.SHIFDE.SOURCE` |
| `stage-upload` | CLI copy to `@<PKG>.SHIFDE.SOURCE/<release digest>/`, then `LIST` compared file by file (sizes and md5) |
| `setup-validate` | staged `manifest.yml`, `setup.sql`, `ui/environment.yml` equal the artifact's bytes |
| `version-create` | no application at plan time: `ALTER APPLICATION PACKAGE <PKG> ADD VERSION V1 USING '@...'` (patch 0); application exists: `ADD PATCH FOR VERSION V1 USING '@...' LABEL = 'shifde <digest12>'` |
| `application-apply` | no application: `CREATE APPLICATION <APP> FROM APPLICATION PACKAGE <PKG> USING VERSION V1 PATCH 0`; application exists: `ALTER APPLICATION <APP> UPGRADE USING VERSION V1 PATCH <n>` |
| `application-access` (terminal) | `GRANT APPLICATION ROLE <APP>.APP_USER TO ROLE <deploy role>` only. The application's warehouse USAGE and `CORTEX_USER` are granted by hand (S6-F) |

- **Patch number (Call S6-J).** One version `V1` per package. Whether the application exists, as seen by inspect, decides the path. If there is no application, the steps are `ADD VERSION V1` (patch 0) and `CREATE APPLICATION`. If there is one, the patch is `n = max(observed patches) + 1`, fixed in the plan snapshot, and the steps are `ADD PATCH` and `ALTER APPLICATION ... UPGRADE`. A plan that would need patch 130 is refused at plan time with `snowflake_patch_limit` ("this package has no free patch; a new package is needed"). Versions are never dropped.
- **Observe:** `SHOW APPLICATIONS LIKE`, `DESCRIBE APPLICATION`: version `V1`, the planned patch, upgrade state complete, source package `<PKG>`.
- **Verify** (all must pass for `verified`): `native-version` (observe facts), `runtime-health` (`SELECT <APP>.APP_PUBLIC.HEALTH()` = `OK` on the bound warehouse), `app-deep-health` (`CALL <APP>.APP_PUBLIC.DEEP_HEALTH()`, passing only if `status == "ok"`, `release_digest` equals the plan's and every check passed; failed reasons copied into the detail).
- **Native resources:** `package`, `stage`, `version` (`V1`), `application` rows in `delivery_native_resources`; ownership is checked between steps as in the SDK.
- **Fake platform** `adapters/native/snowflake/fake.py` (`FakeSnowflakeNativePlatform`): stateful packages, stages (files by path), versions/patches, applications, grants; answers the SQL API and acts as the CLI. It answers `DEEP_HEALTH` by running the staged `ui/deep_health.py` against a `FakeSnowflakeAccount(sample_data=True)` through the session shim of 6.3, so the fake proves the rendered code, not a canned answer. Fault switches: `fail_after_accept:<step>`, `setup_mismatch`, `foreign_package`, `foreign_application`, `deep_health_bad`, `hang`. Receipts carry `platform: fake`.

### 6.3 Generated app (SP6-5)

- `agent_loop.py` is SP5's (S6-B). `backend_snowflake.py` (about 120 lines, no `shifde` import):
  - `call_model`: `session.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?), PARSE_JSON(?))", params=[model, messages_json, '{"max_tokens": 4000}'])` uses the options form and reads `choices[0].messages` and `usage`;
  - after each answer it inserts one `APP_DATA.USAGE` row with bound parameters, under the owner's rights;
  - `run_sql`: `session.sql(sql, params=[...in param_order]).collect()` keeps SP5's row-limit rule and sets `truncated` the same way. It adds no extra `.limit`. Tool results are capped at 256 KiB, as in SP5.
- Limits, `invalid_tool_call`, the protocol and the parity test are SP5's; one parity test runs over both rendered loops.
- Render checks every tool's SQL against the release's `data_reads` (`table_rules` over the union of their tables, #358), after the length limit (8000 characters, a draft's) and the statement and parameter checks: 409 `release_not_renderable` unless every FROM clause holds only declared tables, earlier CTEs and subqueries and no other three-part name is used. It does not read an object a function names by a string, or a one- or two-part name outside a FROM clause (`get_ddl`, `seq.nextval`, a two-part UDF): the installed app's grants are the limiting control there.
- `streamlit_app.py` gets the session with `get_active_session()` and passes it in; the core modules never import Snowpark, so tests inject a fake session.
- **Conformance test** (`tests/appgen/test_generated_snowflake_app.py`): unpack the rendered archive; run `agent_loop` + `backend_snowflake` with a fake session backed by `FakeSnowflakeAccount(sample_data=True, cortex={...: AGENT_SCRIPT})` over the fixture dataset (each case passes its assertions); run `deep_health.run` (200-equivalent `status: ok`, then `table_denied` gives "the app cannot read"); run `streamlit_app.py` with `streamlit.testing.v1.AppTest` and a stub `snowflake.snowpark.context` module on a temporary `sys.path` (chat answers; the table screen shows rows). The archive scan of SP5 6.3 adds `-----BEGIN` and `.snowflakecomputing.com` (no account address in the artifact).

### 6.4 Delivery (SP6-3, SP6-6)

- **Targets.** `TargetCreate.platform_kind: Literal["databricks", "snowflake"]`. Snowflake address: `https://<account>.snowflakecomputing.com` or `https://<account>.privatelink.snowflakecomputing.com`, normalized by the Snowflake entry of `adapters/native/addresses.py` (https, lowercase, `_` -> `-`, no port/path/query); claims and the 409 message as SP2 section 3. Bindings, validated per platform by a discriminated union:
  - `SnowflakeBindings {package, application, warehouse: identifier, cortex_model: ^[a-z0-9][a-z0-9._-]{0,63}$}`;
  - `package` and `application` unique per claim (409 `binding_in_use`).
- **Plan gates** for a `/2` Snowflake release, after SP3's, in order (409):
  1. `target_kind_mismatch`: release `target_kind` differs from the target's platform kind (also added for Databricks);
  2. `cortex_model_mismatch`: the release's single model binding `model` differs from `cortex_model`;
  3. `model_not_in_target_account`: the provider row in `Resolved` is an `openai_compatible` provider whose `base_url` scheme and host normalize to the target address, or a `fake` provider in development/test.
- **Executor.** `runner` picks the adapter by `target.platform_kind`; `--fake-platform` wires both fakes. A credentials-file entry gains `platform` (default `databricks`); a Snowflake entry is `{tenant_id, host, platform: "snowflake", user, role, private_key_file}`. The broker also refuses an entry whose `platform` differs from the target's. The key is read per grant, never logged, never sent to SETH.

## 7. API and screens

**API.** No new route. Changed:
- target create/patch accept `platform_kind: snowflake` and Snowflake bindings;
- app-request create accepts Snowflake connections. **Call S6-K:** the refusal `connection_not_databricks` becomes `connection_platform_unsupported` (any adapter other than `databricks`/`snowflake`); SP5's tests are updated in SP6-4. New 422 `table_name_unsupported`;
- `AppPreviewOut` gains `target_kind`;
- plan request: the 409s of 6.4 and `snowflake_patch_limit`.

Audit events unchanged in name; target events carry `platform_kind`. Refresh `backend/openapi.json`.

**Screens** (`frontend/src/app/(app)/o/[org]/`, Vitest tests):
- `deliver/targets`: platform selector; Snowflake fields (account address, package, application, warehouse, Cortex model); `experimental` badge;
- `solutions/generate`: Snowflake connections listed with their platform; the `table_name_unsupported` message inline;
- `releases/[id]` preview: the file list and the screens drawn by `@shifde/appgen-web` with a note "Deployed as Streamlit in Snowflake; layout differs";
- plan form: the 6.4 refusals and `snowflake_patch_limit` in plain words; the deployment timeline shows the eight steps.

## 8. Security and isolation

1. **One account, one organization.** Snowflake account addresses are claimed like Databricks workspaces; the claim hash and message are SP2's.
2. **Executor.** One organization's executor leases only its jobs; its credentials file holds only its organization's entries; `platform`, `host` and `tenant_id` are checked per grant. The private key never enters SETH, the artifact, a plan or a receipt.
3. **SQL safety.** `sqlrules` with the Snowflake dialect at draft and publish, `access_mode = 'read'` in SETH, and SELECT-only grants to the application in the client account. Quoted identifiers cannot hide a table from the allow-list (S6-D).
4. **Generated package.** `manifest.yml`, `setup.sql` and `environment.yml` are generated and compared byte-for-byte at `setup-validate`; no external access integration, no `EXECUTE IMMEDIATE`, no `references`, no requested account privileges. The archive holds no SETH origin, no account address, no key material.
5. **No adoption, no delete.** A package or application of the bound name that this target does not own refuses the plan; nothing is dropped.
6. **Unknown outcome.** SP2's rules apply unchanged; reconcile is `ambiguous` with evidence "inspect query history; no automatic replay".
7. **Grants by hand (Call S6-F).** The client or FDE gives the application SELECT on the data-read tables, `SNOWFLAKE.CORTEX_USER` and USAGE on the warehouse (runbook). Until they do, deep health fails verify with a reason that names the missing grant. The deploy role gets no privilege on client tables.

## 9. Fakes, live test, runbook

1. Data fake: 6.1. Native fake: 6.2. Both refuse outside `SHIFDE_ENV` development/test and badge receipts `platform: fake`.
2. `SHIFDE_DEV_FAKE_DATA_PLATFORMS` already routes `*.snowflakecomputing.com`; the permissive account gains `sample_data=True` for the local e2e.
3. Pins: `streamlit` and `snowflake-snowpark-python` in `ui/environment.yml` and the backend `dev` group must be equal (test), chosen at SP6-5 from versions on Snowflake's Anaconda channel (Call S6-G).
4. The Snowflake CLI pin lives in `tools/snowflake-cli.in` with a hashed lock, as SP2's Databricks CLI.
5. Live test `tests/adapters/native/test_snowflake_native_live.py` (`-m live`): render the fixture release with `DEMO_DB` replaced by `SNOWFLAKE_LIVE_DATABASE`, deploy, observe, verify (deep health passing after the runbook grants), then a second release as patch 1; cleanup is manual and listed.
6. `.devdata/snowflake.env` gains deploy keys, read only by the live test: `SNOWFLAKE_DEPLOY_USER`, `SNOWFLAKE_DEPLOY_PRIVATE_KEY_FILE`, `SNOWFLAKE_DEPLOY_ROLE` (CREATE APPLICATION PACKAGE, CREATE APPLICATION, USAGE on the warehouse), `SNOWFLAKE_LIVE_DATABASE`, `SNOWFLAKE_CORTEX_MODEL`. The read keys are never used to deploy, the deploy keys never to read.
7. Runbook `docs/runbooks/snowflake-live-validation.md` gains a "Native App" section: the deploy role and its grants, the sample tables, the hand grants of S6-F, the live run and cleanup. It also lists the risks that only a live run can show: setup and `IMPORTS` semantics, stage layout, Cortex reply shapes, the OpenAI-compatible endpoint path, and Streamlit behaviour inside the app.

## 10. Done-when demo

`python scripts/snowflake_demo.py --fake` follows `solution_demo.py`: in-process API, worker (`shifde.worker_main.job_handlers()`) and executor runner on the fakes, scratch database `shifde_demo_<hex>` dropped at the end (also on failure), no network.

1. Org B has project P. A Snowflake connection to `FakeSnowflakeAccount(sample_data=True, cortex={"mistral-large2": AGENT_SCRIPT})` is tested and discovered; a `fake` drafting provider uses `DRAFT_SCRIPT`.
2. Request with `[[appgen-sample]]` and `DEMO_DB.*` tables; drafted; settled hold and `appgen_draft` usage row.
3. Accept: prompt, tools, agent, dataset, solution version with `target_kind snowflake.native-app/v1` and 2 screens.
4. A SETH run of the agent answers case 1 over the Snowflake fake.
5. Seal `/2` with `model = "mistral-large2"`; plan refused `evaluation_required`; evaluation `passed`.
6. Snowflake target (`cortex_model: mistral-large2`) accepts the plan; rendering twice gives one digest; a plan with a wrong Cortex model is refused `cortex_model_mismatch`.
7. Self-approval 403; a second admin approves.
8. Apply (eight checkpoints), observe and verify on the native fake: `verified`, deep health run from the staged code. A second release deploys as patch 1 (upgrade).
9. Unpack the artifact and run the generated app (fake session): each case passes; the table tool returns rows; `APP_DATA.USAGE` has one row per answer; `AppTest` draws both screens; no SETH origin or account address in the archive.
10. Org A gets 404 on B's request, preview, target, plan and deployment; A registering B's account address gets 409 `platform_already_registered`.

## 11. Factory issues (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports` and `bash factory/checks.sh`. **C** = isolation-critical. Migrations are new files under a protected path, so the approver escalates and the lead merges after the 13-gate set (B-2530). The migration names `delivery_0007_snowflake_kinds` and `packaging_0004_snowflake_target_kind` are confirmed. SP6-1 and SP6-2 may start now. SP6-4 to SP6-8 wait until SP5-9 is merged.

1. **SP6-1 SQL tools over a Snowflake connection; Snowflake dialect rules; fake account with sample data and Cortex (C).**
   - Scope: 6.1, without `SNOWFLAKE_SAMPLE_DRAFT` (SP6-4 adds it); golden cases for each dialect rule. Migration: no.
   - Checks: `uv run pytest tests/build tests/connections tests/adapters/dataplatforms -k "sql or snowflake or fake"`.
   - Depends: SP5-1 (merged); may start now.
2. **SP6-2 adapters.native.snowflake: port, clients, address normalizer, fake native platform (C).**
   - Scope: 6.2 except deep-health execution (stubbed fault-switchable answer until SP6-6); SDK tests ported; JWT helper extracted; CLI pin and lock. Migration: no.
   - Checks: `uv run pytest tests/adapters/native -k "snowflake"; uv run pytest tests/adapters/dataplatforms -k snowflake`.
   - Depends: none; may start now (parallel with 1).
3. **SP6-3 delivery: Snowflake targets, claims, bindings, executor dispatch and credentials (C).**
   - Scope: 6.4 targets and executor; `delivery_0007`; cross-client and claim tests of section 5 for targets; delivery of the SDK-free path with a test-only artifact. Migration: yes.
   - Checks: `uv run pytest tests/delivery/test_targets.py tests/delivery/test_platform_claims.py tests/delivery/test_routes_cross_client.py tests/executor tests/schema; uv run shifde-executor --help`.
   - Depends: 2.
4. **SP6-4 packaging target kind and appgen requests on Snowflake (C).**
   - Scope: `packaging_0004`; `/2` compatibility per kind; appgen request create on Snowflake connections, `connection_platform_unsupported`, `table_name_unsupported`; S6-K test updates; the `SNOWFLAKE_SAMPLE_DRAFT` fixture. Migration: yes.
   - Checks: `uv run pytest tests/packaging tests/appgen/test_requests.py tests/appgen/test_accept.py tests/appgen/test_routes_cross_client.py tests/schema`.
   - Depends: 1, SP5-9 merged.
5. **SP6-5 appgen render for Snowflake, generated Streamlit app, deep health, usage table, conformance (C).**
   - Scope: sections 3 and 6.3; S6-B loop split with the Databricks golden digest; dev-group pins and contract 5 additions. Migration: no.
   - Checks: `uv run pytest tests/appgen/test_render.py tests/appgen/test_render_snowflake.py tests/appgen/test_generated_snowflake_app.py tests/appgen/test_generated_app.py tests/appgen/test_protocol_parity.py`.
   - Depends: 1, 4.
6. **SP6-6 delivery plans deploy the generated Snowflake app; verify deep health on the fake (C).**
   - Scope: 6.4 plan gates, render into the artifact, verify mapping; the native fake runs the staged `deep_health.py`; e2e on the fakes including the patch-1 upgrade and `snowflake_patch_limit`. Migration: no.
   - Checks: `uv run pytest tests/delivery tests/executor tests/adapters/native tests/schema; python scripts/solution_demo.py --fake; python scripts/delivery_demo.py --fake`.
   - Depends: 3, 5.
7. **SP6-7 Front end: Snowflake targets, generate form, preview note, plan refusals.**
   - Scope: section 7 screens.
   - Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`.
   - Depends: 3, 4.
8. **SP6-8 Done-when demo, live test and runbook.**
   - Scope: sections 9 and 10, building on SP5-9's `scripts/solution_demo.py`; a "Snowflake demo" line in `CLAUDE.md` "Local commands".
   - Checks: `python scripts/snowflake_demo.py --fake; cd backend; uv run pytest tests/adapters/native/test_snowflake_native_live.py -m live` (reports skipped without the deploy keys).
   - Depends: 6, 7.

## 12. Calls (lead, 2026-11-23; to log in `DECISIONS.md`)

- **Call S6-A:** Streamlit inside the Native App, not React via SPCS (section 3). Departs from the "React front end" constraint for Snowflake only; the owner sees it in STATUS; SPCS is a later kind.
- **Call S6-B:** one platform-neutral `agent_loop.py` plus one backend module per platform; a golden digest test pins the Databricks render byte for byte, with no Databricks generator version bump.
- **Call S6-C:** no Snowflake connector or Snowpark in SETH: SQL API v2 over httpx with key-pair JWT for every statement; the pinned Snowflake CLI 3.33.0 only for stage copy.
- **Call S6-D:** Snowflake dialect treats `"..."` as identifiers and accepts only redundant (uppercase-safe) quoting; `$$` strings opaque; `//` is a comment.
- **Call S6-E:** the Snowflake app writes its usage to `APP_DATA.USAGE` in the client account, closing SP5's S2 for Snowflake only.
- **Call S6-F:** client-table SELECT, `CORTEX_USER` and warehouse USAGE are granted to the application by hand (runbook), not by the deploy role; deep health names which is missing; no manifest `references`.
- **Call S6-G:** Streamlit and Snowpark versions chosen at SP6-5 from Snowflake's Anaconda channel and pinned equal in `environment.yml` and the dev group.
- **Call S6-H:** Snowflake solutions need screens (`/2`); there is no `/1` SDK-template deploy for Snowflake. A Snowflake target kind without screens is refused at seal with SP5's existing error code for the kind check (no new code).
- **Call S6-I:** dates bound as `TEXT` with implicit cast.
- **Call S6-J:** one version `V1` per package, a new patch per release, refused at patch 130; nothing dropped.
- **Call S6-K:** `connection_not_databricks` renamed `connection_platform_unsupported`.
- **Call S6-L:** Cortex is the app's model (`cortex_model` target binding equal to the release's single model binding); the SETH-side model for tests and evaluation is an `openai_compatible` provider on the same account's Cortex endpoint `/api/v2/cortex/v1/chat/completions` with a PAT as bearer, unverified until the live run (or `fake` in dev/test).

## 13. Settled after builder read (B-3921)

- 1, 2, 28: SP6-1 and SP6-2 start now. SP6-1 does not create `SNOWFLAKE_SAMPLE_DRAFT`; SP6-4 does. SP6-4 to SP6-8 wait on SP5-9 merged, and SP6-8 builds on `scripts/solution_demo.py`.
- 3: The live test copies the Databricks native live test's env loader; it skips with "live validation waiting on owner input: no Snowflake account".
- 4, 5: The loop is split into a platform-neutral loop and a backend module per platform. A golden digest test pins the Databricks render byte for byte, and the Databricks generator version does not change. "Byte-identical" means the same template output, with no per-release substitution.
- 6: `manifest.yml` is the same text for every release; the patch label is only in the `ADD PATCH` SQL.
- 7: `RUNTIME_VERSION` is pinned in SP6-5 with S6-G: Python 3.11 unless the Anaconda channel says otherwise, and the value is recorded.
- 8, 9, 10: Statement forms, stage layout and `IMPORTS` are copied from the SDK at `1fd902b`. `IMPORTS` lists every module the handler imports. The fake checks only file presence and statement order; the live test is the only proof.
- 11, 36: `CORTEX_USER` and the application's warehouse USAGE are granted by hand, not by the deploy role. Deep health names the missing one.
- 12: A `tools.json` entry is `{name, sql (with ?), param_order: [names, repeated when used twice]}` plus SP5's fields; `limits.json` is unchanged.
- 13: The model check keeps SP5's closed name `serving_endpoint`; its detail names the Cortex model.
- 14, 15, 16: Deep health reports the renderer's own `generator_version`. `GENERATOR_VERSIONS[target_kind]` and `generator_version_for(target_kind)` are added; `GENERATOR_VERSION` stays as the Databricks alias. SP6-5 widens SP5's version pattern to `shifde.appgen(.<kind>)?/<major>.<minor>`.
- 17: The operation name is whatever `snowflake.py` registers; SP6-1 checks it.
- 18: The API keeps the Databricks type names. Each adapter maps them (`BIGINT` -> `FIXED`, `DOUBLE` -> `REAL`, `STRING`/`DATE` -> `TEXT`, `BOOLEAN` -> `BOOLEAN`), so OpenAPI is unchanged.
- 19: A `?` outside a string literal in tool SQL is refused; the converter works only on tokens outside literals.
- 20: Each dialect rule has golden test cases in the issue's acceptance checks.
- 21: The shared write keywords stay refused anywhere. The Snowflake-only additions are refused only as the first token of a statement.
- 22, 23: Table names and aliases are compared uppercased on both sides, against the discovered names uppercased.
- 24: The fake recognizes the `CORTEX.COMPLETE` call by a regex before the SQLite step, in the SQL API fake only.
- 25, 26, 27: SETH's provider uses `/api/v2/cortex/v1/chat/completions` with a PAT as bearer, marked unverified until the live run. The app calls `SNOWFLAKE.CORTEX.COMPLETE` in the options form and parses `choices[0].messages`. There are two fixtures, one per reply shape.
- 29: Only JWT signing and claims move to a pure module; the cache stays in the adapter.
- 30: The Snowflake normalizer goes into the existing `adapters/native/addresses.py`, keyed by platform.
- 31: SP2's `clients.py` httpx pattern is reused; SQL API egress is allowed only to `*.snowflakecomputing.com`, and the CLI stage copy also reaches the stage cloud storage (presigned S3, Azure Blob or GCS) and OCSP (B-3926).
- 32: The Snowflake CLI pin mirrors wherever SP2 pins the Databricks CLI; SP6-2 finds that place and states it.
- 33: A live-only risk, listed in the runbook.
- 34, 35: Whether the application exists decides the path. No application: `ADD VERSION V1` (patch 0) and `CREATE APPLICATION`. Existing application: `ADD PATCH` (n = max observed + 1) and `ALTER APPLICATION UPGRADE`.
- 37: The deep-health detail always names the failed check.
- 38: The app writes usage under the owner's rights; `APP_USER` gets no INSERT grant.
- 39: SP5's LIMIT rule is kept; there is no extra `.limit`.
- 40: `Resolved` gains the model's provider row, read under the requester's scope.
- Also: the S6-H refusal reuses SP5's existing error code for the kind check; the migration names `delivery_0007_snowflake_kinds` and `packaging_0004_snowflake_target_kind` are confirmed.
