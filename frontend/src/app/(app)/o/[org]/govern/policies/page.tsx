import type { Metadata } from 'next';
import { PoliciesView } from '@/components/governance/PoliciesView';

export const metadata: Metadata = { title: 'Governance policies' };

export default function Page() {
  return <PoliciesView />;
}
