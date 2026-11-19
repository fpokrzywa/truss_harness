import type { ApiClient } from './client';
import type {
  CredentialIssued,
  CredentialOut,
  DeliveryExecutorOut,
  ExecutorStatus,
  TargetCreate,
  TargetOut,
  TargetStatus,
} from './delivery-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const del$ = (org: string) => `/api/v1/orgs/${enc(org)}/delivery`;
const cred$ = (org: string, executorId: string) =>
  `${del$(org)}/executors/${enc(executorId)}/credentials`;

/** Delivery targets and executors (`shifde.delivery.api`); one page per list. */
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
  };
}
