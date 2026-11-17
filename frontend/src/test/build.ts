import type { Artifact, ArtifactKind, ArtifactVersion } from '@/lib/api/build-types';
import { ALL_PERMS, list, stubApi } from './security';
import { jsonResponse, type Handler } from './utils';

export const BUILD_PERMS = [
  ...ALL_PERMS,
  'model:read',
  'prompt:read',
  'prompt:create',
  'prompt:update',
  'prompt:publish',
  'prompt:test',
  'tool:read',
  'tool:create',
  'tool:update',
  'tool:publish',
  'tool:test',
  'tool:test_side_effects',
  'skill:read',
  'skill:create',
  'skill:update',
  'skill:publish',
  'agent:read',
  'agent:create',
  'agent:update',
  'agent:publish',
  'agent:test',
  'agent:activate',
  'approval:decide',
  'model:invoke',
  'connection:read',
];

export function artifact(over: Partial<Artifact> = {}): Artifact {
  return {
    id: 'a-1',
    key: 'support.reply',
    name: 'Support reply',
    description: null,
    scope: { type: 'tenant', id: 'org-1' },
    visibility: 'scope',
    status: 'active',
    latest_published_version: { id: 'v-1', version_no: 1 },
    has_draft: true,
    row_version: 1,
    ...over,
  };
}

export function version(over: Partial<ArtifactVersion> = {}): ArtifactVersion {
  return { id: 'v-1', version_no: 1, state: 'published', spec_hash: 'abcdef0123456789', ...over };
}

export const MODEL = {
  id: 'm-1',
  provider_id: 'p-1',
  model_key: 'fake-chat',
  display_name: 'Fake chat',
  capabilities: ['chat'],
  context_window: 8000,
  max_output_tokens: 1000,
  lifecycle: 'active',
  row_version: 1,
};

/** stubApi with the Build permissions and the lookups every editor performs. */
export function stubBuild(
  kind: ArtifactKind,
  id: string,
  routes: Record<string, Handler> = {},
  perms: string[] = BUILD_PERMS,
) {
  const base = `/api/v1/orgs/acme/${kind}`;
  return stubApi(
    {
      'GET /api/v1/orgs/acme/models/effective': () => list([MODEL]),
      'GET /api/v1/orgs/acme/model-routes': () => list([]),
      'GET /api/v1/orgs/acme/connections': () => list([]),
      'GET /api/v1/tool-executors': () => jsonResponse({ items: [] }),
      [`GET ${base}`]: () => list([]),
      [`GET ${base}/${id}`]: () => jsonResponse(artifact({ id })),
      ...routes,
    },
    perms,
  );
}
