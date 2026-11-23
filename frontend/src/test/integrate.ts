import type { Adapter } from '@/lib/api/integrate-types';
import { ALL_PERMS, list, stubApi } from './security';
import type { Handler } from './utils';

export const INT_PERMS = [
  'vendor:read',
  'vendor:manage',
  'model:read',
  'model:manage',
  'model:invoke',
  'connection:read',
  'connection:manage',
  'connection:test',
  'connection:use',
  'egress:manage',
  'secret:read',
];

export const ADAPTER_REST: Adapter = {
  key: 'rest.http',
  kind: 'rest',
  version: '1.0.0',
  capabilities: [
    {
      capability: 'invoke',
      maturity: 'validated',
      evidence: { run_id: 'run-1', finished_at: '2026-05-01T10:00:00Z', suite_version: '2.1' },
    },
  ],
  config_schema: {
    type: 'object',
    required: ['base_url'],
    properties: {
      base_url: { type: 'string', format: 'uri', title: 'Base URL' },
      timeout_seconds: { type: 'integer', minimum: 1, maximum: 60, title: 'Timeout seconds' },
      verify_tls: { type: 'boolean', default: true, title: 'Verify TLS' },
      allowed_methods: { type: 'array', items: { type: 'string' }, title: 'Allowed methods' },
      auth_style: { type: 'string', enum: ['header', 'query'], title: 'Auth style' },
      api_token: { type: 'string', title: 'API token' },
    },
  },
  secret_fields: ['api_token'],
  auth_methods: ['none', 'bearer'],
  auth_only_fields: {
    oauth2_url: ['oauth2_client_credentials'],
    client_id: ['oauth2_client_credentials'],
    scope: ['oauth2_client_credentials'],
  },
  destination_fields: ['base_url', 'oauth2_url'],
};

export const ADAPTER_PLANNED: Adapter = {
  key: 'databricks',
  kind: 'data_platform',
  version: '0.0.0',
  capabilities: [
    { capability: 'query', maturity: 'planned' },
    { capability: 'discover', maturity: 'planned' },
  ],
  config_schema: { type: 'object', properties: {} },
  secret_fields: [],
  auth_methods: ['token'],
  auth_only_fields: { client_id: ['oauth2_client_credentials'] },
  destination_fields: ['host'],
};

export const ADAPTER_FAKE: Adapter = {
  key: 'fake',
  kind: 'model_provider',
  version: '1.0.0',
  capabilities: [{ capability: 'chat', maturity: 'validated' }],
  config_schema: { type: 'object', properties: {} },
  secret_fields: [],
  auth_methods: ['none'],
  auth_only_fields: {},
  destination_fields: [],
};

export { list } from './security';

/** stubApi plus the Integrate permissions and the adapter registry. */
export function stubInt(
  routes: Record<string, Handler> = {},
  perms: string[] = [...ALL_PERMS, ...INT_PERMS],
  adapters: Adapter[] = [ADAPTER_REST, ADAPTER_PLANNED, ADAPTER_FAKE],
) {
  return stubApi({ 'GET /api/v1/adapters': () => list(adapters), ...routes }, perms);
}
