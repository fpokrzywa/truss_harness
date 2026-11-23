/** The release board (backend `shifde.board.schemas`); a field the caller may not read is null. */

import type { DeploymentStatus } from './delivery-types';
import type { EvaluationStatus } from './evaluation-types';

export type PlanCellStatus = 'requested' | 'planning' | 'sealed' | 'approved' | 'expired';

export interface BoardRef {
  id: string;
  slug: string;
  name: string;
}

export interface BoardEvaluation {
  run_id: string;
  status: EvaluationStatus;
}

export interface BoardRelease {
  id: string;
  version_no: number;
  digest12: string;
  status: 'sealed' | 'withdrawn';
  sealed_at: string;
  evaluation: BoardEvaluation | null;
}

export interface BoardCurrent {
  release_id: string;
  version_no: number;
  deployment_id: string;
  verified_at: string;
}

export interface BoardAttempt {
  deployment_id: string;
  release_id: string;
  status: DeploymentStatus;
  status_reason: string | null;
  updated_at: string | null;
}

export interface BoardOpenPlan {
  plan_id: string;
  release_id: string;
  status: PlanCellStatus;
  expires_at: string | null;
}

export interface BoardTarget {
  target_id: string;
  name: string;
  environment: string;
  platform_kind: string;
  current: BoardCurrent | null;
  last_attempt: BoardAttempt | null;
  open_plan: BoardOpenPlan | null;
}

export interface BoardSolution {
  id: string;
  name: string;
  status: 'active' | 'archived';
  latest_version_no: number | null;
  releases: BoardRelease[];
  targets: BoardTarget[];
}

export interface BoardProject {
  workspace: BoardRef;
  project: BoardRef;
  truncated: boolean;
  solutions: BoardSolution[];
}

export interface BoardOut {
  generated_at: string;
  projects: BoardProject[];
  next_cursor: string | null;
}

export interface BoardQuery {
  workspace?: string;
  project?: string;
  cursor?: string;
  limit?: number;
}
