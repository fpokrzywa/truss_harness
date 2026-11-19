/** Delivery targets and executors (backend `shifde.delivery.schemas`). */

export type DeliveryEnvironment = 'dev' | 'test' | 'prod';
export type TargetStatus = 'active' | 'archived';

export interface TargetBindings {
  warehouse_id: string;
}

export interface TargetCreate {
  workspace: string;
  project?: string | null;
  name: string;
  platform_kind: 'databricks';
  address: string;
  environment: DeliveryEnvironment;
  bindings: TargetBindings;
  credential_ref: string;
}

export interface TargetOut {
  id: string;
  name: string;
  workspace: string | null;
  project: string | null;
  platform_kind: string;
  address: string;
  environment: string;
  bindings: TargetBindings;
  credential_ref: string;
  status: string;
  revision: number;
  created_by: string;
  created_at: string;
  updated_at: string | null;
}

export type ExecutorStatus = 'active' | 'disabled';

export interface DeliveryExecutorOut {
  id: string;
  name: string;
  status: string;
  last_seen_at: string | null;
  version: string | null;
  created_at: string;
  updated_at: string | null;
}

export type CredentialStatus = 'live' | 'revoked' | 'expired';

/** Never the secret or its hash: the secret exists only in `CredentialIssued`. */
export interface CredentialOut {
  id: string;
  executor_id: string;
  prefix: string;
  status: CredentialStatus;
  created_by: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
}

/** Issue and rotate: the full `shx_` token, shown once. */
export interface CredentialIssued {
  credential: CredentialOut;
  secret: string;
}
