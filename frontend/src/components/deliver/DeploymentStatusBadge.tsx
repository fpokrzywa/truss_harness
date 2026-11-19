'use client';

import type { DeploymentStatus } from '@/lib/api/delivery-types';
import { Badge } from '../admin/ui';

const TONE: Record<DeploymentStatus, 'neutral' | 'good' | 'warn' | 'bad'> = {
  queued: 'warn',
  leased: 'warn',
  applying: 'warn',
  observing: 'warn',
  verifying: 'warn',
  verified: 'good',
  expired: 'bad',
  cancelled: 'neutral',
  failed: 'bad',
  unknown_outcome: 'bad',
  resolved_failed: 'bad',
};

const LABEL: Partial<Record<DeploymentStatus, string>> = {
  unknown_outcome: 'Outcome unknown',
  resolved_failed: 'resolved failed',
};

/** Only `verified` reads as good; an unknown outcome is never shown as healthy or cancelled. */
export function DeploymentStatusBadge({ status }: { status: DeploymentStatus }) {
  return <Badge tone={TONE[status] ?? 'warn'}>{LABEL[status] ?? status}</Badge>;
}
