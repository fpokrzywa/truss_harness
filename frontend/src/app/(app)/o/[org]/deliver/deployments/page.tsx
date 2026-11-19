import type { Metadata } from 'next';
import { DeploymentsView } from '@/components/deliver/DeploymentsView';

export const metadata: Metadata = { title: 'Deployments' };

export default function Page() {
  return <DeploymentsView />;
}
