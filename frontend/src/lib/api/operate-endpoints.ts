import type { ApiClient } from './client';
import type {
  Approval,
  ApprovalDecision,
  ApprovalSummary,
  CreateRunInput,
  Run,
  RunEvent,
  RunFilters,
  RunResult,
  Step,
} from './operate-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;

/** Accepts the list envelope or a bare array. */
const items = <T>(res: ListResponse<T> | T[]): T[] => (Array.isArray(res) ? res : res.items);

/** Phase 5 routes (docs/api/PHASE_5_CONTRACT.md). */
export function createOperateEndpoints(client: ApiClient) {
  return {
    /** `idempotencyKey` is sent as the Idempotency-Key header; a replay returns the same run. */
    createRun: (org: string, input: CreateRunInput, idempotencyKey: string) =>
      client.request<Run>(`${org$(org)}/runs`, {
        method: 'POST',
        body: input,
        headers: { 'Idempotency-Key': idempotencyKey },
      }),
    listRuns: (org: string, filters: RunFilters, cursor?: string, limit = 50) =>
      client.request<ListResponse<Run>>(`${org$(org)}/runs`, {
        query: { ...filters, limit, cursor },
      }),
    getRun: (org: string, id: string) => client.request<Run>(`${org$(org)}/runs/${enc(id)}`),
    runSteps: async (org: string, id: string) =>
      items(
        await client.request<ListResponse<Step> | Step[]>(`${org$(org)}/runs/${enc(id)}/steps`),
      ),
    runEvents: async (org: string, id: string) =>
      items(
        await client.request<ListResponse<RunEvent> | RunEvent[]>(
          `${org$(org)}/runs/${enc(id)}/events`,
        ),
      ),
    runResult: (org: string, id: string) =>
      client.request<RunResult>(`${org$(org)}/runs/${enc(id)}/result`),
    cancelRun: (org: string, id: string) =>
      client.request<Run | undefined>(`${org$(org)}/runs/${enc(id)}/cancel`, {
        method: 'POST',
        body: {},
      }),

    listApprovals: (org: string, status: string, cursor?: string, treeRunId?: string) =>
      client.request<ListResponse<ApprovalSummary>>(`${org$(org)}/approvals`, {
        query: { status, limit: 50, cursor, tree_run_id: treeRunId },
      }),
    getApproval: (org: string, id: string) =>
      client.request<Approval>(`${org$(org)}/approvals/${enc(id)}`),
    decideApproval: (org: string, id: string, decision: ApprovalDecision, comment?: string) =>
      client.request<Approval>(`${org$(org)}/approvals/${enc(id)}/decide`, {
        method: 'POST',
        body: { decision, ...(comment ? { comment } : {}) },
      }),
  };
}
