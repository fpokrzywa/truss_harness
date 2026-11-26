/** Delivery targets and executors (backend `shifde.delivery.schemas`). */

export type DeliveryEnvironment = 'dev' | 'test' | 'prod';
export type TargetStatus = 'active' | 'archived';

export interface TargetBindings {
  warehouse_id: string;
  serving_endpoint?: string | null;
}

export interface SnowflakeBindings {
  package: string;
  application: string;
  warehouse: string;
  cortex_model: string;
}

export interface FabricBindings {
  entra_tenant_id: string;
  lakehouse_name: string;
  notebook_name: string;
}

export interface PalantirBindings {
  marketplace_rid: string;
  product_id: string;
  version_id: string;
  namespace_rid: string;
  parent_folder_rid: string;
  ontology_rid: string;
  object_type: string;
  frontend_origin: string;
  frontend_asset_sha256: string;
  allow_preview_apis: boolean;
}

export interface DockerBindings {
  project: string;
  port: number;
  workspace_host: string;
  warehouse_id: string;
  serving_endpoint: string;
}

/** Stands in for stored bindings that no longer validate: no stored value is shown. */
export interface UnusableBindings {
  unusable: true;
}

/** A target's platform and the bindings of that platform. */
export type PlatformBindings =
  | { platform_kind: 'databricks'; bindings: TargetBindings }
  | { platform_kind: 'snowflake'; bindings: SnowflakeBindings }
  | { platform_kind: 'fabric'; bindings: FabricBindings }
  | { platform_kind: 'palantir'; bindings: PalantirBindings }
  | { platform_kind: 'docker'; bindings: DockerBindings };

export type PlatformKind = PlatformBindings['platform_kind'];

/** The bindings of one platform kind. */
export type BindingsOf<K extends PlatformKind> = Extract<
  PlatformBindings,
  { platform_kind: K }
>['bindings'];

/** As stored: any kind's bindings may be unusable, which a reader rules out before reading one. */
export type StoredPlatformBindings = {
  [K in PlatformKind]: { platform_kind: K; bindings: BindingsOf<K> | UnusableBindings };
}[PlatformKind];

export type TargetCreate = PlatformBindings & {
  workspace: string;
  project?: string | null;
  name: string;
  address: string;
  environment: DeliveryEnvironment;
  credential_ref: string;
};

export type TargetOut = StoredPlatformBindings & {
  id: string;
  name: string;
  workspace: string | null;
  project: string | null;
  address: string;
  environment: string;
  credential_ref: string;
  status: string;
  revision: number;
  created_by: string;
  created_at: string;
  updated_at: string | null;
};

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

// ---- Plans (settled call 17) -------------------------------------------------------------

export interface PlanCreate {
  target_id: string;
  /** The sealed release the plan deploys (it needs a passing evaluation). */
  release_id: string;
  application_key: string;
  display_name: string;
}

export type PlanStatus =
  'requested' | 'planning' | 'sealed' | 'approved' | 'rejected' | 'expired' | 'plan_failed';

/** `shifde.adapters.native.contracts.PlanStep`. */
export interface PlanStep {
  key: string;
  action: 'create' | 'update' | 'start' | 'delete' | 'unknown';
  resource_key: string;
  reversible: boolean;
}

export interface PlanApprovalOut {
  decision: 'approve' | 'reject';
  decided_by: string;
  decided_at: string;
  note: string | null;
}

export interface PlanOut {
  id: string;
  target_id: string;
  application_key: string;
  display_name: string;
  status: PlanStatus;
  status_reason: string | null;
  requested_by: string;
  artifact_id: string;
  artifact_digest: string;
  target_revision: number;
  steps: PlanStep[] | null;
  snapshot: Record<string, unknown> | null;
  adapter_version: string | null;
  toolchain_digest: string | null;
  digest: string | null;
  sealed_at: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string | null;
  approval: PlanApprovalOut | null;
  deployment_id: string | null;
  release_id: string | null;
  release_digest: string | null;
  evaluation_run_id: string | null;
}

// ---- Deployments -------------------------------------------------------------------------

export type DeploymentStatus =
  | 'queued'
  | 'leased'
  | 'applying'
  | 'observing'
  | 'verifying'
  | 'verified'
  | 'expired'
  | 'cancelled'
  | 'failed'
  | 'unknown_outcome'
  | 'resolved_failed';

/** One entry of `verification.checks` (`shifde.adapters.native.contracts.Check`). */
export interface VerificationCheck {
  name: string;
  status: 'passed' | 'failed' | 'skipped' | 'not_run';
  required: boolean;
  evidence: string;
}

export interface DeploymentOut {
  id: string;
  plan_id: string;
  target_id: string;
  application_key: string;
  status: DeploymentStatus;
  status_reason: string | null;
  receipt: Record<string, unknown> | null;
  verification: Record<string, unknown> | null;
  reconcile_evidence: Record<string, unknown> | null;
  observe_deadline: string | null;
  resolved_by: string | null;
  resolved_note: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface DeploymentJournalStep {
  step_key: string;
  state: 'intent' | 'complete';
  input_digest: string;
  receipt: Record<string, unknown> | null;
  recorded_at: string;
}

/** The item GET only: the deployment with its journal, ordered by `recorded_at`. */
export interface DeploymentDetail extends DeploymentOut {
  journal: DeploymentJournalStep[];
}
