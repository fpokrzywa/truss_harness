import type { Metadata } from 'next';
import { CostExplorerView } from '@/components/governance/UsageViews';

export const metadata: Metadata = { title: 'Cost explorer' };

export default function Page() {
  return <CostExplorerView />;
}
