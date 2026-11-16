import type { Metadata } from 'next';
import { GovernanceOverviewView } from '@/components/governance/GovernanceViews';

export const metadata: Metadata = { title: 'Governance overview' };

export default function Page() {
  return <GovernanceOverviewView />;
}
