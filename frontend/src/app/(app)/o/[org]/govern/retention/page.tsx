import type { Metadata } from 'next';
import { RetentionView } from '@/components/governance/GovernanceViews';

export const metadata: Metadata = { title: 'Data retention' };

export default function Page() {
  return <RetentionView />;
}
