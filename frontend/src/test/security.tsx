import { vi } from 'vitest';
import type { PermissionDef } from '@/lib/api/types';
import { jsonResponse, routeFetch, type Handler } from './utils';

export const SESSION = {
  user: { id: 'u-me', email: 'me@acme.test', display_name: 'Me', is_platform_admin: false },
  csrf_token: 'csrf-1',
  memberships: [{ org: { id: 'org-1', slug: 'acme', name: 'Acme', status: 'active' }, roles: [] }],
  expires_at: '2030-01-01T00:00:00Z',
  features: [
    'tenancy',
    'security',
    'integrate',
    'build',
    'operate',
    'pipelines',
    'ontology',
    'governance',
  ],
};

export const ALL_PERMS = [
  'member:read',
  'member:manage',
  'group:read',
  'group:manage',
  'role:read',
  'role:manage',
  'serviceaccount:read',
  'serviceaccount:manage',
  'apikey:read',
  'apikey:manage',
  'idp:manage',
  'secret:read',
  'secret:manage',
  'audit:read',
];

export const CATALOG: PermissionDef[] = [
  { key: 'member:read', resource_type: 'member', action: 'read', is_privileged: false },
  { key: 'member:manage', resource_type: 'member', action: 'manage', is_privileged: true },
  { key: 'role:read', resource_type: 'role', action: 'read', is_privileged: false },
  { key: 'apikey:manage', resource_type: 'apikey', action: 'manage', is_privileged: true },
  { key: 'secret:read', resource_type: 'secret', action: 'read', is_privileged: false },
];

export const list = <T,>(items: T[], next_cursor: string | null = null) =>
  jsonResponse({ items, next_cursor });

/** Stubs global fetch with sensible defaults for the acme org; `routes` override or extend them. */
export function stubApi(routes: Record<string, Handler> = {}, perms: string[] = ALL_PERMS) {
  const fetchMock = routeFetch({
    'GET /api/auth/session': () => jsonResponse(SESSION),
    'GET /api/v1/orgs/acme/me/permissions': () => jsonResponse({ permissions: perms }),
    'GET /api/v1/orgs/acme/workspaces': () => list([]),
    'GET /api/v1/permissions': () => list(CATALOG),
    ...routes,
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

export function bodyOf(
  fetchMock: ReturnType<typeof routeFetch>,
  key: string,
): Record<string, unknown> {
  const call = fetchMock.calls.find((c) => c.key === key);
  if (!call) throw new Error(`No call recorded for ${key}`);
  return JSON.parse(call.init.body as string) as Record<string, unknown>;
}
