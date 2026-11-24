import type {
  AssistCatalogOut,
  AssistRequestOut,
  AssistRequestSummary,
  AssistSettingsOut,
  CatalogEntryOut,
  ProposalOut,
  SpecialistOut,
} from '@/lib/api/assist-types';
import { MODEL } from './appgen';
import { ORG, WORKSPACES } from './releases';
import { list, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export { ORG } from './releases';
export { bodyOf, list } from './security';

export const A = `${ORG}/assist`;
/** The eng/api project's assist routes. */
export const PA = `${ORG}/workspaces/eng/projects/api/assist`;
export const REQ_ID = '01900000-0000-7000-8000-0000000000f1';
export const REQ = `${A}/requests/${REQ_ID}`;
export const RELEASE_ID = '01900000-0000-7000-8000-0000000000a1';
export const RUN_ID = '01900000-0000-7000-8000-0000000000b1';
export const RESULT = `${ORG}/runs/${RUN_ID}/result`;

export const USER_PERMS = [
  'assist:request:read',
  'assist:request:write',
  'assist:proposal:accept',
  'usage:read',
];

export const INSTALL_PERMS = [
  'assist:install',
  'prompt:create',
  'prompt:update',
  'prompt:publish',
  'tool:create',
  'tool:update',
  'tool:publish',
  'agent:create',
  'agent:update',
  'agent:publish',
  'agent:activate',
];

const entry = (key: string, title: string, proposal_kinds: string[] = []): CatalogEntryOut => ({
  key,
  title,
  router_description: `What ${title} does.`,
  tools: [],
  required_permissions: [],
  proposal_kinds,
  installs: [],
});

export const catalog = (over: Partial<AssistCatalogOut> = {}): AssistCatalogOut => ({
  catalog_version: 'assist.catalog/1',
  assist_enabled: true,
  entries: [
    entry('research', 'Research'),
    entry('solution_design', 'Solution design'),
    entry('build', 'Build', ['appgen_request']),
    entry('evaluation', 'Evaluation', ['evaluation_run']),
    entry('cost', 'Cost'),
    entry('deployment', 'Deployment', ['delivery_plan']),
  ],
  ...over,
});

export const specialist = (key: string, over: Partial<SpecialistOut> = {}): SpecialistOut => ({
  id: `sp-${key}`,
  key,
  title: key,
  workspace_id: 'ws-1',
  project_id: 'p-1',
  status: 'enabled',
  agent_id: `agent-${key}`,
  agent_version_id: `av-${key}`,
  catalog_version: 'assist.catalog/1',
  available: true,
  installed_by: 'u-me',
  created_at: '2026-11-22T10:00:00Z',
  updated_at: null,
  ...over,
});

export const settings = (over: Partial<AssistSettingsOut> = {}): AssistSettingsOut => ({
  assist_enabled: true,
  catalog_version: 'assist.catalog/1',
  current_catalog_version: 'assist.catalog/1',
  model_routing_enabled: false,
  router_model_id: null,
  specialist_max_cost: 1,
  updated_at: '2026-11-22T10:00:00Z',
  ...over,
});

export const summary = (over: Partial<AssistRequestSummary> = {}): AssistRequestSummary => ({
  id: REQ_ID,
  workspace_id: 'ws-1',
  project_id: 'p-1',
  status: 'routed',
  status_reason: null,
  requested_specialist: null,
  specialist_key: 'deployment',
  route_method: 'rule',
  run_id: RUN_ID,
  requested_by: 'u-me',
  created_at: '2026-11-23T10:00:00Z',
  updated_at: null,
  ...over,
});

export const request = (over: Partial<AssistRequestOut> = {}): AssistRequestOut => ({
  ...summary(),
  rule_id: 'deploy',
  router_version: 'assist.router/1',
  candidates: ['deployment'],
  missing_permissions: null,
  context: null,
  run: { id: RUN_ID, status: 'running', error_code: null },
  proposals: [],
  ...over,
});

/** A refused answer, with no run. */
export const refused = (
  status_reason: string,
  over: Partial<AssistRequestOut> = {},
): AssistRequestOut =>
  request({
    status: 'refused',
    status_reason,
    specialist_key: null,
    route_method: null,
    rule_id: null,
    run_id: null,
    run: null,
    candidates: [],
    ...over,
  });

export const proposal = (over: Partial<ProposalOut> = {}): ProposalOut => ({
  id: 'prop-1',
  run_id: RUN_ID,
  kind: 'evaluation_run',
  payload: { release_id: RELEASE_ID },
  status: 'open',
  status_reason: null,
  result_ref: null,
  decided_by: null,
  decided_at: null,
  created_at: '2026-11-23T10:01:00Z',
  updated_at: null,
  ...over,
});

/** The kind's payload and the result its accept answers with. */
export const APP_PROPOSAL = proposal({
  id: 'prop-app',
  kind: 'appgen_request',
  payload: {
    connection_id: 'conn-dbx',
    tables: ['main.sales.orders', 'main.sales.customers'],
    brief: 'An app for sales managers.',
    model_id: 'm-1',
  },
});
export const EVAL_PROPOSAL = proposal({ id: 'prop-eval' });
export const PLAN_PROPOSAL = proposal({
  id: 'prop-plan',
  kind: 'delivery_plan',
  payload: {
    target_id: 't-1',
    release_id: RELEASE_ID,
    application_key: 'sales-app',
    display_name: 'Sales app',
  },
});

interface Perms {
  org?: string[];
  project?: string[];
}

/**
 * The eng/api project with six available specialists and one request; permissions are answered per
 * scope: `project` for eng/api, `org` without a project.
 */
export function stubAssist(
  routes: Record<string, Handler> = {},
  { org = [], project = USER_PERMS }: Perms = {},
) {
  return stubApi({
    ...WORKSPACES,
    [`GET ${ORG}/me/permissions`]: ({ url }) => {
      const q = new URL(url, 'http://x').searchParams;
      const scoped = q.get('workspace') === 'eng' && q.get('project') === 'api';
      return jsonResponse({ permissions: scoped ? project : org });
    },
    [`GET ${A}/catalog`]: () => jsonResponse(catalog()),
    [`GET ${A}/settings`]: () => jsonResponse(settings()),
    [`GET ${PA}/specialists`]: () => jsonResponse(catalog().entries.map((e) => specialist(e.key))),
    [`GET ${PA}/requests`]: () => list([summary()]),
    [`GET ${REQ}`]: () => jsonResponse(request()),
    [`GET ${ORG}/models/effective`]: () => list([MODEL]),
    ...routes,
  });
}
