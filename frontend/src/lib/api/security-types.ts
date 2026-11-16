// Phase 2: identity & security types (docs/api/PHASE_2_CONTRACT.md).
// MyPermissions, PermissionDef and SetupRedeemResult live in ./types (shell-owned); re-exported here.
export type { MyPermissions, PermissionDef, SetupRedeemResult } from './types';

export type MemberStatus = 'active' | 'suspended';

export interface GroupRef {
  id: string;
  slug?: string;
  name: string;
}

/** Item of `GET …/users`. `row_version` may sit on the item or on `user` (contract is silent). */
export interface UserMember {
  user: { id: string; email: string; display_name: string; row_version?: number };
  status: MemberStatus;
  row_version?: number;
  groups?: GroupRef[];
  role_bindings?: RoleBinding[];
}

export interface CreateUserInput {
  email: string;
  display_name: string;
  role_ids?: string[];
}

export interface CreateUserResult {
  user: UserMember['user'];
  setup_token?: string | null;
}

export type GroupSource = 'local' | 'idp';

export interface GroupMember {
  id: string;
  email: string;
  display_name: string;
}

export interface Group {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  source: GroupSource;
  row_version: number;
  member_count?: number;
  /** Present on `GET …/groups/{id}`. */
  members?: GroupMember[];
}

export interface Role {
  id: string;
  key: string;
  name: string;
  description?: string | null;
  is_system: boolean;
  permissions: string[];
  row_version: number;
}

export interface RoleInput {
  key?: string;
  name: string;
  description?: string;
  permissions: string[];
}

export type SubjectType = 'user' | 'group' | 'service_account';
export type ScopeType = 'tenant' | 'workspace' | 'project' | 'resource';

export interface BindingScope {
  type: ScopeType;
  id: string;
  resource_type?: string;
  name?: string | null;
}

export interface RoleBinding {
  id: string;
  subject_type: SubjectType;
  subject_id: string;
  role_id: string;
  role_name?: string | null;
  scope: BindingScope;
  expires_at?: string | null;
  created_at?: string;
}

export interface CreateBindingInput {
  subject_type: SubjectType;
  subject_id: string;
  role_id: string;
  scope: BindingScope;
  expires_at?: string;
}

export interface BindingFilters {
  subject_type?: SubjectType;
  subject_id?: string;
  scope_type?: ScopeType;
  scope_id?: string;
}

export interface ServiceAccount {
  id: string;
  name: string;
  description?: string | null;
  status: 'active' | 'disabled';
  row_version: number;
  created_at?: string;
}

export type PrincipalType = 'service_account' | 'user';

export interface ApiKeyMeta {
  id: string;
  name: string;
  prefix: string;
  principal_type: PrincipalType;
  principal_id: string;
  scopes: string[];
  resource_scope?: { workspace_ids?: string[]; project_ids?: string[] } | null;
  expires_at?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
  rotated_from_id?: string | null;
}

export interface CreateApiKeyInput {
  name: string;
  principal_type: PrincipalType;
  principal_id: string;
  scopes: string[];
  resource_scope?: { workspace_ids?: string[]; project_ids?: string[] };
  expires_in_days: number;
}

/** `secret` is the only place a key secret ever appears; callers must not persist it. */
export interface ApiKeyWithSecret {
  api_key: ApiKeyMeta;
  secret: string;
}

export interface ApiKeyFilters {
  principal_type?: PrincipalType;
  principal_id?: string;
}

export type JitMode = 'off' | 'create_user' | 'create_and_join';
export type GroupSyncMode = 'off' | 'sync';

export interface Idp {
  id: string;
  display_name: string;
  issuer: string;
  discovery_url: string;
  client_id: string;
  client_secret_set: boolean;
  scopes: string[];
  claim_mappings: { email: string; groups: string };
  jit: JitMode;
  group_sync: GroupSyncMode;
  status: 'active' | 'disabled';
  row_version: number;
}

export interface IdpInput {
  display_name: string;
  issuer: string;
  discovery_url: string;
  client_id: string;
  /** Write-only. Omitted on edit to keep the stored secret. */
  client_secret?: string;
  scopes: string[];
  claim_mappings: { email: string; groups: string };
  jit: JitMode;
  group_sync: GroupSyncMode;
  status?: 'active' | 'disabled';
  row_version?: number;
}

export interface IdpTestCheck {
  name?: string;
  ok?: boolean;
  detail?: string;
  message?: string;
}

export interface IdpTestResult {
  ok: boolean;
  checks: (IdpTestCheck | string)[];
}

/** `scope` is a string or `{type,id}`-like object; the contract leaves its response form open. */
export type SecretScope = string | { type: string; id?: string; name?: string | null } | null;

export interface SecretMeta {
  id: string;
  name: string;
  scope?: SecretScope;
  kind?: string | null;
  created_at?: string;
  rotated_at?: string | null;
  last_used_at?: string | null;
  version: number;
  status?: 'active' | 'disabled';
  row_version?: number;
}

export interface CreateSecretInput {
  name: string;
  /** Write-only: never returned by the API and never retained by the UI after submit. */
  value: string;
  scope?: { type: ScopeType; id: string };
}

export interface AuditEvent {
  id: string;
  at?: string;
  occurred_at?: string;
  created_at?: string;
  actor?: string | null;
  action: string;
  target?: string | null;
  outcome?: string | null;
  category?: string;
  request_id?: string | null;
}

export interface AuditFilters {
  /** Defaults to `security`; the Audit Explorer passes an explicit `undefined` to read all categories. */
  category?: string | undefined;
  actor?: string;
  action?: string;
  from?: string;
  to?: string;
  cursor?: string;
}

export interface AuditVerifyResult {
  ok: boolean;
  checked: number;
  first_broken_id?: string | null;
}
