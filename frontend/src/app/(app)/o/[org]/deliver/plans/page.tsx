import type { Metadata } from 'next';
import { PlansView } from '@/components/deliver/PlansView';

export const metadata: Metadata = { title: 'Deployment plans' };

export default function Page() {
  return <PlansView />;
}
