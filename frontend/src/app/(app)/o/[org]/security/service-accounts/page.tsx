import type { Metadata } from 'next';
import { ServiceAccountsView } from '@/components/security/ServiceAccountsView';

export const metadata: Metadata = { title: 'Service accounts' };

export default function Page() {
  return <ServiceAccountsView />;
}
