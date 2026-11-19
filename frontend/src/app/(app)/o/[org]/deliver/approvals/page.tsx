import type { Metadata } from 'next';
import { PlanApprovalsView } from '@/components/deliver/PlanApprovalsView';

export const metadata: Metadata = { title: 'Plan approvals' };

export default function Page() {
  return <PlanApprovalsView />;
}
