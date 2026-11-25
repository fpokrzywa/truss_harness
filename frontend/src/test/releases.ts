import type { AppPreviewOut } from '@/lib/api/appgen-types';
import type { DatasetOut, EvaluationRunOut } from '@/lib/api/evaluation-types';
import {
  COMPILER_VERSION_APP,
  SNOWFLAKE_TARGET_KIND,
  type ReleaseOut,
  type SolutionOut,
  type SolutionVersionOut,
} from '@/lib/api/packaging-types';
import { ALL_PERMS, list, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export const RELEASE_PERMS = [
  'packaging:solution:read',
  'packaging:solution:write',
  'packaging:release:seal',
  'packaging:release:withdraw',
  'evaluation:dataset:read',
  'evaluation:dataset:write',
  'evaluation:run:read',
  'evaluation:run:request',
];

export const RELEASE_READ_PERMS = [
  'packaging:solution:read',
  'evaluation:dataset:read',
  'evaluation:run:read',
];

export const ORG = '/api/v1/orgs/acme';
/** The eng/api project's solutions and datasets. */
export const S = `${ORG}/workspaces/eng/projects/api/solutions`;
export const DS = `${ORG}/workspaces/eng/projects/api/evaluation-datasets`;

export const SOLUTION: SolutionOut = {
  id: 's-1',
  workspace: 'eng',
  project: 'api',
  name: 'Sales assistant',
  target_kind: 'databricks.app/v1',
  status: 'active',
  created_by: 'u-me',
  created_at: '2026-11-10T10:00:00Z',
  updated_at: null,
};

export const DATASET: DatasetOut = {
  id: 'ds-1',
  workspace: 'eng',
  project: 'api',
  name: 'Sales FAQ',
  version_no: 2,
  cases: [
    { case_id: 'c1', input: { message: 'hi' }, assertions: [] },
    { case_id: 'c2', input: { message: 'bye' }, assertions: [] },
    { case_id: 'c3', input: { message: 'price?' }, assertions: [] },
  ],
  digest: 'e'.repeat(64),
  created_by: 'u-me',
  created_at: '2026-11-11T10:00:00Z',
};

export const VERSION: SolutionVersionOut = {
  id: 'sv-1',
  solution_id: 's-1',
  version_no: 1,
  entrypoint: { kind: 'agent', version_id: 'av-1', spec_hash: 'f'.repeat(64) },
  spec: {
    data_reads: [],
    evaluation: {
      dataset_id: 'ds-1',
      min_accuracy: '0.9',
      max_p95_latency_ms: 2000,
      max_total_cost_usd: '1.5',
    },
  },
  spec_hash: '1'.repeat(64),
  pins: [
    { role: 'agent', version_id: 'av-1', spec_hash: 'f'.repeat(64) },
    { role: 'tool', version_id: 'tv-1', spec_hash: '2'.repeat(64) },
  ],
  created_by: 'u-me',
  created_at: '2026-11-12T10:00:00Z',
};

export const RELEASE_DIGEST = 'abcdef012345'.padEnd(64, '9');

export const RELEASE: ReleaseOut = {
  id: 'r-1',
  solution_id: 's-1',
  solution_version_id: 'sv-1',
  version_no: 1,
  workspace: 'eng',
  project: 'api',
  digest: RELEASE_DIGEST,
  status: 'sealed',
  manifest: {
    schema_version: 1,
    target_kind: 'databricks.app/v1',
    entrypoint: { kind: 'agent', version_id: 'av-1', spec_hash: 'f'.repeat(64) },
    pins: [
      { role: 'agent', version_id: 'av-1', spec_hash: 'f'.repeat(64) },
      { role: 'tool', version_id: 'tv-1', spec_hash: '2'.repeat(64) },
    ],
    data_reads: [{ connection_id: 'conn-db', tables: ['sales.orders', 'sales.customers'] }],
    bindings: {
      model_bindings: { 'm-1': { provider_id: 'prov-1', model: 'gpt-prod' } },
      connection_bindings: { 'conn-crm': 'conn-crm-prod' },
    },
    evaluation: {
      dataset_id: 'ds-1',
      dataset_digest: 'e'.repeat(64),
      thresholds: { min_accuracy: '0.9', max_p95_latency_ms: 2000, max_total_cost_usd: '1.5' },
    },
    compiler_version: 'shifde-compiler/1',
  },
  compatibility: [
    { path: 'entrypoint', status: 'supported', reason: null, mandatory: true },
    { path: 'models.m-1', status: 'supported', reason: null, mandatory: true },
  ],
  sealed_by: 'u-me',
  sealed_at: '2026-11-13T10:00:00Z',
  updated_at: null,
};

/** A generated-app (`/2`) release and its app preview. */
export const APP_RELEASE: ReleaseOut = {
  ...RELEASE,
  id: 'r-2',
  manifest: { ...RELEASE.manifest, compiler_version: COMPILER_VERSION_APP },
};

/** A generated-app release for a Snowflake target. */
export const SNOWFLAKE_APP_RELEASE: ReleaseOut = {
  ...APP_RELEASE,
  manifest: { ...APP_RELEASE.manifest, target_kind: SNOWFLAKE_TARGET_KIND },
};

export const APP_PREVIEW: AppPreviewOut = {
  release_id: 'r-2',
  release_digest: RELEASE_DIGEST,
  generator_version: 'shifde.appgen/1.0',
  files: [
    { path: 'app/app.py', size: 1200, sha256: 'a'.repeat(64) },
    { path: 'app/config/screens.json', size: 210, sha256: 'b'.repeat(64) },
    { path: 'release.json', size: 90, sha256: 'c'.repeat(64) },
  ],
  screens: [
    { kind: 'chat', title: 'Ask about sales', placeholder: 'Ask a question' },
    { kind: 'table', title: 'Orders by region', tool: 'orders_by_region', inputs: ['region'] },
  ],
  tools: [
    {
      name: 'orders_by_region',
      description: 'Orders in a region',
      sql: 'SELECT * FROM sales.orders WHERE region = :region',
      parameters: [{ name: 'region', type: 'string', required: true }],
      max_rows: 200,
    },
  ],
};

export const EVALUATION: EvaluationRunOut = {
  id: 'ev-1',
  release_id: 'r-1',
  release_digest: RELEASE_DIGEST,
  dataset_id: 'ds-1',
  dataset_digest: 'e'.repeat(64),
  thresholds: { min_accuracy: '0.9', max_p95_latency_ms: 2000, max_total_cost_usd: '1.5' },
  status: 'failed',
  status_reason: 'accuracy below the minimum',
  accuracy: '0.5',
  p95_latency_ms: 1200,
  total_cost_usd: '0.42',
  results: [
    {
      case_id: 'c1',
      run_id: 'run-1',
      passed: true,
      assertions: [{ id: 'a1', passed: true, reason: null }],
      latency_ms: 900,
      cost_usd: '0.2',
    },
    {
      case_id: 'c2',
      run_id: 'run-2',
      passed: false,
      assertions: [
        { id: 'a1', passed: true, reason: null },
        { id: 'a2', passed: false, reason: 'output does not contain "refund"' },
      ],
      latency_ms: 1200,
      cost_usd: '0.22',
    },
  ],
  requested_by: 'u-me',
  started_at: '2026-11-13T10:01:00Z',
  finished_at: '2026-11-13T10:02:00Z',
  created_at: '2026-11-13T10:00:30Z',
};

export const QUEUED: EvaluationRunOut = {
  ...EVALUATION,
  id: 'ev-2',
  status: 'queued',
  status_reason: null,
  accuracy: null,
  p95_latency_ms: null,
  total_cost_usd: null,
  results: null,
  started_at: null,
  finished_at: null,
};

export const WORKSPACES = {
  'GET /api/v1/orgs/acme/workspaces': () =>
    list([{ id: 'ws-1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 1 }]),
  'GET /api/v1/orgs/acme/workspaces/eng/projects': () =>
    list([{ id: 'p-1', slug: 'api', name: 'API', status: 'active', row_version: 1 }]),
};

export { list } from './security';

/** stubApi plus the release permissions and one solution, version, release, dataset and run. */
export function stubReleases(
  routes: Record<string, Handler> = {},
  perms: string[] = [...ALL_PERMS, ...RELEASE_PERMS],
) {
  return stubApi(
    {
      ...WORKSPACES,
      [`GET ${S}`]: () => list([SOLUTION]),
      [`GET ${S}/s-1`]: () => jsonResponse(SOLUTION),
      [`GET ${S}/s-1/versions`]: () => list([VERSION]),
      [`GET ${S}/s-1/versions/sv-1/releases`]: () => list([RELEASE]),
      [`GET ${ORG}/releases/r-1`]: () => jsonResponse(RELEASE),
      [`GET ${ORG}/releases/r-1/evaluations`]: () => list([EVALUATION]),
      [`GET ${ORG}/evaluation-runs/ev-1`]: () => jsonResponse(EVALUATION),
      [`GET ${DS}`]: () => list([DATASET]),
      [`GET ${ORG}/connections`]: () => list([]),
      [`GET ${ORG}/model-providers`]: () => list([]),
      [`GET ${ORG}/agents`]: () => list([]),
      ...routes,
    },
    perms,
  );
}
