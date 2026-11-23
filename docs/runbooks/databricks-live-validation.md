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

## Status

The integration remains `experimental` (`live_validated: false`) whatever the outcome of these
runs. It is not validated by this runbook; promoting it is a separate decision for the owner.
