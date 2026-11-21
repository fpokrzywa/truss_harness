import type { ApiClient } from './client';
import type {
  ApiKeyFilters,
  ApiKeyMeta,
  ApiKeyWithSecret,
  AuditEvent,
  AuditFilters,
  AuditVerifyResult,
  BindingFilters,
  CreateApiKeyInput,
  CreateBindingInput,
  CreateSecretInput,
  CreateUserInput,
  CreateUserResult,
  Group,
  Idp,
  IdpInput,
  IdpTestResult,
  MemberStatus,
  PermissionDef,
  PlatformGroup,
  PlatformGroupCreate,
  Role,
  RoleBinding,
  RoleInput,
  SecretMeta,
  ServiceAccount,
  UserMember,
} from './security-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;

export const orgBase = (org: string) => `/api/v1/orgs/${enc(org)}`;
/** Base path for IdP routes: tenant IdPs live under the org, installation IdPs under /platform. */
export const tenantIdpBase = (org: string) => orgBase(org);
export const PLATFORM_IDP_BASE = '/api/v1/platform';
const PLATFORM_GROUPS = '/api/v1/platform/groups';

const idpPath = (base: string, id: string) => `${base}/idps/${enc(id)}`;

type Query = Record<string, string | number | undefined>;

/** Phase 2 routes (docs/api/PHASE_2_CONTRACT.md §1-§6). */
export function createSecurityEndpoints(client: ApiClient) {
  const list = <T>(path: string, query?: Query) =>
    client.request<ListResponse<T>>(path, { query: { limit: 200, ...query } });
  const send = <T>(method: 'POST' | 'PATCH' | 'PUT', path: string, body: unknown) =>
    client.request<T>(path, { method, body });

  return {
    permissionCatalog: async (): Promise<PermissionDef[]> => {
      const res = await client.request<ListResponse<PermissionDef> | PermissionDef[]>(
        '/api/v1/permissions',
        { query: { limit: 200 } },
      );
      return Array.isArray(res) ? res : res.items;
    },

    listUsers: (org: string) => list<UserMember>(`${orgBase(org)}/users`),
    createUser: (org: string, input: CreateUserInput) =>
      send<CreateUserResult>('POST', `${orgBase(org)}/users`, input),
    setUserStatus: (org: string, userId: string, status: MemberStatus, rowVersion: number) =>
      send<UserMember>('PATCH', `${orgBase(org)}/users/${enc(userId)}`, {
        status,
        row_version: rowVersion,
      }),

    listGroups: (org: string) => list<Group>(`${orgBase(org)}/groups`),
    getGroup: (org: string, id: string) =>
      client.request<Group>(`${orgBase(org)}/groups/${enc(id)}`),

    listPlatformGroups: () => list<PlatformGroup>(PLATFORM_GROUPS),
    createPlatformGroup: (input: PlatformGroupCreate) =>
      send<PlatformGroup>('POST', PLATFORM_GROUPS, input),

    listRoles: (org: string) => list<Role>(`${orgBase(org)}/roles`),
    createRole: (org: string, input: RoleInput) =>
      send<Role>('POST', `${orgBase(org)}/roles`, input),
    updateRole: (org: string, id: string, input: RoleInput & { row_version: number }) =>
      send<Role>('PATCH', `${orgBase(org)}/roles/${enc(id)}`, input),

    listBindings: (org: string, filters: BindingFilters = {}) =>
      list<RoleBinding>(`${orgBase(org)}/role-bindings`, { ...filters }),
    createBinding: (org: string, input: CreateBindingInput) =>
      send<RoleBinding>('POST', `${orgBase(org)}/role-bindings`, input),
    deleteBinding: (org: string, id: string) =>
      client.request<void>(`${orgBase(org)}/role-bindings/${enc(id)}`, { method: 'DELETE' }),

    listServiceAccounts: (org: string) => list<ServiceAccount>(`${orgBase(org)}/service-accounts`),
    createServiceAccount: (org: string, input: { name: string; description?: string }) =>
      send<ServiceAccount>('POST', `${orgBase(org)}/service-accounts`, input),
    setServiceAccountStatus: (org: string, sa: ServiceAccount, status: 'active' | 'disabled') =>
      send<ServiceAccount>('PATCH', `${orgBase(org)}/service-accounts/${enc(sa.id)}`, {
        status,
        row_version: sa.row_version,
      }),

    listApiKeys: (org: string, filters: ApiKeyFilters = {}) =>
      list<ApiKeyMeta>(`${orgBase(org)}/api-keys`, { ...filters }),
    createApiKey: (org: string, input: CreateApiKeyInput) =>
      send<ApiKeyWithSecret>('POST', `${orgBase(org)}/api-keys`, input),
    rotateApiKey: (org: string, id: string, graceHours: number) =>
      send<ApiKeyWithSecret>('POST', `${orgBase(org)}/api-keys/${enc(id)}/rotate`, {
        grace_hours: graceHours,
      }),
    revokeApiKey: (org: string, id: string) =>
      send<ApiKeyMeta>('POST', `${orgBase(org)}/api-keys/${enc(id)}/revoke`, {}),

    listIdps: (base: string) => list<Idp>(`${base}/idps`),
    createIdp: (base: string, input: IdpInput) => send<Idp>('POST', `${base}/idps`, input),
    updateIdp: (base: string, id: string, input: IdpInput) =>
      send<Idp>('PATCH', idpPath(base, id), input),
    testIdp: (base: string, id: string) =>
      send<IdpTestResult>('POST', `${idpPath(base, id)}/test`, {}),

    listSecrets: (org: string) => list<SecretMeta>(`${orgBase(org)}/secrets`),
    createSecret: (org: string, input: CreateSecretInput) =>
      send<SecretMeta>('POST', `${orgBase(org)}/secrets`, input),
    rotateSecret: (org: string, id: string, value: string) =>
      send<SecretMeta>('POST', `${orgBase(org)}/secrets/${enc(id)}/rotate`, { value }),
    disableSecret: (org: string, s: SecretMeta) =>
      send<SecretMeta>('PATCH', `${orgBase(org)}/secrets/${enc(s.id)}`, {
        status: 'disabled',
        row_version: s.row_version ?? 0,
      }),

    listAuditEvents: (org: string, filters: AuditFilters = {}) =>
      client.request<ListResponse<AuditEvent>>(`${orgBase(org)}/audit-events`, {
        query: { category: 'security', limit: 50, ...filters },
      }),
    verifyAudit: (org: string) =>
      client.request<AuditVerifyResult>(`${orgBase(org)}/audit-events/verify`),
  };
}
