import type { Metadata } from 'next';
import { PerformanceView } from '@/components/governance/GovernanceViews';

export const metadata: Metadata = { title: 'Execution performance' };

export default function Page() {
  return <PerformanceView />;
}
