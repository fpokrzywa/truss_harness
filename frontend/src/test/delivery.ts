import type {
  CredentialOut,
  DeliveryExecutorOut,
  DeploymentDetail,
  DeploymentJournalStep,
  DeploymentOut,
  PlanOut,
  TargetOut,
} from '@/lib/api/delivery-types';
import { RELEASE, S, SOLUTION, VERSION } from './releases';
import { ALL_PERMS, list, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export const DELIVERY_PERMS = [
  'delivery:target:read',
  'delivery:target:write',
  'delivery:executor:read',
  'delivery:executor:manage',
  'delivery:plan:read',
  'delivery:plan:request',
  'delivery:plan:approve',
  'delivery:deployment:read',
  'delivery:deployment:resolve',
];

export const DELIVERY_READ_PERMS = ['delivery:target:read', 'delivery:executor:read'];

export const D = '/api/v1/orgs/acme/delivery';

export const TARGET: TargetOut = {
  id: 't-1',
  name: 'Analytics prod',
  workspace: 'eng',
  project: 'api',
  platform_kind: 'databricks',
  address: 'https://adb-123.azuredatabricks.net',
  environment: 'prod',
  bindings: { warehouse_id: 'wh-0042' },
  credential_ref: 'dbx.prod',
  status: 'active',
  revision: 1,
  created_by: 'u-me',
  created_at: '2026-11-01T10:00:00Z',
  updated_at: null,
};

export const SNOWFLAKE_TARGET: TargetOut = {
  ...TARGET,
  id: 't-sf',
  name: 'Finance prod',
  platform_kind: 'snowflake',
  address: 'https://acme-fin.snowflakecomputing.com',
  bindings: {
    package: 'SALES_PKG',
    application: 'SALES_APP',
    warehouse: 'APP_WH',
    cortex_model: 'llama3.1-70b',
  },
  credential_ref: 'sf.prod',
};

export const FABRIC_TARGET: TargetOut = {
  ...TARGET,
  id: 't-fab',
  name: 'Fabric prod',
  platform_kind: 'fabric',
  address: 'https://app.fabric.microsoft.com/groups/11111111-2222-4333-8444-555555555555',
  bindings: {
    entra_tenant_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    lakehouse_name: 'sales_lake',
    notebook_name: 'sales_notebook',
  },
  credential_ref: 'fabric.prod',
};

/** Made-up RIDs and ids of a Foundry stack. */
export const PALANTIR_BINDINGS = {
  marketplace_rid: 'ri.marketplace.main.local.00000000-0000-4000-8000-000000000001',
  product_id: '00000000-0000-4000-8000-000000000002',
  version_id: '00000000-0000-4000-8000-000000000003',
  namespace_rid: 'ri.compass.main.namespace.00000000-0000-4000-8000-000000000004',
  parent_folder_rid: 'ri.compass.main.folder.00000000-0000-4000-8000-000000000005',
  ontology_rid: 'ri.ontology.main.ontology.00000000-0000-4000-8000-000000000006',
  object_type: 'sales.Order',
  frontend_origin: 'https://sales.example.com',
  frontend_asset_sha256: 'e'.repeat(64),
  allow_preview_apis: true,
};

export const PALANTIR_TARGET: TargetOut = {
  ...TARGET,
  id: 't-pal',
  name: 'Foundry prod',
  platform_kind: 'palantir',
  address: 'https://acme.palantirfoundry.com',
  bindings: PALANTIR_BINDINGS,
  credential_ref: 'foundry.prod',
};

export const DOCKER_TARGET: TargetOut = {
  ...TARGET,
  id: 't-dock',
  name: 'Docker prod',
  platform_kind: 'docker',
  address: 'docker://ABCD:EFGH:IJKL',
  bindings: {
    project: 'sales-app',
    port: 8443,
    workspace_host: 'https://adb-123.azuredatabricks.net',
    warehouse_id: 'wh-0042',
    serving_endpoint: 'llama-serving',
  },
  credential_ref: 'docker.prod',
};

/** One target of each kind, in the register form's order. */
export const TARGETS_OF_EACH_KIND = [
  TARGET,
  SNOWFLAKE_TARGET,
  FABRIC_TARGET,
  PALANTIR_TARGET,
  DOCKER_TARGET,
];

/** The target with stored bindings that no longer validate, as the API reads it. */
export const unusable = (t: TargetOut, over: Partial<TargetOut> = {}): TargetOut =>
  ({ ...t, ...over, bindings: { unusable: true } }) as TargetOut;

export const EXECUTOR: DeliveryExecutorOut = {
  id: 'e-1',
  name: 'edge-runner',
  status: 'active',
  last_seen_at: '2026-11-18T09:30:00Z',
  version: '2.4.1',
  created_at: '2026-11-01T10:00:00Z',
  updated_at: null,
};

export const CREDENTIAL: CredentialOut = {
  id: 'c-1',
  executor_id: 'e-1',
  prefix: 'shx_ab12',
  status: 'live',
  created_by: 'u-me',
  created_at: '2026-11-02T10:00:00Z',
  expires_at: '2027-02-02T10:00:00Z',
  revoked_at: null,
};

export const EXECUTOR_SECRET = 'shx_ab12_TOPSECRETEXECUTORTOKEN';

export const PLAN_DIGEST = 'a'.repeat(64);

/** A sealed plan requested by someone other than the session user (`u-me`). */
export const PLAN: PlanOut = {
  id: 'p-1',
  target_id: 't-1',
  application_key: 'sales-app',
  display_name: 'Sales app',
  status: 'sealed',
  status_reason: null,
  requested_by: 'u-other',
  artifact_id: 'art-1',
  artifact_digest: 'b'.repeat(64),
  target_revision: 1,
  steps: [
    { key: 'app.create', action: 'create', resource_key: 'apps/sales-app', reversible: true },
    { key: 'app.start', action: 'start', resource_key: 'apps/sales-app', reversible: false },
  ],
  snapshot: null,
  adapter_version: 'databricks-native/0.1.0',
  toolchain_digest: 'c'.repeat(64),
  digest: PLAN_DIGEST,
  sealed_at: '2026-11-18T10:00:00Z',
  expires_at: '2099-01-01T00:00:00Z',
  created_at: '2026-11-18T09:59:00Z',
  updated_at: null,
  approval: null,
  deployment_id: null,
  release_id: '01900000-0000-7000-8000-0000000000a1',
  release_digest: 'd'.repeat(64),
  evaluation_run_id: '01900000-0000-7000-8000-0000000000e1',
};

export const DEPLOYMENT: DeploymentOut = {
  id: 'dep-1',
  plan_id: 'p-1',
  target_id: 't-1',
  application_key: 'sales-app',
  status: 'verified',
  status_reason: null,
  receipt: { operation_handles: { app: 'sales-app' }, deployed_at: '2026-11-18T10:05:00Z' },
  verification: {
    checks: [{ name: 'app_running', status: 'passed', required: true, evidence: 'state=RUNNING' }],
  },
  reconcile_evidence: null,
  observe_deadline: '2026-11-18T10:15:00Z',
  resolved_by: null,
  resolved_note: null,
  resolved_at: null,
  created_at: '2026-11-18T10:01:00Z',
  updated_at: '2026-11-18T10:06:00Z',
};

export const DEPLOYMENT_DETAIL: DeploymentDetail = {
  ...DEPLOYMENT,
  journal: [
    {
      step_key: 'app.create',
      state: 'intent',
      input_digest: 'd'.repeat(64),
      receipt: null,
      recorded_at: '2026-11-18T10:02:00Z',
    },
    {
      step_key: 'app.create',
      state: 'complete',
      input_digest: 'd'.repeat(64),
      receipt: { app: 'sales-app' },
      recorded_at: '2026-11-18T10:03:00Z',
    },
  ],
};

/** The steps of a Snowflake deployment (SP6 6.2), in order. */
export const SNOWFLAKE_STEPS = [
  'package-create',
  'schema-create',
  'stage-create',
  'stage-upload',
  'setup-validate',
  'version-create',
  'application-apply',
  'application-access',
];

/** A verified deployment on `SNOWFLAKE_TARGET`: every step with its intent and complete entry. */
export const SNOWFLAKE_DEPLOYMENT_DETAIL: DeploymentDetail = {
  ...DEPLOYMENT,
  target_id: 't-sf',
  journal: SNOWFLAKE_STEPS.flatMap((step_key, i) =>
    (['intent', 'complete'] as const).map((state, j): DeploymentJournalStep => ({
      step_key,
      state,
      input_digest: 'd'.repeat(64),
      receipt: null,
      recorded_at: `2026-11-18T10:02:${String(i * 2 + j).padStart(2, '0')}Z`,
    })),
  ),
};

/** A verified deployment on `target_id` whose journal holds each step's intent and complete entry. */
export function deploymentWithSteps(target_id: string, steps: string[]): DeploymentDetail {
  return {
    ...DEPLOYMENT,
    target_id,
    journal: steps.flatMap((step_key, i) =>
      (['intent', 'complete'] as const).map((state, j): DeploymentJournalStep => ({
        step_key,
        state,
        input_digest: 'd'.repeat(64),
        receipt: null,
        recorded_at: `2026-11-18T10:02:${String(i * 2 + j).padStart(2, '0')}Z`,
      })),
    ),
  };
}

/** A withdrawn release of the same version as `RELEASE`, never offered for a plan. */
export const WITHDRAWN_RELEASE = {
  ...RELEASE,
  id: 'r-2',
  digest: '0123456789ab'.padEnd(64, '0'),
  status: 'withdrawn',
};

/** The request-plan release cascade: active and archived workspaces and projects, eng/api's
 * solution and version, and one sealed and one withdrawn release. */
export const RELEASE_CASCADE: Record<string, Handler> = {
  'GET /api/v1/orgs/acme/workspaces': () =>
    list([
      { id: 'ws-1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 1 },
      { id: 'ws-2', slug: 'old', name: 'Old workspace', status: 'archived', row_version: 1 },
    ]),
  'GET /api/v1/orgs/acme/workspaces/eng/projects': () =>
    list([
      { id: 'p-1', slug: 'api', name: 'API', status: 'active', row_version: 1 },
      { id: 'p-2', slug: 'legacy', name: 'Legacy project', status: 'archived', row_version: 1 },
    ]),
  [`GET ${S}`]: () => list([SOLUTION]),
  [`GET ${S}/s-1/versions`]: () => list([VERSION]),
  [`GET ${S}/s-1/versions/sv-1/releases`]: () => list([RELEASE, WITHDRAWN_RELEASE]),
};

export { list } from './security';

/** stubApi plus the delivery permissions and one target, executor, credential, plan and deployment. */
export function stubDelivery(
  routes: Record<string, Handler> = {},
  perms: string[] = [...ALL_PERMS, ...DELIVERY_PERMS],
) {
  return stubApi(
    {
      [`GET ${D}/targets`]: () => list([TARGET]),
      [`GET ${D}/executors`]: () => list([EXECUTOR]),
      [`GET ${D}/executors/e-1/credentials`]: () => jsonResponse([CREDENTIAL]),
      [`GET ${D}/plans`]: () => list([PLAN]),
      [`GET ${D}/plans/p-1`]: () => jsonResponse(PLAN),
      [`GET ${D}/deployments`]: () => list([DEPLOYMENT]),
      [`GET ${D}/deployments/dep-1`]: () => jsonResponse(DEPLOYMENT_DETAIL),
      ...routes,
    },
    perms,
  );
}
