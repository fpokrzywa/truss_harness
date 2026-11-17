import type { ApiClient } from './client';
import type {
  ActionInvokeInput,
  LineageRecord,
  Mapping,
  MappingCheck,
  MappingTargetKind,
  ObjectView,
  OntologyQuery,
  OntologyQueryResult,
  Policy,
  PolicyInput,
  PrincipalAttributes,
  PrincipalType,
} from './ontology-types';
import type { Run } from './operate-types';
import type { ListResponse } from './types';
import { normalizeObjectView } from '../ontology-spec';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
const ont$ = (org: string, id: string) => `${org$(org)}/ontologies/${enc(id)}`;
const items = <T>(res: ListResponse<T> | T[]): T[] => (Array.isArray(res) ? res : res.items);

/** Phase 7 routes (docs/api/PHASE_7_CONTRACT.md); ontology CRUD/versions reuse the Build artifact endpoints. */
export function createOntologyEndpoints(client: ApiClient) {
  const send = <T>(
    method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
    path: string,
    body?: unknown,
  ) => client.request<T>(path, { method, ...(body === undefined ? {} : { body }) });
  const mapPath = (org: string, id: string, kind: MappingTargetKind, key: string) =>
    `${ont$(org, id)}/draft/mappings/${kind}/${enc(key)}`;

  return {
    /** `vid` is a version id or `draft`. */
    listMappings: async (org: string, id: string, vid: string): Promise<Mapping[]> =>
      items(
        await client.request<ListResponse<Mapping> | Mapping[]>(
          `${ont$(org, id)}/versions/${enc(vid)}/mappings`,
        ),
      ),
    putMapping: (org: string, id: string, m: Mapping) =>
      send<Mapping>('PUT', mapPath(org, id, m.target_kind, m.target_key), m),
    deleteMapping: (org: string, id: string, kind: MappingTargetKind, key: string) =>
      send<void>('DELETE', mapPath(org, id, kind, key)),
    checkMapping: (org: string, id: string, kind: MappingTargetKind, key: string) =>
      send<MappingCheck>('POST', `${mapPath(org, id, kind, key)}/check`, {}),

    listPolicies: async (org: string, id: string): Promise<Policy[]> =>
      items(await client.request<ListResponse<Policy> | Policy[]>(`${ont$(org, id)}/policies`)),
    createPolicy: (org: string, id: string, input: PolicyInput) =>
      send<Policy>('POST', `${ont$(org, id)}/policies`, input),
    updatePolicy: (org: string, id: string, p: Policy, input: PolicyInput) =>
      send<Policy>('PATCH', `${ont$(org, id)}/policies/${enc(p.id)}`, {
        ...input,
        ...(p.row_version !== undefined ? { row_version: p.row_version } : {}),
      }),
    deletePolicy: (org: string, id: string, pid: string) =>
      send<void>('DELETE', `${ont$(org, id)}/policies/${enc(pid)}`),

    queryOntology: (org: string, id: string, body: OntologyQuery) =>
      send<OntologyQueryResult>('POST', `${ont$(org, id)}/query`, body),
    getObject: async (
      org: string,
      id: string,
      objectType: string,
      pk: string,
      versionId?: string,
    ): Promise<ObjectView> =>
      normalizeObjectView(
        await client.request<unknown>(`${ont$(org, id)}/objects/${enc(objectType)}/${enc(pk)}`, {
          query: { version_id: versionId || undefined },
        }),
      ),
    listLineage: (org: string, ontologyId: string, runId?: string, cursor?: string) =>
      client.request<ListResponse<LineageRecord>>(`${org$(org)}/ontology-lineage`, {
        query: { ontology_id: ontologyId, run_id: runId || undefined, cursor, limit: 50 },
      }),
    getPrincipalAttributes: (org: string, type: PrincipalType, principalId: string) =>
      client.request<PrincipalAttributes>(
        `${org$(org)}/ontology-principal-attributes/${type}/${enc(principalId)}`,
      ),
    /** Replaces the whole attribute map; `rowVersion` guards concurrent edits. */
    putPrincipalAttributes: (
      org: string,
      type: PrincipalType,
      principalId: string,
      attributes: Record<string, unknown>,
      rowVersion?: number,
    ) =>
      send<PrincipalAttributes>(
        'PUT',
        `${org$(org)}/ontology-principal-attributes/${type}/${enc(principalId)}`,
        { attributes, ...(rowVersion !== undefined ? { row_version: rowVersion } : {}) },
      ),
    /** 202 Run (kind `ontology_action`); approvals and side-effect rules apply server-side. */
    invokeAction: (org: string, id: string, actionKey: string, body: ActionInvokeInput) =>
      send<Run>('POST', `${ont$(org, id)}/actions/${enc(actionKey)}/invoke`, body),
  };
}
