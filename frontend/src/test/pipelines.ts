import type { NodeRun, NodeTypeDef } from '@/lib/api/pipeline-types';
import { BUILD_PERMS } from './build';
import { stubApi, list } from './security';
import { jsonResponse, type Handler } from './utils';
import { OPERATE_PERMS } from './operate';

export const PIPELINE_PERMS = [
  ...new Set([
    ...BUILD_PERMS,
    ...OPERATE_PERMS,
    'pipeline:read',
    'pipeline:create',
    'pipeline:update',
    'pipeline:publish',
    'pipeline:test',
    'pipeline:execute',
  ]),
];

const type = (
  t: string,
  over: Partial<NodeTypeDef> & Pick<NodeTypeDef, 'inputs' | 'outputs'>,
): NodeTypeDef => ({
  type: t,
  label: t,
  category: 'logic',
  side_effect: false,
  enabled: true,
  maturity: 'validated',
  ...over,
});

export const NODE_TYPES: NodeTypeDef[] = [
  type('db_query', {
    label: 'Database query',
    category: 'source',
    inputs: [],
    outputs: [{ name: 'rows' }],
    config_schema: {
      type: 'object',
      required: ['sql'],
      properties: {
        connection_id: { type: 'string' },
        sql: { type: 'string', title: 'SQL' },
        max_rows: { type: 'integer', title: 'Max rows', minimum: 1 },
      },
    },
  }),
  type('transform', {
    label: 'Transform',
    inputs: [{ name: 'rows' }],
    outputs: [{ name: 'rows' }],
    config_schema: { type: 'object', properties: { operations: { type: 'array' } } },
  }),
  type('condition', {
    label: 'Condition',
    inputs: [],
    dynamic_inputs: true,
    outputs: [{ name: 'result' }],
    config_schema: {
      type: 'object',
      properties: { expression: { type: 'string', title: 'Expression' } },
    },
  }),
  type('agent', {
    label: 'Agent',
    category: 'ai',
    inputs: [{ name: 'input' }],
    outputs: [{ name: 'output' }],
    config_schema: { type: 'object', properties: { agent_version_id: { type: 'string' } } },
  }),
  type('approval', {
    label: 'Approval',
    category: 'control',
    inputs: [],
    outputs: [],
    config_schema: {
      type: 'object',
      properties: { expires_seconds: { type: 'integer', title: 'Expires (seconds)', minimum: 60 } },
    },
  }),
  type('sink', {
    label: 'Sink',
    category: 'action',
    side_effect: true,
    inputs: [{ name: 'doc' }],
    outputs: [],
    config_schema: {
      type: 'object',
      properties: {
        target: { enum: ['run_artifact', 'connection'], title: 'Target' },
        mode: { enum: ['append', 'replace'], default: 'replace', title: 'Mode' },
        connection_id: { type: 'string' },
        method: { enum: ['POST', 'PUT'], default: 'POST', title: 'Method' },
        path_template: { type: 'string', title: 'Path template' },
      },
    },
  }),
  type('api_request', {
    label: 'API request',
    category: 'action',
    inputs: [],
    outputs: [{ name: 'body' }],
    enabled: false,
    maturity: 'planned',
    reason_disabled: 'Executor conformance test has not passed.',
  }),
];

export function nodeRun(over: Partial<NodeRun> = {}): NodeRun {
  return { node_id: 'src', type: 'db_query', status: 'succeeded', attempts: 1, ...over };
}

/** stubApi with pipeline permissions and the lookups every pipeline screen performs. */
export function stubPipelines(routes: Record<string, Handler> = {}, perms = PIPELINE_PERMS) {
  return stubApi(
    {
      'GET /api/v1/pipelines/node-types': () => jsonResponse({ items: NODE_TYPES }),
      'GET /api/v1/orgs/acme/connections': () => list([]),
      'GET /api/v1/orgs/acme/models/effective': () => list([]),
      'GET /api/v1/orgs/acme/agents': () => list([]),
      'GET /api/v1/orgs/acme/prompts': () => list([]),
      'GET /api/v1/orgs/acme/tools': () => list([]),
      ...routes,
    },
    perms,
  );
}
