import { BUILD_PERMS } from './build';
import { list, stubApi } from './security';
import type { Handler } from './utils';

export const ONTOLOGY_PERMS = [
  ...BUILD_PERMS,
  'ontology:read',
  'ontology:create',
  'ontology:update',
  'ontology:publish',
  'ontology:query',
  'ontology:manage_policies',
  'connection:use',
  'connection:test',
];

/** stubApi with ontology permissions and the lookups the ontology panels perform. */
export function stubOntology(routes: Record<string, Handler> = {}, perms = ONTOLOGY_PERMS) {
  return stubApi(
    {
      'GET /api/v1/orgs/acme/connections': () => list([]),
      'GET /api/v1/orgs/acme/roles': () => list([]),
      'GET /api/v1/orgs/acme/groups': () => list([]),
      'GET /api/v1/orgs/acme/users': () => list([]),
      ...routes,
    },
    perms,
  );
}
