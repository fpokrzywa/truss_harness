import type { ApiClient } from './client';
import type {
  CreatePipelineRunInput,
  NodeRun,
  NodeTypeDef,
  PipelineSchedule,
  RunArtifact,
} from './pipeline-types';
import type { Run } from './operate-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;

const items = <T>(res: ListResponse<T> | T[]): T[] => (Array.isArray(res) ? res : res.items);

/** Phase 6 routes (docs/api/PHASE_6_CONTRACT.md); pipeline CRUD/versions reuse the Build artifact endpoints. */
export function createPipelineEndpoints(client: ApiClient) {
  return {
    pipelineNodeTypes: async (): Promise<NodeTypeDef[]> =>
      items(
        await client.request<ListResponse<NodeTypeDef> | NodeTypeDef[]>(
          '/api/v1/pipelines/node-types',
        ),
      ),
    /** Test runs send `version_id: "draft"` and `is_test: true`. */
    createPipelineRun: (org: string, input: CreatePipelineRunInput, idempotencyKey: string) =>
      client.request<Run>(`${org$(org)}/runs`, {
        method: 'POST',
        body: input,
        headers: { 'Idempotency-Key': idempotencyKey },
      }),
    listPipelineRuns: (org: string, pipelineId: string, cursor?: string, limit = 50) =>
      client.request<ListResponse<Run>>(`${org$(org)}/pipelines/${enc(pipelineId)}/runs`, {
        query: { limit, cursor },
      }),
    runNodes: async (org: string, runId: string): Promise<NodeRun[]> =>
      items(
        await client.request<ListResponse<NodeRun> | NodeRun[]>(
          `${org$(org)}/runs/${enc(runId)}/nodes`,
        ),
      ),
    runArtifact: (org: string, runId: string, artifactId: string) =>
      client.request<RunArtifact>(`${org$(org)}/runs/${enc(runId)}/artifacts/${enc(artifactId)}`),
    /** Amendment E1: 501 `scheduling_not_enabled` unless the feature flag is on; then a bare list. */
    listPipelineSchedules: async (org: string, pipelineId: string): Promise<PipelineSchedule[]> =>
      items(
        await client.request<ListResponse<PipelineSchedule> | PipelineSchedule[]>(
          `${org$(org)}/pipelines/${enc(pipelineId)}/schedules`,
        ),
      ),
  };
}
