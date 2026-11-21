import type { ApiClient } from './client';
import type { DatasetCreate, DatasetOut, EvaluationRunOut } from './evaluation-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
const ds$ = (org: string, ws: string, proj: string) =>
  `${org$(org)}/workspaces/${enc(ws)}/projects/${enc(proj)}/evaluation-datasets`;

/** Evaluation datasets and runs (`shifde.evaluation.api`); one page per list. */
export function createEvaluationEndpoints(client: ApiClient) {
  return {
    // Datasets
    listDatasets: (org: string, ws: string, proj: string) =>
      client.request<ListResponse<DatasetOut>>(ds$(org, ws, proj)),
    createDataset: (org: string, ws: string, proj: string, input: DatasetCreate) =>
      client.request<DatasetOut>(ds$(org, ws, proj), { method: 'POST', body: input }),
    getDataset: (org: string, ws: string, proj: string, id: string) =>
      client.request<DatasetOut>(`${ds$(org, ws, proj)}/${enc(id)}`),

    // Evaluation runs
    requestEvaluation: (org: string, releaseId: string) =>
      client.request<EvaluationRunOut>(`${org$(org)}/releases/${enc(releaseId)}/evaluations`, {
        method: 'POST',
        body: {},
      }),
    listReleaseEvaluations: (org: string, releaseId: string) =>
      client.request<ListResponse<EvaluationRunOut>>(
        `${org$(org)}/releases/${enc(releaseId)}/evaluations`,
      ),
    getEvaluationRun: (org: string, id: string) =>
      client.request<EvaluationRunOut>(`${org$(org)}/evaluation-runs/${enc(id)}`),
  };
}
