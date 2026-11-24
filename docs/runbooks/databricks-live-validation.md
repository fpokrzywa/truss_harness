# Databricks live validation (owner runbook)

The Databricks integration is `experimental` (`live_validated: false`). It has only run against
the fake platform. This runbook is for the owner, once a Databricks development workspace exists:
it runs the opt-in live test `backend/tests/adapters/native/test_databricks_live.py` against that
workspace, and optionally the full screen flow with a real executor.

A passing run is evidence, nothing more. The integration stays `experimental` and is not called
validated because of this run; changing that status is a separate decision for the owner.

## Prerequisites

- **A development workspace chosen by the owner.** Never a client platform, never a production
  workspace. Its URL must be on the allow-list (`https://<name>.cloud.databricks.com`,
  `https://<name>.azuredatabricks.net` or `https://<name>.gcp.databricks.com`, see
  `backend/src/shifde/adapters/native/addresses.py`).
- **The pinned Databricks CLI**, version `CLI_VERSION` in
  `backend/src/shifde/adapters/native/databricks/source.py` (1.25.0 at the time of writing), on
  `PATH` as `databricks`. Any other version fails the capability check.
- **A SQL warehouse** in that workspace; the deployed app runs `SELECT 1` on it.
- **A service principal** with permission to deploy and run Databricks Apps in the workspace,
  and **a token for it** (a personal access token of the service principal).

## The env file

Create `.devdata/databricks.env` in the root of the checkout that runs the test (a worktree does
not see the main checkout's file). One `KEY=value` per line, these keys exactly:

- `DATABRICKS_HOST`: the workspace URL (`https://<host>`)
- `DATABRICKS_TOKEN`: the service principal's token
- `DATABRICKS_WAREHOUSE_ID`: the SQL warehouse id
- `DATABRICKS_SERVICE_PRINCIPAL`: the service principal's workspace user name (its application id)
- `DATABRICKS_WORKSPACE_PURPOSE`: must be `development`; any other value refuses the run

The same file may also hold `DATABRICKS_READ_TOKEN` for the read connection test (see "Read
connection (data platform)" below). That key is optional for this deploy test, which never reads
it.

The test reads only this file, never process environment variables. A missing or empty key, a
purpose other than `development` or a host outside the allow-list fails the test before any host
is contacted; the failure names the key or rule, never a value.

`.devdata/` is gitignored. Never commit this file, never print it, and never paste its contents
(or the token) to an agent, an issue or a chat.

## Run the live test

```powershell
cd backend; uv run pytest tests/adapters/native -m live -q
```

Without the env file the test is skipped with "live validation waiting on owner input: no
Databricks workspace". With the file but without `-m live` (a default pytest run, CI or
`factory/checks.sh`) it is skipped with "live validation is opt-in: run with -m live".

The test drives the real adapter (the pinned CLI writes, workspace REST reads) in this order:

1. `capabilities`: the CLI version check and the workspace probe (warehouse and Apps APIs) pass
2. scaffold, validate and build of the template app
3. `plan`
4. `apply`: once, never retried
5. `observe`, every 15 seconds until `succeeded`; it fails on `failed` or after 20 minutes
6. `verify`: `native-runtime` and `runtime-warehouse` must both pass

Add `-s` to see the line `deployed app <name> (deployment <id>)` that the test prints after
`apply`. No database is used and nothing is written to the repository.

## Remove the deployed app afterwards

The test deploys a real Databricks App to the workspace and leaves it there. Its name is
`app_name(application_id, target_id)` in `backend/src/shifde/adapters/native/databricks/adapter.py`:
`shifde-<12 hex>-<8 hex>`, printed after `apply`. Delete it from the workspace UI (Compute, Apps)
or with the CLI, authenticated as the service principal:

```powershell
databricks apps delete <app name>
```

The bundle files are under `/Workspace/Users/<service principal>/.shifde/` in the workspace;
remove that folder as well if you do not want to keep them. Nothing cleans up automatically.

## Optional: the full screen flow with a real executor

After the live test passes, the owner may run the same flow from SETH's screens with a real
executor (API running):

1. Register a target for the development workspace (Targets screen) with the warehouse binding
   and a `credential_ref`.
2. Create an executor and issue its credential (Executors screen); the credential starts `shx_`.
3. Write the executor's credentials file, JSON `credential_ref -> {tenant_id, host, token,
   service_principal}`, outside the repository or under `.devdata/`. A credentials file belongs
   to exactly one organization: `tenant_id` is that organization's id, `memberships[].org.id` in
   `GET /api/auth/session` while signed in to it. An entry of another organization is refused
   (`credential_unavailable`), and an entry without `tenant_id` makes the executor refuse the file.
   A file whose entries name more than one organization is refused too, and the executor refuses
   to start (exit 2) when the file's organization is not its own (the `tenant_id` of `whoami`).
4. Set `SHIFDE_EXECUTOR_API_URL`, `SHIFDE_EXECUTOR_TOKEN` and `SHIFDE_EXECUTOR_CREDENTIALS_FILE`.
   The executor refuses an `http` API URL unless `SHIFDE_ENV` is `development` (or `test`): for
   a local API such as `http://127.0.0.1:8710`, also set `SHIFDE_ENV=development`; otherwise the
   API URL must be https. Then start the executor **without** `--fake-platform`:
   `cd backend; uv run shifde-executor`.
5. Request a plan naming a sealed release (its release id) in the target's workspace and project
   that has a passing evaluation; without one the request is refused (409
   `evaluation_required`). Approve it as a second person, and follow the deployment through
   apply, observe and verify on the Deployments screen.

A release sealed with screens (compiler `shifde.packaging/2`) deploys the generated app instead of
the SDK template; see "Generated app" below.

## What to record

For each run, record:

- the date and the commit (`git rev-parse HEAD`)
- the Databricks CLI version (`databricks --version`)
- pass or fail of each step above, and of each verify check (`native-runtime`,
  `runtime-warehouse`)
- the deployment id (and the app name)

Never record the token, the host's credentials or the env file's contents.

## Writes, retries and unknown outcomes

Every platform write (`bundle-deploy`, `bundle-run`) is attempted once, with no retry. If `apply`
raises in the live test, its outcome is unknown: do not run the test again to "fix" it. The test
prints the app name only after a successful `apply`, so find the app by hand: in the workspace's
Apps list (Compute, Apps), look for an app named `shifde-<12 hex>-<8 hex>` created by the service
principal, and look under `/Workspace/Users/<service principal>/.shifde/` for its bundle folder.
Delete both as in "Remove the deployed app afterwards" before any new run.

In the screen flow, a lost lease (or an executor error) during apply, after a write was started,
makes the deployment `unknown_outcome`. It is never retried and never treated as healthy. It is
resolved only by a person: **Reconcile** (a read-only job whose evidence is shown; for Databricks
it is always `ambiguous`), then **Mark as failed** with a note. Until then the target refuses new
plans.

## Read connection (data platform)

`backend/tests/adapters/dataplatforms/test_databricks_live.py` is a read-only check of the
`databricks` connection adapter (SQL warehouse and Unity Catalog reads, not the deploy adapter
above). It reads the same `.devdata/databricks.env` and needs these keys:

- `DATABRICKS_HOST`: the workspace URL, on the same allow-list as above
- `DATABRICKS_WAREHOUSE_ID`: the SQL warehouse id
- `DATABRICKS_READ_TOKEN`: a read-only token of a principal with only `CAN USE` on the warehouse
  and read grants (`USE CATALOG`, `USE SCHEMA`, `SELECT`) in Unity Catalog. It is never the
  deploy token `DATABRICKS_TOKEN`; the test never reads that token.

The test skips when `DATABRICKS_READ_TOKEN` is absent or empty, with "DATABRICKS_READ_TOKEN is
missing or empty in the live env file", and skips without the file or without `-m live` as the
deploy test does. A host outside the allow-list fails before any host is contacted.

```powershell
cd backend; uv run pytest tests/adapters/dataplatforms/test_databricks_live.py -m live -q
```

It runs the adapter behind the strict production egress policy (https on port 443, public
addresses only), in this order:

1. `test`: the configuration, egress, DNS, TLS, authentication and Unity Catalog / warehouse
   checks must all pass
2. `discover`: a schema of the catalogs, schemas and tables the principal can see
3. `SELECT 1` on the warehouse must answer `1`

`SELECT 1` may start a stopped warehouse (and use its compute). Nothing is written to the
workspace and nothing needs cleanup. Record the date, the commit and pass or fail of each step,
never a value.

## Generated app

A release sealed with screens (compiler `shifde.packaging/2`) deploys the app `appgen.render`
generates instead of the SDK template. This section sets up the workspace for it (sample tables,
serving endpoint, grants), runs the opt-in live test of the generated app and then the steps of
`scripts/solution_demo.py --fake` against the workspace with a real executor. The demo's app
preview step is left out: that route is not on main yet (#340).

### Sample tables

The `SAMPLE_DRAFT` tools (`backend/tests/_support/appgen_fixtures.py`) name these tables
literally, so create them under exactly these names, with the columns, types and rows of
`SAMPLE_TABLES` in `backend/src/shifde/adapters/dataplatforms/fakes.py` (the fake workspace serves
the same rows). Run this in the workspace's SQL editor on the SQL warehouse, as a principal that
may create tables in `main.default` and `main.sales`:

```sql
CREATE SCHEMA IF NOT EXISTS main.sales;

CREATE TABLE main.default.customers (id BIGINT, name STRING, region STRING);
INSERT INTO main.default.customers VALUES
  (1, 'Sample Customer 1', 'North'),
  (2, 'Sample Customer 2', 'South'),
  (3, 'Sample Customer 3', 'East'),
  (4, 'Sample Customer 4', 'West'),
  (5, 'Sample Customer 5', 'North'),
  (6, 'Sample Customer 6', 'South');

CREATE TABLE main.default.orders (id BIGINT, customer_id BIGINT, amount DOUBLE, ordered_on DATE);
INSERT INTO main.default.orders VALUES
  (1, 1, 25.5, DATE '2026-01-11'),
  (2, 2, 50.5, DATE '2026-01-12'),
  (3, 3, 75.5, DATE '2026-01-13'),
  (4, 4, 100.5, DATE '2026-01-14'),
  (5, 5, 125.5, DATE '2026-02-15'),
  (6, 6, 150.5, DATE '2026-02-16'),
  (7, 1, 175.5, DATE '2026-02-17'),
  (8, 2, 200.5, DATE '2026-02-18'),
  (9, 3, 225.5, DATE '2026-03-19'),
  (10, 4, 250.5, DATE '2026-03-20'),
  (11, 5, 275.5, DATE '2026-03-21'),
  (12, 6, 300.5, DATE '2026-03-22');

CREATE TABLE main.sales.invoices (id BIGINT, order_id BIGINT, total DOUBLE);
INSERT INTO main.sales.invoices VALUES
  (1, 1, 27.5),
  (2, 2, 52.5),
  (3, 3, 77.5),
  (4, 4, 102.5),
  (5, 5, 127.5),
  (6, 6, 152.5),
  (7, 7, 177.5),
  (8, 8, 202.5),
  (9, 9, 227.5),
  (10, 10, 252.5),
  (11, 11, 277.5),
  (12, 12, 302.5);
```

### Serving endpoint

Create a model serving endpoint in the same workspace that speaks the OpenAI chat format
(`POST /serving-endpoints/<name>/invocations` with `messages`, answering `choices[0].message`), for
example an endpoint of a foundation model that supports tool use. Its name is used three times:

- `DATABRICKS_SERVING_ENDPOINT` in the env file (the live test below);
- the `model` of the release's single model binding when the release is sealed;
- the target's `serving_endpoint` binding.

The binding's provider is an `openai_compatible` model provider of the organization whose
`base_url` is on the target's workspace host (`https://<workspace host>/serving-endpoints`), with a
model whose `model_key` is the endpoint's name and a rate card for it.

- **Serving endpoint binding.** The target needs a `serving_endpoint` binding (the model serving
  endpoint's name), and it must equal the `model` of the release's single model binding. Without
  it the plan request is refused (409 `serving_endpoint_required`), with another name too (409
  `serving_endpoint_mismatch`). The binding's provider must be an `openai_compatible` provider
  whose `base_url` is on the target's workspace host (409 `model_not_in_target_workspace`
  otherwise).
- **Resources.** Apply adds the endpoint to the app as the `serving-endpoint` resource with
  `CAN_QUERY`, next to the `sql-warehouse` resource (`CAN_USE`).

### Grants for the app's service principal (S6)

Each Databricks App runs as a service principal of its own. After the first apply, grant it these
by hand (the client or the FDE); SETH never grants them:

- `USE CATALOG` on `main`, `USE SCHEMA` on `main.default` and `main.sales`, and `SELECT` on
  exactly the three data-read tables `main.default.customers`, `main.default.orders` and
  `main.sales.invoices`, nothing else;
- `CAN_QUERY` on the serving endpoint, through the app's `serving-endpoint` resource;
- `CAN_USE` on the SQL warehouse, through the app's `sql-warehouse` resource.

```sql
GRANT USE CATALOG ON CATALOG main TO `<app service principal>`;
GRANT USE SCHEMA ON SCHEMA main.default TO `<app service principal>`;
GRANT USE SCHEMA ON SCHEMA main.sales TO `<app service principal>`;
GRANT SELECT ON TABLE main.default.customers TO `<app service principal>`;
GRANT SELECT ON TABLE main.default.orders TO `<app service principal>`;
GRANT SELECT ON TABLE main.sales.invoices TO `<app service principal>`;
```

Until it has them, verify's `app-deep-health` check fails with "the app cannot read <table>" for
each missing table, and the deployment ends `failed`.

The app identity's grants are the only data boundary inside the client. The app runs with no SETH
check in the loop: it queries the warehouse and the serving endpoint under its own identity, so
grant it SELECT on exactly the data-read tables and nothing else.

### The live test

`backend/tests/appgen/test_generated_app_live.py` renders the `SAMPLE_DRAFT` app with the env
file's serving endpoint and runs it in process (Flask's test client), its `WorkspaceClient`
pointed at the workspace with the read token for the test's duration. It deploys nothing, writes
nothing to the workspace and uses no database. It reads `.devdata/databricks.env` and needs these
keys:

- `DATABRICKS_HOST`: the workspace URL, on the same allow-list as above
- `DATABRICKS_WAREHOUSE_ID`: the SQL warehouse id
- `DATABRICKS_READ_TOKEN`: the read token of "Read connection (data platform)"; its principal also
  needs `CAN_QUERY` on the serving endpoint and `SELECT` on the three sample tables (with
  `USE CATALOG` and `USE SCHEMA`). It is never the deploy token `DATABRICKS_TOKEN`; the test never
  reads that token.
- `DATABRICKS_SERVING_ENDPOINT`: the serving endpoint's name
- `DATABRICKS_WORKSPACE_PURPOSE`: must be `development`

```powershell
cd backend; uv run pytest tests/appgen -m live -q
```

Without the file it skips with "live validation waiting on owner input: no Databricks workspace",
without `-m live` with "live validation is opt-in: run with -m live", and with a missing or empty
key with a reason naming only that key. A purpose other than `development` or a host outside the
allow-list fails before any host is contacted. It checks, in order:

1. `GET /health?deep=1` is 200 with every check passed (the warehouse, the serving endpoint and
   the three tables)
2. `POST /api/tools/orders_by_customer` with customer 1 since 2026-01-01 returns orders 1 and 7
3. `POST /api/ask` with case 1's message returns 200 with a non-empty answer (a real model: the
   scripted case assertions are not expected to hold)

### The demo steps run live

With the sample tables and the serving endpoint in place and the live test passing, run the steps
of `scripts/solution_demo.py --fake` through SETH's screens or API against the workspace, with a
real executor as in "Optional: the full screen flow with a real executor" (API and worker
running):

1. Organization A has project P. Create a Databricks read connection (`access_mode` `read`) to the
   workspace with the read token, test it and discover it. Set up a drafting model, the runtime
   model (the `openai_compatible` provider on the workspace host with the endpoint's model, see
   "Serving endpoint") and a tenant budget.
2. As the FDE, request a draft over the three sample tables; the worker drafts it (`drafted`).
   Check the settled budget hold and the draft call's usage row.
3. Edit one tool's `max_rows` (revision 2) and accept it with the runtime model: the prompt, the
   two tools, the agent, the 3-case dataset and the solution version with two screens exist.
4. Start an ordinary run of the published agent with case 1's message; it completes with an
   answer.
5. Seal a release with the single model binding `model = <endpoint name>`. A plan request is
   refused (409 `evaluation_required`). Request an evaluation and wait for its outcome (a real
   model may not satisfy the scripted assertions; record it).
6. Register a target for the workspace with the warehouse and `serving_endpoint` bindings and
   request the plan; the executor seals it.
7. The requester's own approval is refused (403); a second tenant administrator approves.
8. Follow apply, observe and verify on the Deployments screen. If verify fails with "the app
   cannot read <table>", grant as in "Grants for the app's service principal (S6)" and deploy
   again.
9. Open the deployed app: ask each case's question, open the table screen and call
   `/health?deep=1`.
10. As an administrator of a second organization B, the request, a draft of it, the release and
    the plan answer 404.

Record the date, the commit, pass or fail of each step and the three verify checks
`native-runtime`, `runtime-warehouse` and `app-deep-health`; never a value (no token, host,
endpoint credential or env file content). A generated deployment is `verified` only when all three
pass.

### Remove the deployed app and the sample data afterwards

Delete the deployed app and its bundle folder as in "Remove the deployed app afterwards" above
(`databricks apps delete <app name>`, then `/Workspace/Users/<service principal>/.shifde/`). Drop
the sample tables and the serving endpoint too when you no longer need them.

## Status

The integration remains `experimental` (`live_validated: false`) whatever the outcome of these
runs. It is not validated by this runbook; promoting it is a separate decision for the owner.
