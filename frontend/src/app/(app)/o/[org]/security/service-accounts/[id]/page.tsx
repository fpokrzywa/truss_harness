import type { Metadata } from 'next';
import { ServiceAccountDetailView } from '@/components/security/ServiceAccountsView';

export const metadata: Metadata = { title: 'Service account' };

export default function Page() {
  return <ServiceAccountDetailView />;
}
