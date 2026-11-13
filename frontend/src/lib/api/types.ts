export type ScopeStatus = 'active' | 'archived';

export interface User {
  id: string;
  email: string;
  display_name: string;
  is_platform_admin: boolean;
}

export interface Org {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  status: ScopeStatus;
  row_version: number;
  created_at?: string;
  updated_at?: string;
}

export interface Membership {
  org: Pick<Org, 'id' | 'slug' | 'name' | 'status'>;
  roles: string[];
}

export interface Session {
  user: User;
  csrf_token: string;
  memberships: Membership[];
  expires_at: string;
  /** Enabled feature areas; absent on older backends (treated as tenancy only). */
  features?: string[];
}

export interface OidcProvider {
  id: string;
  display_name: string;
  login_url: string;
}

export interface AuthConfig {
  local_enabled: boolean;
  oidc_providers: OidcProvider[];
}

export interface Workspace {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  status: ScopeStatus;
  row_version: number;
  created_at?: string;
  updated_at?: string;
}

export interface Project extends Workspace {
  workspace_slug?: string;
}

export interface ListResponse<T> {
  items: T[];
  next_cursor: string | null;
}

export interface CreateScopeInput {
  slug: string;
  name: string;
  description?: string;
}

export interface CreateOrgInput {
  slug: string;
  name: string;
  admin_email?: string;
}

export interface DashboardActivity {
  at: string;
  actor: string | null;
  action: string;
  target: string | null;
}

export interface Dashboard {
  scope: { org: string; workspace: string | null; project: string | null };
  counts: { workspaces?: number; projects?: number; members?: number };
  recent_activity: DashboardActivity[];
}

export interface PlatformStatus {
  version: string;
  environment: string;
  database: { status: string; revision?: string | null; head?: string | null };
  auth: { local_enabled: boolean; oidc_providers: number };
  worker: { status: string; count?: number; last_heartbeat_at?: string | null };
}

export interface Readiness {
  status: string;
  checks?: Record<string, string>;
}

/** "applied": the platform acts on the values. "stored": recorded by the profile only. */
export type SectionStatus = 'applied' | 'stored';

export type InstallationSection =
  | 'branding'
  | 'approved_vendors'
  | 'feature_flags'
  | 'security'
  | 'retention'
  | 'limits'
  | 'governance_defaults'
  | 'identity_providers'
  | 'connections'
  | 'model_providers';

/** `GET /api/v1/platform/installation` (PHASE_9_CONTRACT section 6). Sections may be `{}`/absent. */
export interface PlatformInstallation {
  /** Absent or missing keys must be treated as "stored" (safe default). */
  section_status?: Partial<Record<InstallationSection, SectionStatus>>;
  version: string;
  env: string;
  deployment_mode: string;
  migration: { current: string | null; expected_head: string | null; ok: boolean };
  profile: { name: string; version: string; applied_at: string; checksum?: string } | null;
  feature_flags?: Record<string, boolean>;
  auth?: {
    local_enabled?: boolean;
    oidc_providers?: { name: string; issuer_host: string }[];
  };
  security?: {
    session_idle_minutes?: number;
    session_absolute_hours?: number;
    api_key_max_days?: number;
    egress_default_allow_private?: boolean;
  };
  retention?: {
    audit_days?: number;
    run_artifacts_days?: number;
    enforcement?: string;
  };
  limits?: {
    max_run_seconds?: number;
    max_tool_calls?: number;
    inline_output_max_bytes?: number;
  };
  governance_defaults?: Record<string, unknown>;
  workers?: { name: string; last_seen_at: string; status: 'alive' | 'stale' }[] | null;
}

// Shell-owned copies of two authz types. The shell needs them for permission-aware navigation
// (`usePermissions`) and for the shared test fixtures; SETH kept them in `security-types.ts`
// (authz/audit area). The authz port re-exports these from there.
export interface MyPermissions {
  permissions: string[];
}

export interface PermissionDef {
  key: string;
  resource_type: string;
  action: string;
  description?: string | null;
  is_privileged: boolean;
}
