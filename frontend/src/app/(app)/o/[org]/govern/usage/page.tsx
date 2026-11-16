import type { Metadata } from 'next';
import { UsageView } from '@/components/governance/UsageViews';

export const metadata: Metadata = { title: 'Token usage' };

export default function Page() {
  return <UsageView />;
}
