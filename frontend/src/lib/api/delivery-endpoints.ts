import type { ApiClient } from './client';
import type {
  CredentialIssued,
  CredentialOut,
  DeliveryExecutorOut,
  DeploymentDetail,
  DeploymentOut,
  DeploymentStatus,
  ExecutorStatus,
  PlanCreate,
  PlanOut,
  PlanStatus,
  TargetCreate,
  TargetOut,
  TargetStatus,
} from './delivery-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const del$ = (org: string) => `/api/v1/orgs/${enc(org)}/delivery`;
const cred$ = (org: string, executorId: string) =>
  `${del$(org)}/executors/${enc(executorId)}/credentials`;

/** Delivery targets, executors, plans and deployments (`shifde.delivery.api`); one page per list. */
export function createDeliveryEndpoints(client: ApiClient) {
  const send = <T>(method: 'POST' | 'PATCH', path: string, body: unknown) =>
    client.request<T>(path, { method, body });

  return {
    // Targets
    listTargets: (org: string, q: { status?: TargetStatus } = {}) =>
      client.request<ListResponse<TargetOut>>(`${del$(org)}/targets`, { query: q }),
    registerTarget: (org: string, input: TargetCreate) =>
      send<TargetOut>('POST', `${del$(org)}/targets`, input),
    archiveTarget: (org: string, id: string) =>
      send<TargetOut>('POST', `${del$(org)}/targets/${enc(id)}/archive`, {}),

    // Executors
    listExecutors: (org: string) =>
      client.request<ListResponse<DeliveryExecutorOut>>(`${del$(org)}/executors`),
    createExecutor: (org: string, name: string) =>
      send<DeliveryExecutorOut>('POST', `${del$(org)}/executors`, { name }),
    setExecutorStatus: (org: string, id: string, status: ExecutorStatus) =>
      send<DeliveryExecutorOut>('PATCH', `${del$(org)}/executors/${enc(id)}`, { status }),

    // Executor credentials
    listExecutorCredentials: (org: string, executorId: string) =>
      client.request<CredentialOut[]>(cred$(org, executorId)),
    issueExecutorCredential: (org: string, executorId: string) =>
      send<CredentialIssued>('POST', cred$(org, executorId), {}),
    rotateExecutorCredential: (org: string, executorId: string, credentialId: string) =>
      send<CredentialIssued>('POST', `${cred$(org, executorId)}/${enc(credentialId)}/rotate`, {}),
    revokeExecutorCredential: (org: string, executorId: string, credentialId: string) =>
      send<CredentialOut>('POST', `${cred$(org, executorId)}/${enc(credentialId)}/revoke`, {}),

    // Plans
    listPlans: (org: string, q: { status?: PlanStatus; target?: string } = {}) =>
      client.request<ListResponse<PlanOut>>(`${del$(org)}/plans`, { query: q }),
    getPlan: (org: string, id: string) => client.request<PlanOut>(`${del$(org)}/plans/${enc(id)}`),
    requestPlan: (org: string, input: PlanCreate) =>
      send<PlanOut>('POST', `${del$(org)}/plans`, input),
    approvePlan: (org: string, id: string, body: { plan_digest: string; note?: string }) =>
      send<PlanOut>('POST', `${del$(org)}/plans/${enc(id)}/approve`, body),
    rejectPlan: (org: string, id: string, body: { note?: string }) =>
      send<PlanOut>('POST', `${del$(org)}/plans/${enc(id)}/reject`, body),

    // Deployments
    listDeployments: (org: string, q: { status?: DeploymentStatus; target?: string } = {}) =>
      client.request<ListResponse<DeploymentOut>>(`${del$(org)}/deployments`, { query: q }),
    getDeployment: (org: string, id: string) =>
      client.request<DeploymentDetail>(`${del$(org)}/deployments/${enc(id)}`),
    cancelDeployment: (org: string, id: string) =>
      send<DeploymentOut>('POST', `${del$(org)}/deployments/${enc(id)}/cancel`, {}),
    reconcileDeployment: (org: string, id: string) =>
      send<DeploymentOut>('POST', `${del$(org)}/deployments/${enc(id)}/reconcile`, {}),
    markDeploymentFailed: (org: string, id: string, note: string) =>
      send<DeploymentOut>('POST', `${del$(org)}/deployments/${enc(id)}/mark-failed`, { note }),
  };
}
