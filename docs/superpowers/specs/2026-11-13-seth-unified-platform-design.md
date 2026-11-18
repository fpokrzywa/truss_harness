# SETH unified platform: design

Date: 2026-11-13
Status: approved by the owner on 2026-11-13.

## 1. Intent

SHI's forward deployed engineers (FDEs), project managers and senior staff build AI solutions for clients. Today that work is spread over three applications that each rebuilt the same plumbing (job queue, tenancy, sign-in) and do not share a workflow:

| Source (stays in place, read-only) | What it contributes |
|---|---|
| SETH, `S:\Development_2026\shi_work\shi_enterprise_truss_harness`, `main` at `3ba0e06` | The base. Sign-in, groups and roles, client isolation, agent/tool/prompt/orchestration builder, run engine, cost ledger, governance, the web front end. About 51k lines of backend source, 35.5k of tests, 48k of front end. |
| FDE SDK, `%USERPROFILE%\OneDrive - 3C Publish LLC\Documents\ChatGPT\SHIFDE SDK`, commit `1fd902b` | Deployment to client platforms: adapters for Databricks, Snowflake, Fabric and Palantir, sealed deployment plans, second-person approval, an executor process, verification. About 10.4k lines. |
| Agent Factory, `S:\Development_2026\the_agent_factory`, commit `0fdd09c` | Sealed, digest-bound releases; a compatibility check that refuses unsupported features by name; evaluation gates; cost reservation before each model call; Entra sign-in; standalone Docker deployment. About 5.4k lines of application code. |

The goal is one application, SETH, in one codebase with modules, on one PostgreSQL database. An FDE signs in, works inside one client's space, builds and tests a solution against that client's platform, and deploys the finished solution onto the client's own infrastructure.

**SETH itself is never installed at a client.** There is one shared SETH, used by SHI staff. Only the solutions the team builds are deployed to clients.

### The flow this must support

Client A uses Databricks:

1. An administrator creates Client A as an organization and gives the right groups access to it.
2. An FDE adds a connection to Client A's Databricks workspace (API or the vendor's MCP server).
3. The FDE defines the solution in SETH (agents, tools, prompts, workflow, screens) and tests it against the client's development workspace.
4. The FDE seals a release. Evaluation checks must pass.
5. The FDE plans a deployment. A second person approves it. SETH deploys and then verifies health.
6. What is now running at the client is **a native Databricks application inside the client's Databricks workspace** (a Databricks App). It reads the client's data under its own identity in that workspace and does not call back to SETH.
7. SETH records which release is on which client target. An update repeats steps 4 and 5.

Client B uses Snowflake: the same flow, and the result is a Snowflake Native App in the client's Snowflake account.

## 2. Goals, non-goals, anti-goals

**Goals**

- One repository, one deployable application, one PostgreSQL database, one migration history.
- Each team member has their own sign-in. Access is granted through groups bound to roles.
- A client's data, connections, credentials, runs and artifacts are visible only to staff who have been given access to that client.
- A solution defined in SETH becomes a native application on the client's platform. Databricks first, Snowflake second.
- Token cost is tracked and capped per client and per project while building and testing in SETH.
- SETH's quality gates apply to all code: mypy strict, ruff, layered import rules, tests with every module.
- Tests run per module and in parallel, so a change to one module does not need the full hour-long suite.

**Non-goals (not in this program unless added later)**

- Client staff signing in to SETH.
- Deployed applications reporting back to SETH (usage, health, telemetry).
- Installing SETH per client, white-label branding, client configuration bundles. SETH's existing code for these is ported last or not at all.
- Migrating data from the three existing databases. They hold development data only.
- A replacement for a task tracker such as Jira. Project management in SETH means: engagements, projects, solutions and where each release stands.
- Mobile applications.

**Anti-goals (the program has failed if any of these is true, even if it runs)**

- Any path by which a staff member sees, or an agent reads, data belonging to a client they have no access to.
- Three applications glued together behind one screen, with three queues, three sign-ins or three databases.
- Deleting or modifying the three source repositories.
- A deployment reported as healthy when the write to the client's platform has an unknown outcome.
- A vendor integration described as working when it has never been run against a real platform.

## 3. Constraints

Decided by the user:

- Built in `s:\Development_2026\shi_work\agt_trus_harness`. The three source repositories are kept and are not changed.
- One codebase with modules. SETH is the front end the team uses.
- One PostgreSQL database, on a PostgreSQL 17 instance the user hosts. Creating roles is allowed. Connections use TLS.
- Generated client front ends are React (web), not React Native.
- A separate executor process is the only process that holds deployment credentials (the ones that can change a client's platform) and runs vendor command-line tools. Read connections used while building and testing stay encrypted in SETH's database and are used by the worker, as in SETH today; they must use read-only credentials.
- Staff groups are SHI-wide: a group exists once and is given a role in each client it serves.
- No real client data enters SETH until the go-live step (sub-project 4) is done. Until then the instance holds test organizations only.
- SHI's own model keys are one provider shared across clients, switched off for each client until a platform administrator turns it on for that client.
- All ported code meets SETH's strict gates.
- Access for outside tools over MCP is a later module.
- SETH should be a multi-agent system that sends each request to the right specialist agent.
- The deployed Databricks result is a native Databricks application running in the client's workspace.

From the sources:

- Python 3.13 satisfies all three codebases. SETH's stack is kept: FastAPI, SQLAlchemy 2 (sync) with psycopg 3, Alembic, Pydantic v2, a PostgreSQL job queue (`FOR UPDATE SKIP LOCKED`, with `run_after` delay), no Redis; Next.js App Router, React 19, TypeScript, Tailwind, pnpm, Vitest.
- The development machine is Windows. `uv` needs `UV_LINK_MODE=copy`; scripts use `python`, not `python3`; git needs `core.longpaths true`.
- SETH and the SDK both use the package name `shifde`, the `SHIFDE_*` environment prefix and `shifde-*` commands. Only one can keep them.
- No adapter has been run against a real client platform. The SDK marks every native kind experimental with `liveValidated: false`. Agent Factory's Snowflake support is an offline simulation.
- SETH's last recorded release verdict was NO-GO with 2 critical and 23 major findings. Commit `3ba0e06` is titled "R1 release-blocker work", but `docs/planning/REMEDIATION_PLAN.md` still lists R1 to R6 as not started. The builder must confirm by test, not by commit title, that C1 (a worker crash during an approved write must not be reported as cancelled) and C2 (repointing a connection must not send a secret without `secret:use`) are fixed.

## 4. Approaches considered

1. **Three applications behind one front end.** SETH calls the SDK and Agent Factory as separate services. Fastest to a demo. Rejected by the user: three codebases, three queues and three databases to maintain, and three places where client isolation has to be right.
2. **Copy SETH into this directory and add to it.** Fast start. Carries over the 23 open major findings, the hour-long test suite, the per-client install machinery and 25 migrations written for a different deployment model, all before any new work begins.
3. **Rewrite from scratch, using the three as reference.** Cleanest result. Discards about 970 backend and 660 front-end tests that currently describe SETH's behavior, and re-opens every security decision SETH has already had audited.
4. **Clean skeleton, then move modules in one at a time (chosen).** Start an empty application with the gates, test layout and database baseline in place. Move SETH's modules in from the bottom layer up, each with its tests, fixing the audit findings that belong to that module as it arrives. Then move in the SDK and Agent Factory function as new modules. Slower than option 2 at the start; every module that lands is clean, tested and isolated per client, and the application runs at each step.

The user chose option 4.

## 5. Design

### 5.1 Shape of the application

One repository with `backend/`, `frontend/`, `scripts/` and `docs/`. One Python package. Four processes:

| Process | Role | Reaches |
|---|---|---|
| API | All requests from the front end and from executors | The database |
| Worker | Runs agents, pipelines, schedules and deployment timers from the job queue | The database, model providers, client data connections |
| Executor, one per client | Builds and deploys to that client's platform. Holds that client's deployment credential and the vendor command-line tools | The SETH API over outbound HTTPS only. Never the database |
| Front end | The Next.js application the team uses | The API |

An executor can run on SHI infrastructure or inside the client's network. In both cases it connects outward to SETH, and SETH never connects in.

Temporal, which the SDK uses only as a durable timer around deploy-then-poll (one 50-line workflow), is dropped. A deployment is a job in SETH's queue: the executor leases it, and the observe step is a job re-queued with `run_after` set to the poll interval. Writes to a client platform are attempted once and never retried automatically.

### 5.2 Modules

SETH's existing modules keep their names and their layer order (`core` at the bottom, then `adapters`, `audit | authz | observability | ledger`, `secrets`, `identity`, `tenancy`, `installation`, `vendors`, `connections`, `admin | models_admin`, `governance`, `build`, `ontology`, `runtime`, `app | cli`).

New modules:

| Module | Comes from | Does |
|---|---|---|
| `adapters.native` | SDK `packages/adapters`, `packages/contracts` | One adapter per client platform, each implementing the same ten operations: capabilities, inspect, scaffold, validate, build, plan, apply, observe, reconcile, verify |
| `delivery` | SDK `packages/core`, `packages/hosted` | Client targets, build artifacts, sealed plans that expire in 15 minutes, approval by a second person, the deployment journal, ownership of native resources, the executor gateway (lease, heartbeat, checkpoint, result) |
| `packaging` | Agent Factory `ir`, `compiler`, `targets`, `deployment` | Turns a solution version plus its environment bindings into an immutable release identified by its digest. Reports every unsupported feature by name instead of dropping it silently. Also holds the standalone Docker target |
| `evaluation` | Agent Factory `evaluations` | Runs a release against a dataset with assertions and thresholds (accuracy, latency, estimated cost). A release cannot be deployed until its evaluation passes |
| `appgen` | New | Generates the source of the native application from a solution: the agent service, the React front end and the platform files |
| `assist` | New | The supervisor and specialist agents inside SETH (section 5.7) |
| executor entry point | SDK `executors`, `hosted/executor.py` | The executor process |

Cost reservation before each model call (Agent Factory) goes into `ledger`. The Entra sign-in profile (Agent Factory) goes into `identity`.

The single package keeps the name `shifde`, the `SHIFDE_*` prefix and the `shifde-api`, `shifde-worker` and `shifde-admin` commands, and adds `shifde-executor`. SETH is 75% of the code, so its names win; the SDK's colliding commands disappear because its code becomes modules.

### 5.3 Client isolation

This is the first priority of the program, because one shared SETH now holds every client.

- **One organization per client.** Every client-owned row carries the organization id. Workspaces and projects sit beneath the organization, as in SETH today.
- **The database enforces it.** Every client-owned table has forced row-level security. The application connects as a role that does not own the tables and cannot bypass row-level security, and sets the organization for each transaction. A second role, used only for migrations and maintenance commands, owns the schema.
- **The application refuses to start** if its database role is a superuser, owns the tables or can bypass row-level security. SETH's audit found nothing checks this today.
- **A schema test fails the build** if any table lacks a row-level security policy, unless the table is on a short written list of global tables (for example the list of organizations, price lists, the registry of client platform addresses).
- **A cross-client test exists for every module.** For each route, a user from organization A requests an object from organization B and must be refused. These tests arrive with the module, not later.
- **Access is through SHI-wide groups.** A group (for example "Databricks FDEs") exists once, at platform level, and holds staff. A group is bound to a role inside a client at organization, workspace or project level. With no binding to a client, a user cannot see that the client exists. This replaces SETH's per-client groups, where every group belongs to one organization and there is no way to give an existing user access to a second one. Entra groups map onto these groups in sub-project 3.
- **Who may grant access.** A platform administrator, or the owner of that client, may bind a group to a role in a client. Administering SETH does not by itself grant sight of client content. A platform administrator may add themselves to a client; that is always audited and is shown to the client's owner.
- **A client platform belongs to one organization.** A Databricks workspace address or Snowflake account can be registered as a deployment target by exactly one organization. A second registration is refused with a message that does not reveal which organization holds it. This rule arrives with deployment targets in sub-project 2, not in sub-project 1.
- **An executor belongs to one organization.** It can lease only that organization's jobs and receive only that organization's credential.
- **Artifacts** are stored under the organization and checked against it on every read.
- **Agent memory and run history** never cross organizations. A specialist agent in SETH works inside the organization the user is currently in.
- **Client data and model providers.** While building and testing, an agent may read client data. A client may use model providers configured inside its own organization (for example models served in the client's own platform). SHI's own model keys are one platform-level provider, shared across clients, and switched off for each client by default. A platform administrator turns it on for a client; the change is audited and shown to the client's owner. A run in a client where it is off cannot reach it. This setting and its gate are delivered in sub-project 1 with `models_admin`.
- **Client credentials.** Read connections used for building and testing are stored encrypted under the client's organization and used by the worker. Whoever creates one confirms it is a read-only credential, and tools that write through a client connection stay behind SETH's existing approval gate. Deployment credentials are different: only that client's executor ever holds one (sub-project 2).

### 5.4 Database

- One database on the user's PostgreSQL 17 instance, reached over TLS, holding everything. Connection details are supplied in a local environment file that is not committed.
- One Alembic history. The first migration creates roles' privileges and shared machinery only. Each module brings its own tables in its own migration branch when it is ported, so modules can be developed in parallel without collisions. SETH's 25 migrations are squashed into one per module.
- For ported SETH modules, the resulting tables must match SETH's schema at `3ba0e06`, checked by an automated comparison, apart from deliberate changes that are listed in the migration.
- Every migration has a downgrade path.
- No data is migrated. The new database starts empty and is filled by a development seed script.
- Tests and daily development use a local PostgreSQL cluster on the developer's machine, with a throwaway database per test worker. The hosted instance holds the shared SETH database only; tests never create or drop databases there.

### 5.5 From a solution to a native application

This is the central piece that none of the three sources has. Today the SDK deploys a fixed template (for Databricks, a small Flask application with a health check that runs `SELECT 1` on a SQL warehouse) and the FDE writes the application by hand. SETH defines agents and workflows but can only run them inside SETH.

A **solution** is a new versioned item in a project. It names a published agent or orchestration from SETH's builder, the data it reads, the screens of its front end and the kind of target it is for.

`packaging` checks the solution against what the target kind supports and either produces a sealed release or a report naming each unsupported feature. `appgen` generates the source. `delivery` and the executor deploy it.

For Databricks, the generated application is a Databricks App containing:

- a Python service that runs the agent loop inside the app, calling model endpoints served in the client's workspace and reading tables through the client's SQL warehouse under the app's own identity;
- a React front end, built and served by that service, that reads the same datasets;
- the platform files (the app configuration and the asset bundle), which are generated and cannot be edited by hand.

The first version supports a deliberately small subset: agents and orchestrations whose tools are read-only queries over the client's tables. Tools that write, call outside services or need a human approval step are reported as unsupported until each is designed.

For Snowflake, the result is a Snowflake Native App. The SDK's template uses a Streamlit screen; a React front end there needs Snowflake's container service, which is unproven. That decision belongs to the Snowflake sub-project.

### 5.6 Cost

SETH's ledger (prices, usage, budgets, enforcement) is the single cost system. Agent Factory's rule is added: the estimated cost of a model call is reserved against the budget before the call is made, and the reservation is stored with the run's checkpoint so a crash cannot lose it. Budgets and reports are per client, per project and per user.

This covers spend inside SETH while building and testing. A deployed application spends on the client's own platform account, and SETH does not see it. The generated application records its own token usage in a table in the client's platform so the client can see it.

### 5.7 SETH as a multi-agent system

SETH's builder already supports graphs of agents with child runs. What is missing is choosing the specialist at run time. `assist` adds a supervisor that reads a request, picks one specialist and hands over, with each specialist limited to the permissions of the signed-in user and to the current organization:

- research (from Agent Factory's research assistant and document reviewer presets),
- solution design,
- build (drafts agents, tools, prompts and screens for the FDE to accept),
- evaluation,
- cost,
- deployment (prepares plans; never approves them).

The specialists are ordinary SETH agents built with SETH's own builder. They draft and propose; a person accepts, and deployment approval stays with a second person.

## 6. Sub-projects, in order

Each sub-project gets its own short design and plan when it is reached, and ends with the application running. Only sub-project 1 is planned from this document.

| # | Sub-project | Done when | Needs from the user |
|---|---|---|---|
| 1 | Foundation and SETH port | Today's SETH behavior runs from this repository as one shared instance on the hosted database, with the isolation rules of 5.3 proven by tests | Database connection details in a local file |
| 2 | Native delivery, Databricks | The SDK's template Databricks App is planned, approved by a second person, deployed and verified from SETH's screens against a real workspace | A Databricks development workspace and a credential for it |
| 3 | Releases, evaluation, cost reservation, Entra sign-in | A release cannot be deployed without a passing evaluation; staff sign in with Entra | An Entra application registration |
| 4 | Go-live | Every remaining major audit finding is closed or accepted in writing; production mode, trusted-proxy and header settings, backup and restore, and process control are in place and tested. Only after this may real client data enter SETH | Where SETH will be hosted |
| 5 | Solution generator for Databricks | The flow in section 1 works end to end for a read-only agent with a React front end | The same workspace, with sample data |
| 6 | Snowflake | The same flow produces a Snowflake Native App | A Snowflake development account |
| 7 | Supervisor and specialists; project and release board | A request in SETH reaches the right specialist; each client's solutions show their release and target status | Nothing |
| 8 | MCP access; Fabric; Palantir; standalone Docker target | Decided when reached | To be decided |

### Sub-project 1 in detail

**Skeleton first.** A repository with the backend package, the front-end application, the local database script, the strict gates (ruff, mypy strict, import-linter with SETH's contracts, secret scan), the test layout and continuous integration, all passing on an application that only serves a health check. The baseline migration and both database roles exist. The startup check on the database role exists and is tested.

**Then modules, bottom layer first**, in SETH's own layer order: `core`; `adapters`; `audit`, `authz`, `observability`, `ledger`; `secrets`; `identity`; `tenancy`; `vendors`; `connections`; `admin`, `models_admin`; `governance`; `build`; `ontology`; `runtime`; the API application and command-line entry points. `installation` is ported only as far as other modules need it.

For each module:

1. Copy the module and its tests from SETH at `3ba0e06`.
2. Write its migration branch and confirm the schema comparison.
3. Add its cross-client tests.
4. Fix the audit findings that belong to it. C1 and C2 are confirmed by test before `runtime` and `connections` are accepted; the remaining security items from SETH's remediation batch R2 are fixed in the module they belong to.
5. Port the matching front-end area and its tests.
6. All gates pass before the next module starts.

**Test layout.** Tests live beside the module they cover and can be run per module. The full suite runs in parallel with one database per worker. Targets: one module's tests in under 10 minutes, the full backend suite in under 20 minutes (it takes 1 hour 13 minutes in SETH today). If the targets cannot be met, the builder reports the measured numbers rather than removing tests.

**Shared-instance changes.** Where SETH assumes one client per installation, the organization becomes the client. The per-client installation profile, branding and install validator are not ported in this sub-project. Two changes are new work, not a port, and land with the modules that own them: SHI-wide groups with the rules on who may grant access (`identity`, `authz`, `tenancy`), and the platform-level SHI model provider with its per-client switch (`models_admin`). Both come with their screens and cross-client tests.

**Audit findings in scope.** Sub-project 1 closes: the two critical findings (C1, C2); the security batch (R2 in SETH's `docs/planning/REMEDIATION_PLAN.md`), for the code that is ported; and the three cost-accuracy findings from batch R3 (budget overshoot, `max_cost`, usage honesty). Every other major finding is listed in the build status as a known defect and becomes an issue for the factory after sub-project 1; all are closed or accepted in writing by the go-live step. Findings about code that is not ported (the install profile, the install validator, restore scripts) are dropped with a note saying so.

**The instance at the end of sub-project 1** is a proving ground. The processes are started by hand on the development machine against the hosted database, which holds test organizations and local-login test accounts only.

**Which isolation rules sub-project 1 proves.** From section 5.3: one organization per client, database enforcement, the startup role check, the schema test, the cross-client tests, access through groups, artifacts, and agent memory and run history. The executor rule and the one-platform-per-organization rule are proven in sub-project 2, when executors and deployment targets exist.

**Where SETH does not fit the per-module recipe.** SETH's tests are organised by build phase, not by module, and its test harness imports the application factory and the seed command. Some lower-layer tables have foreign keys to tables owned by modules ported later (`tenants`, `runs`). The builder reorganises tests by module, builds the shared test harness with the skeleton, and adds each such foreign key in the migration of whichever module arrives second.

**Done when:** every ported module's tests pass in the new repository; the schema comparison passes; the cross-client tests pass for every route; the application refuses to start under a privileged database role; two test organizations with separate groups can be used side by side on the hosted database with neither visible to the other's users.

## 7. Calls made by the designer

These were proposed by the designer and accepted by the owner on 2026-11-13. Call 9 was replaced by the model-provider rule in section 5.3.

1. Only SHI staff sign in to SETH.
2. Deployed applications are self-contained and do not call back to SETH.
3. The base is SETH `main` at `3ba0e06`.
4. The package, environment prefix and commands keep SETH's `shifde` names.
5. No data is migrated from the three existing databases.
6. Tests and daily development use a local PostgreSQL cluster; the hosted instance holds only the shared SETH database.
7. The generated Databricks application runs the agent inside the Databricks App, using model endpoints and a SQL warehouse in the client's workspace.
8. The first generator version supports read-only data tools only.
9. By default, client data is sent only to the client's own platform models during building and testing.
10. Administering SETH does not by itself grant sight of client content.
11. Executors are per client and may be hosted by SHI or inside the client's network.
12. Project management means engagement, project, solution and release status, not task tracking.
13. Entra sign-in arrives in sub-project 3, not sub-project 1. Until then sign-in is SETH's existing local login.
14. Continuous integration runs on GitHub Actions, in a new repository the user creates when ready. Nothing is pushed without the user asking.
15. Per-client installation, branding and the install validator are dropped from the first sub-project.

## 8. Left to the builder

- Exact directory layout inside `backend/` and `frontend/`, and where tests sit relative to modules.
- The exact position of the new modules in the import-linter layer list, provided `adapters.native` stays in the adapter layer and `delivery`, `packaging`, `evaluation`, `appgen` and `assist` do not import each other in a cycle.
- How migration branches are named and ordered.
- The local database port and script names, provided they do not collide with SETH's development cluster on port 55440.
- Parallel test tooling.
- Order of front-end areas within a module's port.

## 9. Known unknowns

- No adapter has run against a real platform. Sub-projects 2, 5 and 6 cannot be called done without the workspaces listed in section 6.
- Where SETH will be hosted. Needed for the go-live step, not before.
- Whether a React front end can ship inside a Snowflake Native App without the container service.
- Whether SETH's job queue, after the C1 fix, is sufficient for deployment timers without change. It has delayed jobs and leases; the unknown-outcome handling for platform writes must be shown by test in sub-project 2.
- Whether the hosted PostgreSQL instance permits the row-level security role setup as designed. This is checked in the first step of sub-project 1.
