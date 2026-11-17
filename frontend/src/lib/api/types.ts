export type ScopeStatus = 'active' | 'archived';
/** A client's status: only the platform administrator suspends one (F-03). */
export type OrgStatus = 'active' | 'suspended' | 'archived';

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
  status: OrgStatus;
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

/**
 * `GET /api/v1/platform/installation`. The unified backend drops `profile`, `retention`,
 * `governance_defaults` and `section_status` (B-1101, B-1102); they are not declared here.
 * `workers` is `null` until the runtime module exists (B-1105).
 */
export interface PlatformInstallation {
  version: string;
  env: string;
  deployment_mode: string;
  migration: { current: string | null; expected_head: string | null; ok: boolean };
  feature_flags?: Record<string, boolean>;
  auth?: {
    local_enabled?: boolean;
    oidc_providers?: { name: string; issuer_host: string | null }[];
  };
  security?: {
    session_idle_minutes?: number;
    session_absolute_hours?: number;
    api_key_max_days?: number;
    egress_default_allow_private?: boolean;
  };
  limits?: {
    max_run_seconds?: number;
    max_tool_calls?: number;
    inline_output_max_bytes?: number;
  };
  workers?: { name: string; last_seen_at: string; status: 'alive' | 'stale' }[] | null;
}

/** `POST /api/auth/setup/redeem`: a session, or a pending OIDC link. */
export type SetupRedeemResult = Session | { status: 'pending_oidc_link' };

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
