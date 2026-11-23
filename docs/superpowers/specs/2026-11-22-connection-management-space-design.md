# Connection management space (#156) — short design

Date: 2026-11-22. Status: settled by the lead after the builder read (section 14, B-2743..B-2758); CHARTER 7 steps 1-2; owner priority S-025. Contract: unified design sections 3, 5.3 ("Client credentials", "Client data and model providers", "A client platform belongs to one organization"), 6; GitHub issue #156.

## 1. Intent and "done when"

The owner asked for one place per client to set up Databricks, Snowflake and every other connection, per client and project, "so each is unique".

**Done when:** in one client's Connections space an administrator creates a Databricks connection for project P1 and a Snowflake connection for project P2, each with its own credential. The administrator tests, discovers, edits, rotates the credential of, and archives each one against the in-process fakes, and sees the client's model providers and delivery targets in the same space with status, last check and scope.

The isolation side of "done":
- A member of P1 only cannot see or use P2's connection or credential (404).
- Another client gets 404 on every new or changed route.
- A P2 connection cannot be bound to a P1 secret, and the database refuses it too.

The checks:
- `python scripts/connections_demo.py --fake` ends `CONNECTIONS DEMO PASSED`.
- The Databricks and Snowflake connection adapters are `experimental`.
- STATUS records "live validation waiting on owner input" until the opt-in live tests (7.5) pass.

Nothing here talks to a real Databricks or Snowflake. The fakes are in-process httpx transports. The live tests are opt-in, owner-only, and skipped without credential files.

## 2. Scope and non-goals

In scope:
- Scope and credential isolation rules for connections (section 4), enforced in the service and in the database.
- A connection-level credential lifecycle: inline credential at create, set or rotate, and disable on archive.
- Real `databricks` and `snowflake` connection adapters for read connections (test, discover, read-only SQL invoke), with fakes, conformance suites run against the fakes, and live tests.
- The Connections space screen and the uniform flow (wizard, test, discover, edit, rotate, archive).
- A done-when demo script.

Non-goals:
- Databricks or Snowflake **deployment**. Delivery targets stay as sub-project 2 built them; Snowflake native apps are sub-project 6.
- Snowflake OAuth and Private Link hosts.
- Microsoft Fabric (stays `planned`).
- Ontology pushdown over the new adapters (they do not declare `query.pushdown_join`).
- New permissions, role changes or permission relaxation (B-2748).
- Moving connection discover/invoke onto the worker (#144). The new adapters run unchanged under it whenever it merges.

## 3. What exists and what is reused

| Need | Exists (reuse) | Gap closed here |
|---|---|---|
| Scope | `connections.workspace_id/project_id` (`ck_connections__scope`); `ScopeSlugs` on create; list filtered by `pdp.visible_scopes("connection:read")` (`repository.list_connections`); `ConnectionPlacement`; `packaging.service.in_scope` (organization-wide and workspace-wide objects are in a project's scope) | Names are unique per **client** (`uq_connections__tenant_name`), not per scope. There is no list of "this project plus what it inherits". |
| Credentials | `secrets` with `scope_type/scope_id` (`trg_secrets_scope` keeps them in the client); `fk_connections__secret` composite key; `connections_guard` (same client only); `_check_secret` (`secret:use` at the secret's own scope); `secrets.service.create_secret/rotate_secret/disable_secret` (`MAX_SECRET_BYTES` = 16384 UTF-8 bytes; kinds `generic`, `api_key`, `password`, `token`, `pem`) | **Nothing ties a secret's scope to the connection's scope.** `tests/connections/test_routes_cross_client.py::test_a_secret_of_another_workspace_cannot_be_bound` says so. A tenant-wide administrator can bind P1's secret to P2's connection. The wizard picks any visible secret. There is no rotate from the connection. |
| Model providers | `model_providers.connection_id` -> `connections (tenant_id, id)`; `create_provider` checks only `connection:read` and `model_api`. `ProviderPatch` has no `connection_id`, and `shifde_app` has no UPDATE on it. | A client-wide provider can wrap a project-scoped connection, which hands P1's credential to every project's runs. |
| Adapters | `AdapterDescriptor` (config schema, `secret_fields` hidden from `ConnectionOut.config`, `auth_methods`, `auth_only_fields`, `destination_fields`); `ConnectionAdapter` protocol; `EgressGuard` with injectable `resolver` and `inner`; `IntegrationRuntime` (`settings`, `registry`, `resolver`, `inner` hooks); `adapters/oauth2.py` token cache keyed `(tenant, connection, fingerprint)`; `conformance.run_suite`; `native/addresses.normalize_databricks_address` with `DATABRICKS_HOST_SUFFIXES` and `settings.delivery_databricks_host_suffixes` | `databricks` and `snowflake` are `adapters/planned.py` entries; test, discover and invoke answer 422 `adapter_not_implemented`. |
| Delivery targets | `/orgs/{org}/delivery/targets` (workspace/project scoped; normalized `address`; `credential_ref` is a name the **executor** resolves locally; one-platform claim) | Shown only under Deliver. |
| UI (verified in `frontend/src`) | `components/integrate/{ConnectionsView, ConnectionWizard, ConnectionDetailView, ConnectionTests, ConnectionConsole, ProvidersView, ProviderDetailView, SchemaForm, SlugScopeSelect, Maturity}.tsx`, `components/deliver/TargetsView.tsx`, `components/ConfirmDialog.tsx`, `components/DashboardView.tsx` (the project page `app/(app)/o/[org]/w/[ws]/p/[proj]/page.tsx` renders it); hooks `useConnections`, `useProviders`, `useAdapters` (`lib/integrate-hooks.ts`), `useTargets` (`lib/delivery-hooks.ts`), `useSecrets` (`lib/security-hooks.ts`); `NAV_ITEMS` in `lib/nav.ts`; `integrateHref`/`deliverHref` in `lib/scope.ts`; `lib/schema-form.ts` `fieldsFromSchema` | Three separate places. The wizard asks for an existing secret instead of the credential. |
| Audit | `connection.created/updated/status_changed/tested/discovered/used/invoked` via `shifde.audit.service.record` (`ConnectionService._audit`); `secret.created/rotated/disabled` today only from `shifde.admin.providers` | `connection.credential_set` and `connection.credential_rotated`; secret events emitted from `shifde.connections.service` (B-2752). |

## 4. Scope model and credential isolation (the rules)

1. **Scope.** A connection belongs to one client and to exactly one of three scopes:
   - client (`workspace_id` NULL),
   - workspace (`project_id` NULL),
   - project.

   Scope is fixed at create: `ConnectionPatch` has no scope field, and `shifde_app` has no UPDATE on `workspace_id`/`project_id` (unchanged).
2. **Shared connections exist and are marked as shared.** Client-wide and workspace-wide connections are a project's *shared* connections, as `in_scope` already treats them. The UI labels them "Shared: client-wide" or "Shared: workspace <slug>".
   - No grant is implied: a project member sees a shared connection only with `connection:read` at its scope (PDP unchanged).
   - Project connections are never visible from another project.
3. **Names are unique within their scope:** `(tenant_id, workspace or none, project or none, name)`, case-sensitive.
4. **A connection's secret has exactly the connection's scope:**
   - a project connection takes only a `project` secret of that project;
   - a workspace connection takes only a `workspace` secret of that workspace;
   - a client connection takes only a `tenant` secret.

   To share a credential, make a shared connection. The rule holds in the service and in the database (`connections_guard`, L1).
5. **Status codes (B-2745, B-2746):**
   - `_check_secret` runs first, unchanged: an unknown secret, another client's secret, or one the caller cannot read gets 404 `secret not found`; a missing `secret:use` gets 403.
   - Then the scope check: a readable secret of the wrong scope gets 422 `secret_scope_mismatch` ("the secret must have the connection's scope").
   - The scope check runs on create and on any PATCH that changes `secret_id`, `workspace_id` or `project_id` (the last two cannot be patched today, so in practice `secret_id`).
   - Database errors are mapped by SQLSTATE: `23505` -> 409 "a connection with this name already exists in this scope" (create and PATCH rename); `23514` -> 422 carrying the guard's message; any other `IntegrityError` is re-raised.
6. **A model provider's connection is client-wide** (`workspace_id IS NULL`). Otherwise the service returns 422 "a model provider needs a client-wide model_api connection", and L2 enforces it in the database.
7. **The credential lives in the connection's scope:**
   - An inline credential (create or `PUT .../credential`) becomes a secret named `connection-<connection_id>` with the connection's scope. The kind follows the auth method: `bearer` and `oauth2_client_credentials` -> `token`, `key_pair` and `mtls` -> `pem`, `api_key` -> `api_key`, `basic` -> `password`. No new kind.
   - Creating, setting or rotating it needs `connection:manage` **and `secret:manage`** at the connection's scope ref. There is no permission relaxation (B-2748).
   - Secrets with the reserved `connection-` prefix are **owned by their connection**. `POST /orgs/{org}/secrets` refuses the prefix (422), and the admin rotate, PATCH and disable routes refuse an owned secret (409 "this secret belongs to a connection; manage it from the connection") (B-2750).
   - Archiving disables the owned secret unless another live connection (`status <> 'retired'`) binds it (B-2751).
8. **Delivery credentials never enter a connection.**
   - A read connection's credential is stored encrypted under the client and must be read-only: `read_only_confirmed: true` is required whenever `access_mode` is `read` and a credential is bound, whether by `secret_id` or inline (B-1507, B-2747).
   - A delivery target's deployment credential stays with the client's executor (`credential_ref`, design 5.3).

## 5. Data model: lead-written prerequisites (protected paths)

No new table and no new global table, so `tests/schema/global_tables.txt` and the RLS schema test are unchanged. Every table touched keeps forced row-level security and its policies. Every new function is **invoker** (not SECURITY DEFINER, so RLS applies inside it) and gets `REVOKE ALL ... FROM PUBLIC`. The startup role check is untouched. The lead writes both migrations and their yaml entries, has `isolation-reviewer` read them, and hand-merges them (B-2705 pattern) **before CM-1**, which depends on both.

**Helpers (B-2744).** Each file defines the module-local `_run(*statements)` used by every migration here. L1 also copies `_previous(module)` from `runtime_0003_claim_active_clients.py` to load `connections_0001_connections.CONNECTIONS_GUARD` for the new body's unchanged part and for the downgrade. `_touch` and `_tenant_table` are not needed: no table is created.

**Precheck (B-2743).** A count taken as `shifde_owner` with no client context could pass vacuously, because `tenants` and every client table are under forced RLS. No existing migration has a per-client loop helper; `tenancy_0002` and `runtime_0003` only reference `shifde.tenants` in SQL. So each precheck is one `DO` block:
1. It sets `app.platform_admin = 'on'` (transaction-local) to list every row of `shifde.tenants`; the `tenant_isolation` policy admits the platform-administrator context.
2. Per client it sets `app.platform_admin = ''` and `app.tenant_id = <id>` with `pg_catalog.set_config(..., true)`, then adds that client's violating rows to a total.
3. It resets both settings to `''`.
4. If the total is above 0 it raises `<revision>: <n> <rows> violate <rule>`: the count only, never ids or names.

`set_config` inside a `DO` block is not stored in the catalog, so `tests/schema/catalog_scan.py` is unaffected.

**L1 `connections_0002_scope_isolation`** (branch `connections`, `down_revision = "connections_0001"`, `depends_on = ("tenancy_0001", "secrets_0001")`). Upgrade:
1. Precheck with the predicate `c.secret_ref_id IS NOT NULL AND NOT <scope match>` over `connections c JOIN secrets s ON s.tenant_id = c.tenant_id AND s.id = c.secret_ref_id`. Message: `connections_0002: <n> connection(s) bind a secret of another scope`.
2. `ALTER TABLE shifde.connections DROP CONSTRAINT uq_connections__tenant_name`.
3. `CREATE UNIQUE INDEX uq_connections__scope_name ON shifde.connections (tenant_id, COALESCE(workspace_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid), name)`.
4. `CREATE OR REPLACE FUNCTION shifde.connections_guard()`: the three `connections_0001` checks unchanged and in the same order (another client's secret keeps its message), then:
   ```sql
   IF NEW.secret_ref_id IS NOT NULL AND NOT EXISTS (
     SELECT 1 FROM shifde.secrets s
     WHERE s.id = NEW.secret_ref_id AND s.tenant_id = NEW.tenant_id
       AND ((NEW.project_id IS NOT NULL AND s.scope_type = 'project' AND s.scope_id = NEW.project_id)
         OR (NEW.project_id IS NULL AND NEW.workspace_id IS NOT NULL
             AND s.scope_type = 'workspace' AND s.scope_id = NEW.workspace_id)
         OR (NEW.workspace_id IS NULL AND s.scope_type = 'tenant')))
   THEN RAISE EXCEPTION 'secret scope must match the connection scope' USING ERRCODE = '23514';
   END IF;
   ```
   Then `REVOKE ALL ON FUNCTION shifde.connections_guard() FROM PUBLIC`.
5. `DROP TRIGGER trg_connections_guard ON shifde.connections`, then `CREATE TRIGGER trg_connections_guard BEFORE INSERT OR UPDATE OF secret_ref_id, egress_policy_id, vendor_id, workspace_id, project_id ON shifde.connections FOR EACH ROW EXECUTE FUNCTION shifde.connections_guard()`.
6. Grants are unchanged. `shifde_app` has no UPDATE on `secrets.scope_type/scope_id` and none on `connections.workspace_id/project_id`.

Downgrade restores the exact `connections_0001` state:
1. Drop and recreate the function from `_previous("connections_0001_connections").CONNECTIONS_GUARD`, plus the REVOKE.
2. Recreate the trigger exactly as `connections_0001` did: `BEFORE INSERT OR UPDATE OF secret_ref_id, egress_policy_id, vendor_id`.
3. Drop `uq_connections__scope_name`.
4. Re-add `CONSTRAINT uq_connections__tenant_name UNIQUE (tenant_id, name)`. This fails if same-named connections now exist in two scopes; documented in the docstring.

**L2 `models_admin_0004_provider_connection_scope`** (branch `models_admin`, `down_revision = "models_admin_0003"`, `depends_on = ("connections_0001",)`). The existing `trg_model_providers_guard` (vendor check, SETH plus its yaml entry) is left untouched. Upgrade:
1. Precheck with the predicate `p.connection_id IS NOT NULL AND c.workspace_id IS NOT NULL` over `model_providers p JOIN connections c ON c.tenant_id = p.tenant_id AND c.id = p.connection_id`. Message: `models_admin_0004: <n> model provider(s) use a workspace or project connection`.
2. Create the function:
   ```sql
   CREATE FUNCTION shifde.model_providers_connection_guard() RETURNS trigger LANGUAGE plpgsql AS $$
   BEGIN
     IF NEW.connection_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM shifde.connections c
       WHERE c.tenant_id = NEW.tenant_id AND c.id = NEW.connection_id AND c.workspace_id IS NULL)
     THEN RAISE EXCEPTION 'a model provider needs a client-wide connection' USING ERRCODE = '23514';
     END IF;
     RETURN NEW;
   END $$
   ```
   Then `REVOKE ALL ... FROM PUBLIC`.
3. `CREATE TRIGGER trg_model_providers_connection_scope BEFORE INSERT OR UPDATE OF connection_id, tenant_id ON shifde.model_providers FOR EACH ROW EXECUTE FUNCTION shifde.model_providers_connection_guard()`. No route updates `connection_id` today; the trigger covers any future PATCH (B-2754).

Downgrade drops the trigger, then the function.

**What `tests/schema` compares (B-2744).** `tests/schema/snapshot.py` compares, per table: columns, constraints, indexes (`pg_get_indexdef`), triggers (`pg_get_triggerdef`), RLS flags, policies and privileges. It does **not** compare function bodies. `test_function_schema.py` pins only the five base helpers and SECURITY DEFINER functions, so changing the invoker body of `connections_guard` needs no yaml entry. The prose for `shifde.connections.connections_vendor_id_fkey` in `test_rls_schema.py` (around line 1394) describes `trg_connections_guard` as refusing another client's vendor. That stays true, so it needs no edit.

`tests/schema/deliberate_changes.yaml` entries, exactly:
```yaml
- table: connections
  path: [constraints, uq_connections__tenant_name]
  reason: "names are unique per scope, not per client (#156): replaced by the unique index uq_connections__scope_name on (tenant_id, workspace or zero, project or zero, name)."
  migration: connections_0002

- table: connections
  path: [indexes, uq_connections__tenant_name]
  reason: "index of uq_connections__tenant_name, dropped with it; names are unique per scope (#156)."
  migration: connections_0002

- table: connections
  path: [indexes, uq_connections__scope_name]
  reason: "names are unique within a connection's scope (client, workspace or project) (#156)."
  migration: connections_0002

- table: connections
  path: [triggers, trg_connections_guard]
  reason: "connections_guard also requires the bound secret's scope to equal the connection's scope (#156); the trigger also fires on workspace_id and project_id (defense in depth: shifde_app cannot update them)."
  migration: connections_0002

- table: model_providers
  path: [triggers, trg_model_providers_connection_scope]
  reason: "a client-level model provider cannot carry a workspace's or project's credential: its connection must be client-wide (#156)."
  migration: models_admin_0004
```

No migration is needed for Databricks or Snowflake. These values already exist: `connection_type 'data_platform'`; `auth_method` values `bearer`, `oauth2_client_credentials` and `key_pair`; discovery `kind 'schema'`; status `retired` (= archived); secret kinds `token` and `pem`.

## 6. API (all under `/api/v1`, all PDP-checked, all 404 for another client)

**`GET /orgs/{org}/connections`** (CM-1). New `include_shared` (bool, default false; needs `workspace`). One query extends `repository.list_connections` with an OR of scope conditions:
- `project_id = :project`
- `(workspace_id = :workspace AND project_id IS NULL)`
- `workspace_id IS NULL`

This runs under RLS, intersected with the existing `visible_scopes` predicate, with the existing `id > :after` cursor and `ORDER BY id`. With `workspace` only, it returns workspace-wide plus client-wide rows.

**`ConnectionOut.last_test_outcome`** (CM-1): `"pass" | "fail" | null`. It comes from a `LEFT JOIN LATERAL (SELECT t.outcome FROM shifde.connection_tests t WHERE t.tenant_id = c.tenant_id AND t.connection_id = c.id ORDER BY t.started_at DESC LIMIT 1)` in the list and get queries (index `ix_connection_tests__connection` exists). There is no per-row query.

**`POST /orgs/{org}/connections`** (CM-1, CM-2):
- Rules 4.4 and 4.5.
- New optional `credential: {value}` (pydantic `SecretStr`), mutually exclusive with `secret_id` (422 if both).
- The value limit is checked in bytes by `secrets.service._check_value` (1..16384 UTF-8 bytes, 422).
- The inline credential needs `secret:manage` at the connection's scope (B-2748) and `read_only_confirmed` as for `secret_id` (B-2747).
- The secret `connection-<id>` and the connection are created in one transaction, and the inline value is checked by `validate_credential` (CM-3/CM-4 adapters).

**`PATCH /orgs/{org}/connections/{id}`** (CM-1): rule 4.5 on `secret_id`; the 23505/23514 mapping on rename and rebind.

**`PUT /orgs/{org}/connections/{id}/credential`** (CM-2, new). Body `{value: SecretStr, row_version: int, read_only_confirmed: bool | null}`; `row_version` is the **connection's** (B-2749). Check order:
1. Connection unreadable -> 404.
2. No `connection:manage` on it -> 403.
3. No `secret:manage` at its scope ref -> 403.
4. Status `retired` -> 409.
5. `auth_method = 'none'` -> 422.
6. Invalid value (byte limit, then the adapter's `validate_credential(auth_method, value)`, messages never echo the value) -> 422.
7. `row_version` mismatch -> 409.
8. Read mode without `read_only_confirmed: true` -> 422.

Effects:
- **No secret bound:** creates `connection-<id>` (kind per 4.7, connection's scope), binds it, and audits `connection.credential_set {secret_id, version: 1}` and `secret.created`.
- **A secret bound:** reads it and its `secret_version` inside the same transaction, rotates it in place (`secrets.service.rotate_secret` with that version), and audits `connection.credential_rotated {secret_id, version, shared_with: <n other live connections binding it>}` and `secret.rotated`.
- In both cases it sets `health_status = 'unknown'`, bumps the connection's `row_version`, and returns `ConnectionOut`.

**`POST /orgs/{org}/connections/{id}/status`** with `retired` (CM-2, B-2751):
1. Check `row_version` first.
2. Change the status.
3. In the same transaction, if the bound secret is owned (`connection-<id>`) and no other live connection binds it, disable it (`secrets.service.disable_secret` with its current version) and audit `secret.disabled`.

**`POST /orgs/{org}/secrets`** (CM-2): names starting `connection-` get 422.

**`POST /orgs/{org}/secrets/{id}/rotate` and `PATCH /orgs/{org}/secrets/{id}`** (CM-2): an owned secret gets 409 (B-2750), checked after the existing 404 and `secret:manage` checks.

**`POST /orgs/{org}/model-providers`** (CM-1): a non-client-wide connection gets 422 (4.6); a 23514 from L2 maps to the same 422.

**Test, test-jobs, tests, discover, discoveries/latest, invoke:** unchanged routes. They now run the real `databricks` and `snowflake` adapters (CM-3, CM-4).

**Audit (B-2752).** `connection.*` events go through `ConnectionService._audit` (`shifde.audit.service.record`). `shifde.connections.service` emits `secret.created`, `secret.rotated` and `secret.disabled` itself through `shifde.audit.service.record`, with the field shapes `shifde.admin.providers` uses:
- `target_type="secret"`, `target_label=f"secret:{name}"`;
- details `{name, kind, scope_type}` for created, `{version}` for rotated, `{}` for disabled.

`shifde.connections` never imports `shifde.admin`: `admin` sits above `connections` in the layers. `shifde.secrets.service` stays audit-free for these events, as today.

**No aggregate route.** The space reads the three existing lists (B-2758: `GET /orgs/{org}/connections`, `GET /orgs/{org}/model-providers`, `GET /orgs/{org}/delivery/targets`), each already PDP-filtered and cross-client tested. `backend/openapi.json` is refreshed in CM-1 and CM-2.

## 7. Databricks and Snowflake connection kinds (CM-3, CM-4)

### 7.1 Placement, hooks, shared toolkit

New package `shifde/adapters/dataplatforms/` in the existing `adapters` layer:
- **`sqlapi.py`:** a statement submit/poll/cancel loop bounded by the guard deadline, a row cap, and a read-only statement check. HTTP status maps to `ErrorClass`: 401/403 `auth`, 404 `config`, 429 `rate_limit` (one retry if `Retry-After` fits the deadline), 5xx `upstream`, timeouts `timeout`.
- **`databricks.py`, `snowflake.py`.**
- **`fakes.py`:** `FakeDatabricksWorkspace` and `FakeSnowflakeAccount`, each an `httpx.MockTransport` handler that routes by the `Host` header (`_pinned_copy` keeps it).
- **`suites.py`.**

`registry.BUILTIN` and the `pyproject.toml` entry points move `databricks` and `snowflake` from `planned:*` to the new classes (same keys, `declared_maturity="experimental"`, capabilities `query.sql`, `discover.schema`). The registry sync test keeps both in step. `FabricPlanned` stays.

**Hooks (B-2753).** Adapters never read settings. Two optional methods are added to the protocol in `adapters/base.py`, with a frozen `AdapterRules(databricks_host_suffixes: tuple[str, ...])`:
- **`normalize_config(config, rules) -> config`.** `ConnectionService` calls it, when the adapter has it, on create and on any PATCH that sends `config`, before `_validate_definition` and before storing. `connections/api.py` `_connections` builds `AdapterRules` from `runtime.settings.delivery_databricks_host_suffixes` and passes it to `ConnectionService`. The worker path (`run_test_job`) never normalizes.
- **`validate_config`** checks shape only, not host suffixes. A stored host accepted by normalization therefore keeps validating, and egress decides at call time.
- **`validate_credential(auth_method, value) -> list[ConfigError]`.** It runs on an inline credential at create and on `PUT .../credential`.

**No new Python dependency.**
- Both platforms have REST SQL APIs that work over the existing `httpx`: the Databricks Statement Execution API 2.0 and Unity Catalog 2.1, and the Snowflake SQL API v2.
- The Snowflake key-pair JWT is RS256, signed with the existing `cryptography`. No PyJWT, no `snowflake-connector-python`, no `databricks-sdk`.
- Every request goes through `ctx.egress.client()`.

### 7.2 Databricks (`databricks`, connection_type `data_platform`)

**Config schema** (`additionalProperties: false`):
- `host` (required): normalized by `normalize_databricks_address(host, rules.databricks_host_suffixes)` to `https://<host>`.
- `warehouse_id` (`^[A-Za-z0-9-]{1,64}$`; required for invoke).
- `default_catalog`, `default_schema` (`^[A-Za-z0-9_][A-Za-z0-9_-]{0,254}$`).
- `catalogs` (at most 50).
- `client_id` (`auth_only_fields` for `oauth2_client_credentials`, where it is required).
- `max_rows` (1..10000, default 1000).
- `statement_timeout_seconds` (5..50, default 30).

Descriptor values:
- `destination_fields = ("host",)`.
- `secret_fields = ("token", "client_secret")`: never config keys under `additionalProperties: false`, and kept hidden from `ConnectionOut.config` and from the wizard's config form, as `rest_http` does.

**Auth:**
- **`bearer`** (PAT or service principal token).
- **`oauth2_client_credentials`** (OAuth M2M): `POST https://<host>/oidc/v1/token`, `grant_type=client_credentials`, `scope=all-apis`, Basic `client_id:client_secret`. Tokens are cached in `oauth2.TokenCache` under `(tenant, connection, sha256(secret, host, client_id))` and registered for redaction.

**Test** (existing check names):
- `config`, then `egress`/`dns`/`tls` by the guard.
- `auth`: `GET /api/2.0/preview/scim/v2/Me`.
- `capability`: `GET /api/2.1/unity-catalog/catalogs?max_results=1` and, with `warehouse_id`, `GET /api/2.0/sql/warehouses/{id}`. The warehouse state is reported; a test never starts a warehouse.

**Discover:**
- Catalogs (at most 50, or the filter), schemas (at most 200 per catalog), then tables (columns included, at most 2000 in total).
- Payload: `{"tables": [{"catalog", "schema", "name", "table_type", "columns": [{"name", "type", "nullable"}]}], "truncated": bool}`.

**Invoke** (`SqlInvokeIn`):
1. `POST /api/2.0/sql/statements` `{warehouse_id, statement, parameters, catalog, schema, wait_timeout, on_wait_timeout: "CANCEL", disposition: "INLINE", format: "JSON_ARRAY", row_limit: max_rows+1}`.
2. Poll until the deadline, then `POST .../cancel`.
3. More than `max_rows` rows sets `truncated`.

### 7.3 Snowflake (`snowflake`, connection_type `data_platform`)

**Config schema:**
- `account` (required; `^[A-Za-z0-9][A-Za-z0-9_-]{0,254}(\.[A-Za-z0-9-]{1,64}){0,2}$`). The host is `https://<account lowercased, "_" to "-">.snowflakecomputing.com`, and that is the only suffix.
- `user` (required).
- `role`, `warehouse`, `database`, `schema`.
- `databases` (at most 50).
- `max_rows` (1..10000, default 1000).
- `statement_timeout_seconds` (5..120, default 30).

Descriptor values: `destination_fields = ("account",)`, `secret_fields = ("private_key", "token")`.

**Auth:**
- **`key_pair`** (default). The secret is an unencrypted PKCS#8 PEM RSA key of at least 2048 bits; `validate_credential` parses it.
  - JWT claims: `iss = <ACCOUNT>.<USER>.SHA256:<base64 sha256 of the DER public key>`, `sub = <ACCOUNT>.<USER>` (ACCOUNT uppercased; only the locator part when a region follows), `iat`, `exp = iat + 300 s`.
  - Headers: `Authorization: Bearer <jwt>` and `X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`.
  - Cache key: `(tenant, connection, sha256(key, account, user))`.
- **`bearer`** (programmatic access token), sent with `X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN`.

**Test:** `config`, the guard checks, then `auth`+`capability` via `POST /api/v2/statements` running `SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_VERSION()`.

**Discover:**
1. `SHOW DATABASES` (at most 50, or the filter).
2. Per database: `SELECT table_schema, table_name, column_name, data_type, is_nullable FROM "<DB>".INFORMATION_SCHEMA.COLUMNS WHERE table_schema <> 'INFORMATION_SCHEMA' ORDER BY 1, 2 LIMIT 20000`, with the identifier quoted (`"` doubled).
3. The payload has the same shape as Databricks, with `catalog` = the database.

**Invoke:**
1. `POST /api/v2/statements` `{statement, timeout, database, schema, warehouse, role, bindings, parameters: {MULTI_STATEMENT_COUNT: "1"}}`.
2. A 202 is polled until the deadline, then cancelled.
3. Only the first partition is read, and rows are capped.

### 7.4 Read-only and egress

In `read` mode, `sqlapi.check_read_only(sql)` strips comments and accepts one statement whose first keyword is `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `DESC` or `EXPLAIN`. Anything else gets 422 before any call. For Snowflake (`dialect="snowflake"`), the check also reads `$$...$$` as a string and refuses the words `PROCEDURE` and `CALL` outside quoted text (B-3211). `read_write` needs `connection:write` and the existing approval gate. The read-only credential (B-1507) is the real control; the UI copy says the keyword check is defense in depth.

Egress is the AND of four layers: the adapter host rule, the installation default (https, port 443, public addresses only, no redirects), the client default, and the connection policy (`connections/egress.py` unchanged).

### 7.5 Fakes, conformance, live tests

**Fakes.** Fault switches:

| Switch | Fake behavior |
|---|---|
| `bad_token` | 401 |
| `forbidden` | 403 |
| `rate_limited` | 429 with `Retry-After` |
| `server_error` | 500 |
| `slow` | `PENDING`/202 forever |
| `oversize` | body over the response cap |
| `redirect` | 302 to another host |
| `warehouse_missing` | 404 |

`FakeSnowflakeAccount` **verifies** the JWT signature against a registered public key and checks `iss`, `sub` and `exp`. `FakeDatabricksWorkspace` serves the OAuth token endpoint and checks the Basic credentials. Tests build the guard with `resolver=lambda host: ["127.0.0.1"]` and `inner=<fake transport>`, so the real policy layers decide (loopback passes only the development/test installation layer).

**Conformance.** `DatabricksSqlSuite` and `SnowflakeSqlSuite` (in `dataplatforms/suites.py`) yield the fake from `target()`; pytest runs them with `run_suite`, and `outcome == "pass"` is required. They are not registered in `SUITES`, and the adapters define no `conformance_suite()`. As a result, `shifde-admin adapters verify` records no evidence, and maturity stays `experimental`.

**Development switch (B-2755).** `SHIFDE_DEV_FAKE_DATA_PLATFORMS` (bool, default false):
- Valid `SHIFDE_ENV` values (`core/settings.py` `Environment`) are `development`, `test` and `production`; the default is `production`.
- The switch is allowed only when `SHIFDE_ENV` is `development` or `test`. Otherwise the settings model validator raises `ValueError`, like the other guards in `core/settings.py`.
- When it is true, `connections/egress.build_guard` gives connections whose `adapter_key` is `databricks` or `snowflake` the fakes' transport and the loopback resolver. Other adapters are unchanged.
- It is used by the local e2e and the demo only.

**Live tests (opt-in, owner-only).** `backend/tests/adapters/dataplatforms/test_databricks_live.py` and `test_snowflake_live.py` follow `tests/adapters/native/test_databricks_live.py` exactly:
- `@pytest.mark.live`.
- A module-level `LIVE_ENV = Path(__file__).resolve().parents[4] / ".devdata" / "<name>.env"`.
- `pytest.skip` if the file is missing, or if `-m live` did not select the test (the native file's `live_selected` helper, imported or copied).
- `read_live_config` uses `dotenv_values` and raises a refusal naming keys only.

Credential files and keys:
- **Databricks:** `.devdata/databricks.env` keys `DATABRICKS_HOST`, `DATABRICKS_WAREHOUSE_ID` and `DATABRICKS_READ_TOKEN`. The read token is read-only and is **never** the deploy token `DATABRICKS_TOKEN`; skip if it is absent.
- **Snowflake:** `.devdata/snowflake.env` keys `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_FILE`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, and `SNOWFLAKE_ACCOUNT_PURPOSE=development` (any other value refuses).

Each test runs test, discover and `SELECT 1`. No value appears in any message, skip reason or repr. Runbooks: `docs/runbooks/snowflake-live-validation.md` (new) and a "read connection" section in `docs/runbooks/databricks-live-validation.md`.

**Stored planned rows.** Connections created under the planned `databricks`/`snowflake` keys keep their rows. A test or edit answers 422 per-field config errors until the config matches the new schema. There is no data migration.

## 8. Relationship with delivery targets and model providers

- **Databricks connection and delivery target: kept separate, shown together.**
  - A connection is a read path with a read-only credential in SETH; a target is a deploy path whose credential only the executor holds.
  - Both store `https://<host>` from the same normalizer, so the space shows "Same workspace as target <name>" (and the reverse) by exact match within what the caller can read.
  - The one-platform claim stays a target-only rule.
- **Model providers** are client-level rows over a client-wide `model_api` connection (4.6). The space lists them with `health_status` and `health_checked_at` as "last check", with the scope "Shared: client-wide".

## 9. UI (CM-6, CM-7)

**Route `/o/[org]/connections`** (new page `app/(app)/o/[org]/connections/page.tsx`, new component `components/integrate/ConnectionsSpaceView.tsx`):
- It is a new first item of the Integrate group in `lib/nav.ts` `NAV_ITEMS`, shown with any of `connection:read`, `model:read` or `delivery:target:read`. Its href comes from a new `connectionsHref` in `lib/scope.ts`.
- `app/(app)/o/[org]/integrate/connections/page.tsx`, `[id]/page.tsx` and `[id]/tests/page.tsx` redirect to `/o/[org]/connections`, `/o/[org]/connections/[id]` and `/o/[org]/connections/[id]/tests`. The existing `ConnectionDetailView` and `ConnectionTests` render under the new paths. Their links, and the links in `ConnectionsView` and `ProviderDetailView` that build `/integrate/connections/...`, are updated.
- The project page (`DashboardView`) gets a "Connections" link to `/o/[org]/connections?ws=&proj=` (the SP3-7 pattern).

**List:**
- Scope selector (`SlugScopeSelect`). With a project chosen, the list uses `include_shared=true` and tags inherited rows "Shared".
- Tabs: All, Data and tools (`useConnections`), Model providers (`useProviders`), Delivery targets (`useTargets`). Each tab is hidden without its read permission.
- Columns: name; kind (adapter display name and `Maturity` badge); scope; status; last test (time and `last_test_outcome`, or "never"); credential ("set", "not set" or "held by executor").
- Loading, empty, error and 403 states.

**Uniform flow** (all kinds; `ConnectionWizard` and `ConnectionDetailView` extended):
- **Wizard:** kind -> scope -> config fields from `config_schema` through `fieldsFromSchema`/`SchemaForm`, with only the chosen auth method's `auth_only_fields` -> credential -> read-only confirmation -> review.
  - The credential step offers a password-type field for the value. With `secret:manage` at that scope it also offers "use an existing secret of this scope": `useSecrets`, filtered to scope equal to the chosen one and excluding `connection-` names.
- **Detail:** Test (queue and poll), Discover (catalog, schema, table tree with a truncation notice), Edit (host-change warning), Rotate credential (`ConfirmDialog`-style dialog that never shows the old value and offers "Test now"), Archive (confirm; says the credential is disabled).
- Planned adapters keep "Declared - not validated".

## 10. Isolation and tests

Layout (B-2753). New backend files sit beside the module's existing helpers: `tests/connections/routes.py` (`connection`, `secret`, `grant`, `limited_user`) and `tests/connections/conftest.py` (`world`, `pair`, `people`, `client`, `owner_session`, `owner_secret`, `connection_values`). Adapter tests sit under `tests/adapters/dataplatforms/` with an `__init__.py` and a `conftest.py` for the fakes.

**`tests/connections/test_scope_isolation.py` (C):**
- Names: the same name in P1, P2 and client-wide is allowed. A duplicate in one scope gets 409, on create and on rename.
- P2 connection with a P1 secret: a P2 manager without read on it gets 404; a tenant administrator gets 422 `secret_scope_mismatch`; the same on PATCH.
- A tenant secret on a project connection and a project secret on a client connection each get 422.
- As `shifde_app` in A's context, a raw INSERT and an UPDATE of `secret_ref_id` with the wrong scope raise 23514. An UPDATE of `workspace_id`/`project_id` is refused by privilege.
- `include_shared` returns exactly the visible project, workspace-wide and client-wide rows, and pagination across the union works.
- A provider over a project connection gets 422 from the service and 23514 on a raw insert as `shifde_app`.

**`tests/connections/test_routes_cross_project.py` (C):** a P1-only member gets 404 on every connections route for a P2 connection: get, patch, status, credential, test, test-jobs, tests, discover, discoveries/latest, invoke. The list excludes P2. A route-coverage assertion like `test_every_connections_route_has_a_cross_client_test` fails on a route without a case.

**`tests/connections/test_routes_cross_client.py`:** `ROUTE_TESTS` gains `PUT /api/v1/orgs/{org}/connections/{connection_id}/credential`. B's credential, rotate and archive calls on A's connection get 404, and A's secret row is byte-identical afterwards.

**`tests/connections/test_credential.py`:**
- Inline create.
- Set, then rotate (version 2); the old token-cache entry is unused.
- **Permissions (B-2748):** a user with `connection:manage` and `secret:manage` at the project gets 200; with `connection:manage` only, 403 on inline create, set and rotate. The check order of 6 is followed, one case per step.
- The `connection-` prefix gets 422 on `POST /secrets`. Admin rotate and PATCH on an owned secret get 409 (B-2750).
- Archive: a stale `row_version` gets 409 with nothing changed; otherwise the owned secret is disabled, but one still bound by another live connection is kept.
- `read_only_confirmed` is required for the inline credential.
- Audit rows carry no value. The redaction registry masks it in a failing test's problem details. `secret.created`, `secret.rotated` and `secret.disabled` rows exist with the admin field shapes.

**`tests/adapters/dataplatforms/`:**
- `test_databricks.py`, `test_snowflake.py`: the suites pass against the fakes; every fault maps to its `ErrorClass`; M2M and JWT are right (the fake verifies the signature); truncation and row cap; read-only refusals (`INSERT`, `DROP`, `SELECT 1; DROP ...`, comment tricks).
- `test_egress.py`: refused hosts (wrong suffix, IP literal, http, port 8443, userinfo), redirect refused, private resolution refused under a client policy, response cap, deadline and cancel.
- `test_token_cache.py`: two clients with the same host and client_id get distinct tokens; rotation gives a new token.
- `test_dev_fake_switch.py` in `tests/core` or here: the switch raises `ValueError` under `production`.
- The live files.

**Existing fixtures (B-2754).** CM-1 fixes every existing fixture that binds a secret of another scope, or builds a provider over a non-client-wide connection, without changing any assertion. Typically the fix creates the secret in the connection's scope. The files named by the builder read:
- `tests/connections/test_regressions.py`
- `tests/connections/conftest.py` (`owner_secret`, `connection_values`)
- `tests/build/service_support.py`
- `tests/delivery/test_approvals.py`
- `tests/app/test_build_review2.py`
- `tests/build/test_tool_runtime.py`
- `tests/models_admin/conftest.py` and `tests/models_admin/routes.py`

`grep -rn "secret_ref_id\|secret_id\|create_secret" backend/tests` finds any other. CM-1's check command covers all of them.

**`tests/schema/` (lead):** `test_schema_vs_seth.py` passes with the five yaml entries. `test_rls_schema.py` and `test_function_schema.py` are unchanged and pass. The startup role check tests are unchanged.

**Frontend:**
- Vitest: `ConnectionsSpaceView.test.tsx` (grouping, shared tag, hidden tabs, states), `ConnectionWizard.test.tsx` (auth-specific fields, credential field, scope-filtered picker, confirmation), `ConnectionDetailView.test.tsx` (rotate, archive, discover tree), `lib/nav.test.ts` (new item).
- Playwright: `frontend/e2e/connections.spec.ts` with `SHIFDE_DEV_FAKE_DATA_PLATFORMS=true`, local only (B-2756).

## 11. Calls made

1. **Equal-scope credentials.** Sharing happens through a shared connection, never through a shared secret.
2. **Connection and target stay separate** (section 8).
3. **No aggregate API** (section 6).
4. **Archive = `retired`.** It disables only the connection's own credential.
5. **REST over httpx, no vendor SDKs.** One egress path for all of it.
6. **Fake-backed suites record no evidence.**
7. **A test never starts a Databricks warehouse.**
8. **No permission relaxation.** Credential handling through a connection needs `secret:manage` (B-2748).

## 12. Factory issues (in order)

Every backend issue's checks include `cd backend; uv run ruff format --check .; uv run ruff check .; uv run mypy src; uv run lint-imports; uv run bandit -q -r src --severity-level medium` and `bash factory/checks.sh`. Issues that change routes or schemas also refresh the snapshot and run `uv run python -m tests._support.openapi_snapshot --check`. **C** = isolation-critical (module-builder-critical, lead isolation review). Lead prerequisites L1 and L2 (section 5) are merged first by hand.

1. **CM-1 connections + models_admin: scope isolation (C).**
   - Covers: rules 4.1-4.6 in the service, the SQLSTATE mapping, `include_shared`, `last_test_outcome`, the provider connection-scope check, the fixture fixes of section 10, and the openapi refresh.
   - Checks: `cd backend; uv run pytest tests/connections tests/models_admin tests/secrets tests/schema tests/build tests/delivery/test_approvals.py tests/app/test_build_review2.py`.
   - Depends: L1, L2.
2. **CM-2 connections + admin + secrets: credential lifecycle (C).**
   - Covers: inline `credential` on create, `PUT .../credential` with the 6 check order, archive disabling the owned secret, the reserved prefix (422 on create, 409 on admin rotate/PATCH), the audit events of section 6, and cross-client and cross-project coverage of the new route.
   - Checks: `cd backend; uv run pytest tests/connections tests/secrets tests/admin tests/audit`.
   - Depends: CM-1.
3. **CM-3 adapters: data-platform toolkit and Databricks connection adapter (C: credentials and token cache).**
   - Covers: `dataplatforms/{sqlapi,databricks,fakes,suites}.py`, `AdapterRules` and the two hooks wired into `ConnectionService` and `connections/api.py`, the registry and entry-point swap, and `SHIFDE_DEV_FAKE_DATA_PLATFORMS` with its `ValueError` outside development/test.
   - Checks: `cd backend; uv run pytest tests/adapters tests/connections tests/core`.
   - Depends: CM-2.
4. **CM-4 adapters: Snowflake connection adapter (C: credentials and token cache).**
   - Covers: key-pair JWT, PAT, the fake with signature verification, the suite.
   - Checks: `cd backend; uv run pytest tests/adapters/dataplatforms tests/connections`.
   - Depends: CM-3.
5. **CM-5 live tests and runbooks (opt-in, owner-only).**
   - Covers: the two live files, runbooks, the `live` marker text in `pyproject.toml`, and the CLAUDE.md "Local commands" lines.
   - Checks: `cd backend; uv run pytest tests/adapters/dataplatforms -q` (live cases skipped) and `uv run pytest tests/adapters/dataplatforms -m live` (skipped without the env files).
   - Depends: CM-3, CM-4.
6. **CM-6 front end: the Connections space.**
   - Covers: `ConnectionsSpaceView`, scope selector, tabs, shared tags, nav item, redirects of the three `integrate/connections` routes, the `DashboardView` link.
   - Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`.
   - Depends: CM-2.
7. **CM-7 front end: the uniform flow.**
   - Covers: wizard, test, discover tree, edit, rotate, archive, plus `frontend/e2e/connections.spec.ts`.
   - Checks: `cd frontend; pnpm lint; pnpm typecheck; pnpm test; pnpm build`. The e2e spec is added and listed as a local-only check (`cd frontend; pnpm e2e`, cluster running); it is not part of the acceptance gate (B-2756).
   - Depends: CM-3, CM-4, CM-6.
8. **CM-8 scripts: `scripts/connections_demo.py --fake`.**
   - Runs in-process on a scratch database:
     1. Two clients. In A, projects P1 and P2.
     2. A Databricks connection in P1 and a Snowflake connection in P2, each with an inline credential.
     3. Each connection is tested, discovered, edited, rotated and archived against the fakes.
     4. Isolation: a P1-only member gets 404 on P2's connection. A P2 bind of P1's secret is refused by the service (422) and by the database as `shifde_app` (23514). B gets 404.
     5. As A's administrator it lists, through the routes, `GET /api/v1/orgs/{org}/connections?workspace=&project=&include_shared=true`, `GET /api/v1/orgs/{org}/model-providers` and `GET /api/v1/orgs/{org}/delivery/targets`, and finds a client-wide model provider and a delivery target in P1's workspace (B-2757).
   - Ends `CONNECTIONS DEMO PASSED`; STATUS gets the done-when result.
   - Checks: `python scripts/connections_demo.py --fake; python -m unittest discover -s scripts/tests`.
   - Depends: CM-1-CM-7.

## 13. Open questions (remaining proposals)

1. Can a project connection use a client-wide secret? No; equal scope (call 1).
2. *Superseded by B-2748:* credential handling needs `secret:manage`.
3. A rotated secret that other same-scope connections bind: rotate in place, record `shared_with` in the audit, and warn in the dialog.
4. Snowflake OAuth: deferred.
5. Passphrase-protected Snowflake keys: refused; unencrypted PKCS#8 only.
6. Extra Databricks host suffixes: `SHIFDE_DELIVERY_DATABRICKS_HOST_SUFFIXES` through `AdapterRules`.
7. Fake suites record no evidence (call 6).
8. The e2e needs `shifde-worker` for queued tests: the local e2e harness adds it as a third Playwright `webServer` (health port from SP4-6 if merged, else a log line). This is local only and outside the gate (B-2756).
9. A connection takes no one-platform claim.
10. Planned rows: no data migration (7.5).
11. Discovery payload adds `catalog`; consumers that read `schema`/`name` are unaffected.
12. Names are case-sensitive.

## 14. Settled after the builder read (lead, 2026-11-22)

- **B-2743 (R1)** L1 and L2 prechecks loop over every `shifde.tenants` row (listed under `app.platform_admin = 'on'`). They set `app.tenant_id` per client with transaction-local `set_config` and refuse the upgrade naming only the count. No reusable helper exists in `backend/migrations/versions` (section 5).
- **B-2744 (R2)** Migrations use the module-local `_run` (plus `_previous` from `runtime_0003` in L1). L1's downgrade restores the exact `connections_0001` guard body and trigger. The five yaml entries are listed exactly. Function bodies are not compared by `tests/schema`. The `test_rls_schema.py` (around line 1394) prose stays true and is unchanged (section 5).
- **B-2745 (R3)** SQLSTATE `23505` -> 409 name collision (create and PATCH rename); `23514` -> 422 with the guard's message; any other `IntegrityError` re-raised (4.5).
- **B-2746 (R4)** The secret-scope check runs on create and on any PATCH changing `secret_id`, `workspace_id` or `project_id`, after the existing 404/`secret:use` check (4.5).
- **B-2747 (R5)** `read_only_confirmed` is required with an inline `credential` exactly as with `secret_id` in `read` mode (4.8, 6).
- **B-2748 (R6)** No permission relaxation. Inline create, set and rotate need `connection:manage` and `secret:manage` on the connection's scope ref. No role change. Both the allowed and the 403 case are tested (4.7, 6, 10).
- **B-2749 (R7)** `PUT .../credential` `row_version` is the connection's, and the secret's version is read in the same transaction. Check order: 404, 403 `connection:manage`, 403 `secret:manage`, 409 retired, 422 `auth_method` none, 422 invalid value (6).
- **B-2750 (R8)** Owned secrets (`connection-` prefix) get 409 on the admin rotate/PATCH/disable routes and are managed only through the connection. Tested (4.7, 6, 10).
- **B-2751 (R9)** "Live" means `status <> 'retired'`. Archive checks `row_version` first, then disables the owned secret (its current version) in the same transaction, only if no other live connection binds it (6).
- **B-2752 (R10)** `connection.credential_set`/`connection.credential_rotated` go through `ConnectionService._audit`. `secret.created`/`secret.rotated`/`secret.disabled` are emitted by `shifde.connections.service` via `shifde.audit.service.record` with the admin field shapes. `connections` never imports `admin` (6).
- **B-2753 (R11)** Settled from the code:
  - inline secret kind per auth method from the existing kinds;
  - limit in UTF-8 bytes via `secrets.service._check_value` (16384);
  - `include_shared` as one OR query under RLS with the existing cursor;
  - `last_test_outcome` via a lateral join;
  - `secret_fields` `("token", "client_secret")` for Databricks and `("private_key", "token")` for Snowflake;
  - test layout and fixtures (section 10);
  - live skip as `tests/adapters/native/test_databricks_live.py`;
  - `AdapterRules` built in `connections/api.py` `_connections` from `runtime.settings`;
  - `normalize_config` on create and on PATCH with `config`, `validate_credential` on inline create and `PUT .../credential` (7.1).
- **B-2754 (R12)** The L2 trigger also fires on `UPDATE OF connection_id`; no provider route changes it today. CM-1 fixes the listed fixtures without changing assertions, and its check command covers them (section 10, 12).
- **B-2755 (R13)** `SHIFDE_DEV_FAKE_DATA_PLATFORMS` is allowed only when `SHIFDE_ENV` is `development` or `test` (valid values `development`, `test`, `production`; default `production`). Otherwise settings validation raises `ValueError` like the other guards (7.5).
- **B-2756 (R14)** `pnpm e2e` stays local only. CM-7 is accepted on lint, typecheck, test and build; the e2e spec is added and listed as a local-only check (12).
- **B-2757 (R15)** CM-8 lists `GET /api/v1/orgs/{org}/connections`, `GET /api/v1/orgs/{org}/model-providers` and `GET /api/v1/orgs/{org}/delivery/targets` (12).
- **B-2758 (R16)** Frontend names were checked in `frontend/src`. Fixes made in this revision:
  - the project page is `DashboardView`;
  - hrefs live in `lib/scope.ts`;
  - nav is `NAV_ITEMS` in `lib/nav.ts`;
  - the hooks are `useConnections`/`useProviders`/`useAdapters`, `useTargets` and `useSecrets`;
  - `ConnectionsSpaceView` and `connectionsHref` are new;
  - the existing link builders for `/integrate/connections/...` are updated (sections 3, 9).
- **B-2759 (open question 8)** CM-7's e2e harness starts `shifde-worker` as a third Playwright server with the plain `uv run shifde-worker` command against the same database, independent of whether SP4-6's health port has merged; it waits on the API's readiness, not on a worker port.
