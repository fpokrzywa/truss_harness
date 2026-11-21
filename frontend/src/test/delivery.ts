import type {
  CredentialOut,
  DeliveryExecutorOut,
  DeploymentDetail,
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
