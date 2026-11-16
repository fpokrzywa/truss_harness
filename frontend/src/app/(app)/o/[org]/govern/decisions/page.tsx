import type { Metadata } from 'next';
import { DecisionsView } from '@/components/governance/GovernanceViews';

export const metadata: Metadata = { title: 'Policy decisions' };

export default function Page() {
  return <DecisionsView />;
}
