import type { AppRequestOut, AppRequestSummary, Draft, DraftOut } from '@/lib/api/appgen-types';
import type { Connection, Model } from '@/lib/api/integrate-types';
import { WORKSPACES } from './releases';
import { ALL_PERMS, list, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export const APPGEN_PERMS = ['appgen:request:read', 'appgen:request:write', 'appgen:draft:accept'];

export const ORG = '/api/v1/orgs/acme';
/** The eng/api project's app requests, and one request. */
export const AR = `${ORG}/workspaces/eng/projects/api/app-requests`;
export const ONE = `${ORG}/app-requests/ar-1`;

export const connection = (over: Partial<Connection> = {}): Connection => ({
  id: 'conn-dbx',
  name: 'Sales warehouse',
  connection_type: 'data_platform',
  adapter_key: 'databricks',
  config: {},
  auth_method: 'bearer',
  access_mode: 'read',
  status: 'active',
  row_version: 1,
  ...over,
});

export const MODEL: Model = {
  id: 'm-1',
  provider_id: 'prov-1',
  model_key: 'gpt-x',
  display_name: 'GPT X',
  capabilities: [],
  context_window: 8000,
  max_output_tokens: 1000,
  lifecycle: 'active',
  row_version: 1,
};

export const DISCOVERY = {
  id: 'disc-1',
  kind: 'schema',
  payload: {
    tables: [
      { catalog: 'Main', schema: 'Sales', name: 'Orders', columns: ['id'] },
      { catalog: 'main', schema: 'sales', name: 'customers', columns: ['id'] },
    ],
  },
};

export const DRAFT: Draft = {
  agent: { name: 'sales-agent', instructions: 'Answer sales questions.' },
  tools: [
    {
      name: 'orders_by_region',
      description: 'Orders of a region',
      sql: 'SELECT * FROM main.sales.orders WHERE region = :region',
      parameters: [{ name: 'region', type: 'string', required: true }],
      max_rows: 200,
    },
  ],
  screens: [
    { kind: 'chat', title: 'Ask', placeholder: 'Ask about sales' },
    { kind: 'table', title: 'Orders', tool: 'orders_by_region', inputs: ['region'] },
  ],
  evaluation_cases: [
    {
      case_id: 'c1',
      input: { message: 'Orders in the west?' },
      assertions: [{ id: 'a1', kind: 'contains', value: 'west' }],
    },
  ],
};

export const revision = (over: Partial<DraftOut> = {}): DraftOut => ({
  revision_no: 1,
  source: 'model',
  draft: DRAFT,
  draft_hash: 'a'.repeat(64),
  validation: [],
  created_by: 'u-me',
  created_at: '2026-11-20T10:01:00Z',
  ...over,
});

export const SUMMARY: AppRequestSummary = {
  id: 'ar-1',
  workspace_id: 'ws-1',
  project_id: 'p-1',
  connection_id: 'conn-dbx',
  status: 'drafted',
  status_reason: null,
  latest_revision_no: 1,
  solution_version_id: null,
  requested_by: 'u-me',
  created_at: '2026-11-20T10:00:00Z',
  updated_at: null,
};

export const request = (over: Partial<AppRequestOut> = {}): AppRequestOut => ({
  ...SUMMARY,
  tables: ['main.sales.orders'],
  brief: 'An app for regional sales managers.',
  model_id: 'm-1',
  discovery_id: 'disc-1',
  accepted_revision_no: null,
  accept_progress: {},
  revisions: [revision()],
  ...over,
});

/** stubApi plus the appgen permissions, the eng/api project, one connection, model and request. */
export function stubAppgen(
  routes: Record<string, Handler> = {},
  perms: string[] = [...ALL_PERMS, ...APPGEN_PERMS],
) {
  return stubApi(
    {
      ...WORKSPACES,
      [`GET ${ORG}/connections`]: () => list([connection()]),
      [`GET ${ORG}/connections/conn-dbx/discoveries/latest`]: () => jsonResponse(DISCOVERY),
      [`GET ${ORG}/models/effective`]: () => list([MODEL]),
      [`GET ${AR}`]: () => list([SUMMARY]),
      [`GET ${ONE}`]: () => jsonResponse(request()),
      ...routes,
    },
    perms,
  );
}
