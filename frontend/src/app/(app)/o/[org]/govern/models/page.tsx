import type { Metadata } from 'next';
import { ModelConsumptionView } from '@/components/governance/UsageViews';

export const metadata: Metadata = { title: 'Model consumption' };

export default function Page() {
  return <ModelConsumptionView />;
}
