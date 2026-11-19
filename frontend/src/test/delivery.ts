import type { CredentialOut, DeliveryExecutorOut, TargetOut } from '@/lib/api/delivery-types';
import { ALL_PERMS, list, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export const DELIVERY_PERMS = [
  'delivery:target:read',
  'delivery:target:write',
  'delivery:executor:read',
  'delivery:executor:manage',
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

export { list } from './security';

/** stubApi plus the delivery permissions and one target, executor and credential. */
export function stubDelivery(
  routes: Record<string, Handler> = {},
  perms: string[] = [...ALL_PERMS, ...DELIVERY_PERMS],
) {
  return stubApi(
    {
      [`GET ${D}/targets`]: () => list([TARGET]),
      [`GET ${D}/executors`]: () => list([EXECUTOR]),
      [`GET ${D}/executors/e-1/credentials`]: () => jsonResponse([CREDENTIAL]),
      ...routes,
    },
    perms,
  );
}
